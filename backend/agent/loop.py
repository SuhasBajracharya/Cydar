"""Autonomous recon/PoC loop: ReAct over the tool registry.

Protocol (provider-agnostic text blocks, so qwen3:8b and hosted models all
work): each assistant reply is free-text reasoning ending in at most one
fenced block — ```tool {"name":…, "args":{…}} or ```report {…}.

This module holds pure loop state + protocol helpers. Threading, SSE, and
approval plumbing live in targets/agent_runtime.py; HTTP in
targets/agent_views.py.
"""

import json
import re
import time
import urllib.parse

from .client import chat_complete
from .prompts import agent_system_prompt

from recon.runner import ReconError
from recon.tools import allowed_tools, creds_summary, describe, dry_command, execute, scope_hint, validate_args

# What the model sees per tool result (full output is saved on AgentAction).
HISTORY_OUTPUT_CHARS = 4000
MAX_PARSE_RETRIES = 3

# 401/403 hits worth naming individually (vs the WAF blanket-403 flood).
_SENSITIVE_PATH_RE = re.compile(
    r"env|admin|api|config|\.git|backup|\.bak|sql|dump|dashboard|login|"
    r"console|debug|phpinfo|server-status|actuat|swagger|docs|xmlrpc|"
    r"shell|wp-|passwd|secret|token|key",
    re.IGNORECASE,
)

_GOBUSTER_LINE_RE = re.compile(
    r"^(\S+)\s+\(Status:\s*(\d+)\)\s+\[Size:\s*(\d+)\]"
    r"(?:\s+\[-->\s*(.*?)\])?\s*$"
)
_GOBUSTER_VHOST_RE = re.compile(r"^Found:\s*(\S+)(.*)$")
_NMAP_PORT_RE = re.compile(r"^(\d+)/(tcp|udp)\s+(\S+)\s+(.*?)\s*$")
_DIGEST_LIST_CAP = 40

SEVERITIES = ("info", "low", "medium", "high", "critical")

_FENCE_RE = re.compile(r"```(tool|report|json)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

_REPORT_PROSE_MARKERS = (
    "executive summary", "confirmed", "leads", "scope notes",
    "methodology", "how to verify", "final report",
)


def looks_like_unfenced_report(text):
    """True when the reply reads like a report but has no fenced block —
    i.e. the model wants to report but forgot the ```report fence."""
    low = (text or "").lower()
    if "```report" in low or "```tool" in low or "```json" in low:
        return False
    return sum(1 for m in _REPORT_PROSE_MARKERS if m in low) >= 3


class LoopError(Exception):
    """Unrecoverable loop problem (budgets exhausted, bad report, …)."""


def _cap(items):
    """Cap a digest list, noting how many more are in stored output."""
    if len(items) > _DIGEST_LIST_CAP:
        return items[:_DIGEST_LIST_CAP] + [f"… +{len(items) - _DIGEST_LIST_CAP} more in stored output"]
    return items


def _summarize_gobuster(output):
    """Lossy-but-triageable digest: every gobuster run is far bigger than
    the history window, so keep counts + each actionable class (200s,
    redirects with Location, 500s, sensitive-name 401/403s, vhosts) and
    collapse the rest (blanket 403s, 429 floods) into one line each."""
    hits200, redirects, server_err, sensitive, vhosts = [], [], [], [], []
    counts = {}
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        mv = _GOBUSTER_VHOST_RE.match(line)
        if mv:
            vhosts.append(line)
            continue
        m = _GOBUSTER_LINE_RE.match(line)
        if not m:
            continue
        path, status, size = m.group(1), int(m.group(2)), m.group(3)
        dest = (m.group(4) or "").strip()
        counts[status] = counts.get(status, 0) + 1
        if status == 200:
            hits200.append(f"{path} (Size: {size})")
        elif status in (301, 302, 303, 307, 308):
            redirects.append(f"{path} --> {dest or '(no Location)'}")
        elif status >= 500:
            server_err.append(f"{path} (Status: {status})")
        elif status in (401, 403) and _SENSITIVE_PATH_RE.search(path):
            sensitive.append(f"{path} (Status: {status})")
    total = sum(counts.values())
    summary = ", ".join(f"{s}: {counts[s]}" for s in sorted(counts)) or "no parseable hits"
    lines = [f"gobuster digest ({total} classified hits — {summary})"]
    if vhosts:
        lines += ["--- vhosts found (audit EVERY one) ---"] + _cap(vhosts)
    if hits200:
        lines += ["--- HTTP 200 (fetch bodies for forms/params) ---"] + _cap(hits200)
    if redirects:
        lines += ["--- redirects (follow Location: new hosts → header_audit) ---"] + _cap(redirects)
    if server_err:
        lines += ["--- 5xx (unhandled errors — probe carefully) ---"] + _cap(server_err)
    if sensitive:
        lines += ["--- 401/403 on SENSITIVE names (leads, not findings) ---"] + _cap(sensitive)
    n403 = counts.get(403, 0)
    if n403 > 100 and not counts.get(404):
        lines.append("NOTE: site 403s missing paths (WAF/CDN blanket-deny) — "
                     "dir-bust is LOW SIGNAL here; pivot to vhost/subdomain + content recon.")
    if counts.get(429, 0) > 20:
        lines.append("NOTE: heavy 429 throttling — do NOT re-run enumeration; "
                     "go passive (subdomain_enum, fetch titles of known pages).")
    if len(lines) == 1:
        lines.append("(no actionable hits)")
    lines.append("Full output is stored on the action row — this digest is what fits in context.")
    return "\n".join(lines)


def _summarize_nmap(output):
    """Keep the port table + service versions; drop timing boilerplate."""
    ports = []
    for raw in output.splitlines():
        m = _NMAP_PORT_RE.match(raw.strip())
        if m and m.group(3) == "open":
            ports.append(f"{m.group(1)}/{m.group(2)} open ({m.group(4).strip()})")
    if not ports:
        return None
    lines = ["nmap digest — open ports:"] + ports
    tail = [l for l in output.splitlines() if "scan report" in l.lower()][:5]
    lines += tail
    return "\n".join(lines)


def summarize_history(name, output):
    """Compact digest of a tool result for conversation history.

    The full output is always saved on the AgentAction row (audit trail);
    this is what the model keeps in context, so enumeration floods must
    degrade to triageable digests instead of a blind head-cut.
    """
    output = output or ""
    digest = None
    if name == "gobuster":
        digest = _summarize_gobuster(output)
    elif name == "nmap":
        digest = _summarize_nmap(output)
    if digest is None:
        digest = output
    if len(digest) > HISTORY_OUTPUT_CHARS:
        digest = digest[:HISTORY_OUTPUT_CHARS] + "\n…[output trimmed for context]"
    return digest


def normalize_tool_payload(payload):
    """Coerce common model variants into {"name": …, "args": {…}}.

    Small models often emit flat or aliased shapes, e.g.
    {"tool": "header_audit", "url": "https://x/"} or
    {"name": "x", "url": "..."} instead of the documented
    {"name": "x", "args": {...}}. Accept those instead of refusing.
    Also handle single-key wrapper: {"header_audit": {"url": "..."}}
    and alias "tool_name".
    """
    if not isinstance(payload, dict):
        return payload
    name = payload.get("name") or payload.get("tool") or payload.get("tool_name") or payload.get("function")
    if not isinstance(name, str) or not name.strip():
        # Single-key wrapper: {"header_audit": {"url": "..."}} — if exactly one
        # non-reserved key whose value is a dict, treat that key as the tool name.
        reserved = {"name", "tool", "tool_name", "function", "args", "arguments", "parameters"}
        non_reserved = [(k, v) for k, v in payload.items() if k not in reserved and isinstance(v, dict)]
        if len(non_reserved) == 1:
            name, args = non_reserved[0]
            return {"name": name.strip(), "args": args}
        return payload
    args = payload.get("args") or payload.get("arguments") or payload.get("parameters")
    if isinstance(args, str):
        try:
            args = json.loads(args, strict=False)
        except json.JSONDecodeError:
            args = {}
    if not isinstance(args, dict):
        # Flat shape: every key that isn't the tool/arg wrapper is an arg.
        reserved = {"name", "tool", "tool_name", "function", "args", "arguments", "parameters"}
        args = {k: v for k, v in payload.items() if k not in reserved}
    return {"name": name.strip(), "args": args}


def parse_reply(text):
    """Split a reply into (kind, thought, payload).

    kind is "tool" | "report" | "thought" | "invalid". thought is the text
    before the fenced block (or the whole reply). payload is the parsed
    JSON object, or the raw block string when JSON is broken.
    """
    match = None
    for match in _FENCE_RE.finditer(text or ""):
        pass  # take the LAST fenced block (the model's final word)
    if match is None:
        clean_text = (text or "").strip()
        brace_match = re.search(r'(\{.*\})', clean_text, re.DOTALL)
        if brace_match:
            try:
                raw_json = brace_match.group(1)
                clean_json = re.sub(r',\s*([}\]])', r'\1', raw_json)
                payload = json.loads(clean_json, strict=False)
                if "confirmed" in payload or "executive_summary" in payload or "methodology" in payload:
                    return "report", clean_text[:brace_match.start()].strip(), payload
                else:
                    return "tool", clean_text[:brace_match.start()].strip(), normalize_tool_payload(payload)
            except json.JSONDecodeError:
                pass
        return "thought", clean_text, None
    kind, raw = match.group(1).lower(), match.group(2).strip()
    # Accept ```json as an alias for ```tool (many models default to json fences)
    if kind == "json":
        kind = "tool"
    thought = text[: match.start()].strip()
    try:
        payload = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        try:
            # Local models often emit trailing commas
            clean_raw = re.sub(r',\s*([}\]])', r'\1', raw)
            payload = json.loads(clean_raw, strict=False)
        except json.JSONDecodeError:
            return "invalid", thought, raw
    if kind == "tool":
        payload = normalize_tool_payload(payload)
    return kind, thought, payload


def validate_report(data):
    """Check a ```report payload. Returns normalized dict or raises LoopError.

    Honest schema: `confirmed` (proven, evidence attached — these become
    bounty-shaped findings) vs `leads` (suspicious but unproven: a missing
    header with no injection point, an untested login wall, an odd redirect).
    Leads NEVER become findings. Either list may be empty — a clean run with
    documented leads is a valid outcome.
    """
    if not isinstance(data, dict):
        raise LoopError("Report block must be a JSON object.")
    problems = []
    for key in ("title", "executive_summary", "methodology", "scope_notes"):
        if not isinstance(data.get(key), str) or not data.get(key, "").strip():
            problems.append(f"missing/empty {key!r}")
    
    confirmed = _validate_confirmed(data.get("confirmed", []), problems)
    leads = _validate_leads(data.get("leads", []), problems)
    
    if problems:
        raise LoopError("Report rejected: " + "; ".join(problems) + ". You MUST put your findings inside the JSON 'confirmed' or 'leads' arrays, not just in prose.")
        
    return {
        "title": data.get("title", "").strip(),
        "executive_summary": data.get("executive_summary", "").strip(),
        "methodology": data.get("methodology", "").strip(),
        "scope_notes": data.get("scope_notes", "").strip(),
        "confirmed": confirmed,
        "leads": leads,
    }


def _validate_confirmed(findings, problems):
    if not isinstance(findings, list):
        problems.append("'confirmed' must be a list")
        return []
    clean = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            problems.append(f"confirmed #{i + 1} is not an object")
            continue
        missing = [k for k in (
            "title", "severity", "description", "steps_to_reproduce",
            "evidence", "impact", "remediation",
        ) if not f.get(k)]
        if missing:
            problems.append(f"confirmed #{i + 1} missing: {', '.join(missing)}")
            continue
        if f["severity"] not in SEVERITIES:
            problems.append(
                f"confirmed #{i + 1} bad severity {f['severity']!r} "
                f"(use {', '.join(SEVERITIES)})"
            )
            continue
        steps = f["steps_to_reproduce"]
        if isinstance(steps, str):
            f["steps_to_reproduce"] = [steps]
        elif not isinstance(steps, list) or not steps:
            problems.append(f"confirmed #{i + 1} needs non-empty steps_to_reproduce")
            continue
        if not str(f.get("evidence", "")).strip():
            problems.append(f"confirmed #{i + 1} has no evidence — demote it to a lead")
            continue
        clean.append(f)
    return clean


def _validate_leads(leads, problems):
    if not isinstance(leads, list):
        problems.append("'leads' must be a list")
        return []
    clean = []
    for i, lead in enumerate(leads):
        if not isinstance(lead, dict):
            problems.append(f"lead #{i + 1} is not an object")
            continue
        missing = [k for k in (
            "title", "suspicion", "why_suspicious", "how_to_verify", "confidence",
        ) if not lead.get(k)]
        if missing:
            problems.append(f"lead #{i + 1} missing: {', '.join(missing)}")
            continue
        if lead["confidence"] not in ("low", "medium", "high"):
            problems.append(f"lead #{i + 1} bad confidence (use low|medium|high)")
            continue
        steps = lead["how_to_verify"]
        if isinstance(steps, str):
            lead["how_to_verify"] = [steps]
        elif not isinstance(steps, list) or not steps:
            problems.append(f"lead #{i + 1} needs non-empty how_to_verify")
            continue
        clean.append(lead)
    return clean


def render_report_markdown(report, target, meta):
    """Submission-style markdown: confirmed findings (proven, evidence
    attached) strictly separated from leads (unproven — exact manual
    verification steps included so the operator can confirm in minutes)."""
    lines = [
        f"# {report['title']}",
        "",
        f"**Target:** {target.domain}",
        f"**Date:** {meta.get('date', '')}",
        f"**Produced by:** autonomous agent ({meta.get('provider', '')}/"
        f"{meta.get('model', '')}, {meta.get('mode', '')} mode, "
        f"{meta.get('tool_calls', 0)} tool calls)",
        "",
        "## Executive summary",
        "",
        report["executive_summary"],
        "",
        "## Scope",
        "",
        f"In-scope assets: {(target.in_scope or '(target domain)').strip()}",
        f"Out-of-scope: {(target.out_of_scope or '(none listed)').strip()}",
        "",
        report["scope_notes"],
        "",
        "## Methodology",
        "",
        report["methodology"],
        "",
        f"## Confirmed findings ({len(report['confirmed'])})",
        "",
        "_Every item below was reproduced by a tool; evidence is attached. "
        "Anything unproven lives under Leads, not here._",
        "",
    ]
    if not report["confirmed"]:
        lines += ["None. See Leads below for what deserves manual follow-up.", ""]
    for i, f in enumerate(report["confirmed"], 1):
        lines += [
            f"### [{f['severity'].upper()}] C{i}: {f['title']}",
            "",
            "**Description:**",
            "",
            f["description"],
            "",
            "**Steps to reproduce:**",
            "",
            *[f"{n}. {s}" for n, s in enumerate(f["steps_to_reproduce"], 1)],
            "",
            "**Evidence:**",
            "",
            "```",
            f["evidence"],
            "```",
            "",
            "**Impact:**",
            "",
            f["impact"],
            "",
            "**Remediation:**",
            "",
            f["remediation"],
            "",
        ]
    lines += [
        f"## Leads for manual follow-up ({len(report['leads'])})",
        "",
        "_Suspicious but unproven. Each lead names the exact check that would "
        "confirm or kill it — work highest confidence first._",
        "",
    ]
    if not report["leads"]:
        lines += ["None — the engagement covered the reachable surface.", ""]
    for i, lead in enumerate(report["leads"], 1):
        lines += [
            f"### L{i}: {lead['title']} (confidence: {lead['confidence']})",
            "",
            f"**Suspicion:** {lead['suspicion']}",
            "",
            f"**Why suspicious:** {lead['why_suspicious']}",
            "",
            "**How to verify:**",
            "",
            *[f"{n}. {s}" for n, s in enumerate(lead["how_to_verify"], 1)],
            "",
        ]
    return "\n".join(lines)


class AgentLoop:
    """Conversation state + budget tracking for one autonomous session."""

    def __init__(self, target, provider, model_id, max_iterations=25,
                 time_budget=1800):
        self.target = target
        self.provider = provider
        self.model_id = model_id
        self.max_iterations = max_iterations
        self.time_budget = time_budget
        self.started = time.monotonic()
        self.iterations = 0
        self.tool_calls = 0
        self.parse_failures = 0
        self.nudges = 0
        self.seen_calls = set()  # exact (name, args) already executed — no repeats
        self.dead_hosts = set()  # DNS-dead hosts — never retried this session
        slots = creds_summary(target)
        system = agent_system_prompt(
            describe(target.automation_level), target.automation_level
        )
        user = (
            f"Target domain/scope: {target.domain}\n"
            f"In-scope assets:\n{target.in_scope or '(not specified — assume the domain and its subdomains)'}\n"
            f"Out-of-scope:\n{target.out_of_scope or '(not specified)'}\n"
            f"Allowed automation level: {target.automation_level}\n"
            f"Saved sessions (per host): "
            f"{', '.join(slots) if slots else 'none — login-walled areas need a session (operator paste), trial_signup where allowed, or list them as leads'}. "
            f"Verify with auth_check before trusting authed results.\n\n"
            f"Program scope rules (verbatim):\n{target.scope_rules}\n\n"
            f"{scope_hint(target)}\n\n"
            "Begin the engagement. Reason briefly, then call your first tool "
            "or write the report if no testing is needed."
        )
        self.messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        self.system_snapshot = system

    def budget_left(self):
        return (
            self.iterations < self.max_iterations
            and (time.monotonic() - self.started) < self.time_budget
        )

    def think(self):
        """One model call. Returns raw text (transport errors propagate)."""
        text = chat_complete(self.messages, provider=self.provider, model=self.model_id)
        self.messages.append({"role": "assistant", "content": text})
        self.iterations += 1
        return text

    def note(self, text, role="user"):
        """Inject a message (tool result, rejection, guidance, nudge)."""
        self.messages.append({"role": role, "content": text})

    def tool_result_note(self, name, output):
        """Append a triageable digest of the tool result for the next iteration."""
        self.note(f"TOOL RESULT [{name}]:\n{summarize_history(name, output)}")

    _REPEATABLE = {"auth_check"}  # re-verifying a refreshed session is legitimate
    _DEAD_MARKERS = (
        "name or service not known", "nodename nor servname",
        "no address associated", "dns lookup failed", "name resolution",
    )

    @staticmethod
    def _call_host(name, args):
        """Host a proposal targets (for dead-host memory), or None."""
        try:
            if name in ("dns_lookup",):
                return (args.get("host") or "").lower() or None
            if name in ("subdomain_enum",):
                return ((args.get("domain") or "").lower() or None)
            if name == "auth_check":
                return ((args.get("host") or "").lower() or None)
            url = (args.get("url") or "")
            if isinstance(url, str) and "://" in url:
                return urllib.parse.urlparse(url).hostname
            urls = args.get("urls") or []
            if isinstance(urls, str):
                urls = [urls]
            hosts = set()
            for u in urls if isinstance(urls, list) else []:
                if isinstance(u, str) and "://" in u:
                    h = urllib.parse.urlparse(u).hostname
                    if h:
                        hosts.add(h.lower())
            return ",".join(sorted(hosts)) or None
        except (AttributeError, ValueError):
            return None

    def note_tool_failure(self, name, args, error_text):
        """Remember DNS-dead hosts so the model never retries them."""
        low = (error_text or "").lower()
        if not any(m in low for m in self._DEAD_MARKERS):
            return
        host = self._call_host(name, args if isinstance(args, dict) else {})
        if host and "," not in host:
            self.dead_hosts.add(host)

    def check_tool(self, name, args):
        """Pre-execution validation: known tool + level gate + arg shapes.
        Raises ReconError. Runs at proposal time so broken calls are refused
        BEFORE the approval screen (no wasted operator approvals)."""
        if name not in allowed_tools(self.target.automation_level):
            raise ReconError(
                f"Tool {name!r} is not available at automation level "
                f"'{self.target.automation_level}'. Available: "
                f"{allowed_tools(self.target.automation_level)}."
            )
        if not isinstance(args, dict):
            raise ReconError("Tool args must be a JSON object.")
        validate_args(name, args, self.target)
        sig = (name, json.dumps(args, sort_keys=True))
        if name not in self._REPEATABLE and sig in self.seen_calls:
            raise ReconError(
                f"Already ran this exact {name} call — the result is in "
                f"history above. Do NOT repeat it; results don't change. "
                f"Follow up on the output or move on."
            )
        host = self._call_host(name, args)
        if host and "," not in host and host in self.dead_hosts:
            raise ReconError(
                f"Host {host} is DNS-dead (failed earlier this session) — "
                f"do not retry it. Treat its surface as unreachable and move on."
            )
        return dry_command(name, args, self.target)

    def run_tool(self, name, args):
        """Execute + count. Returns (command, output). Raises ReconError."""
        command, output = execute(name, args, self.target)
        self.tool_calls += 1
        if name not in self._REPEATABLE and isinstance(args, dict):
            self.seen_calls.add((name, json.dumps(args, sort_keys=True)))
        return command, output
