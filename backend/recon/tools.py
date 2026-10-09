"""Tool registry for the autonomous agent loop.

The LLM never runs raw shell: it picks a tool name + JSON args, and the
backend validates + executes via fixed argv (nmap/gobuster/dig) or stdlib
HTTP (urllib). Every tool enforces scope through `host_in_scope()` — the
target's domain plus `in_scope` lines, minus `out_of_scope` lines.

Tool spec: {"description", "args", "min_level", "run"} where `run(args,
target) -> (command_str, output_str)`. `describe(level)` renders the
prompt block listing only tools the automation level allows.
"""

import json
import os
import random
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .mailbox import MailboxError
from .mailbox import status as mailbox_status

from .runner import NMAP_PROFILES, SUBDOMAIN_WORDLISTS, ReconError, clean_host, run_preset

HTTP_TIMEOUT_SECONDS = 25
MAX_HTTP_BYTES = 50_000


class AuthExpired(ReconError):
    """Session credentials rejected — operator must paste a fresh token."""


class AuthNeeded(ReconError):
    """Login wall with no usable saved session — operator decision required.

    The runtime pauses (semi AND auto) for: paste-a-session / skip-host.
    Carries the wall host so the decision can be scoped per subdomain."""

    def __init__(self, message, host=""):
        super().__init__(message)
        self.host = (host or "").lower()


def _iter_credentials(target):
    """Yield (host_pattern_or_None, type, name, value) session slots:
    explicit per-host rows first, the legacy single slot last (matches all
    hosts). Tolerates non-ORM targets (tests) via duck typing."""
    try:
        slots = list(target.auth_credentials.all())
    except AttributeError:
        slots = []
    # Most-specific pattern first so api.example.com beats *.example.com.
    slots.sort(key=lambda s: len(getattr(s, "host_pattern", "")), reverse=True)
    for s in slots:
        if getattr(s, "auth_type", "") and getattr(s, "auth_value", ""):
            yield s.host_pattern, s.auth_type, s.auth_name, s.auth_value
    if getattr(target, "auth_type", "") and getattr(target, "auth_value", ""):
        yield None, target.auth_type, target.auth_name, target.auth_value


def creds_for_host(target, host):
    """Return (auth_type, auth_name, auth_value) for `host`, or None."""
    host = (host or "").lower()
    for pattern, atype, aname, avalue in _iter_credentials(target):
        if pattern is None or _match_host(host, pattern):
            return atype, aname, avalue
    return None


def creds_summary(target):
    """Short operator-facing inventory, e.g. ['*.example.com (cookie)']."""
    return [f"{p or '*'} ({t})" for p, t, _, _ in _iter_credentials(target)]


def redact(text, secret):
    """Scrub a live secret from text before storage/display. The agent never
    sees the value (tools inject it server-side), but authed responses can
    echo it back — so every stored/emitted string passes through here."""
    if not text or not secret or len(secret) < 4:
        return text or ""
    out = text
    for variant in {secret, secret.strip()}:
        if variant and len(variant) >= 4:
            out = out.replace(variant, "[REDACTED]")
    return out


def _auth_headers(target, host=""):
    """Build request headers from the session saved for `host`.

    Per-host AuthCredential rows win; the legacy single slot (if set)
    matches all hosts. The model never sees the secret — it only knows
    auth is configured. Names are strictly validated (no newlines/colons)
    against header injection.
    """
    creds = creds_for_host(target, host)
    if creds is None:
        return {}
    auth_type, name, value = creds
    if auth_type == "bearer":
        return {"Authorization": f"Bearer {value}"}
    name = (name or "").strip()
    if auth_type == "header":
        if not re.fullmatch(r"[A-Za-z0-9-]+", name):
            raise ReconError(f"Invalid auth header name {name!r}.")
        return {name: value}
    if auth_type == "cookie":
        if not re.fullmatch(r"[^=;\s]+", name):
            raise ReconError(f"Invalid cookie name {name!r}.")
        return {"Cookie": f"{name}={value}"}
    raise ReconError(f"Unknown auth_type {auth_type!r}.")


def _match_host(host, pattern):
    """Substring or `*.example.com` wildcard match (case-insensitive)."""
    host = host.lower()
    pattern = pattern.strip().lower()
    if not pattern:
        return False
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".example.com"
        return host.endswith(suffix) and len(host) > len(suffix)
    return pattern in host


def parse_scope_list(text):
    """Split a scope box into clean host rules.

    Users paste comma-joined lines, bare hosts, wildcards (`*.example.com`),
    even full URLs — split on newlines/commas/semicolons, extract hostnames
    from URLs, drop ports/slashes/empties. Returns a de-duplicated list.
    """
    rules = []
    for chunk in re.split(r"[\n,;]+", text or ""):
        chunk = chunk.strip().lower()
        if not chunk:
            continue
        if "://" in chunk:
            try:
                chunk = urllib.parse.urlparse(chunk).hostname or chunk
            except ValueError:
                pass
            chunk = (chunk or "").strip().lower()
        chunk = chunk.strip().strip("/")
        if chunk.count(":") == 1:
            maybe_host, _, port = chunk.partition(":")
            if port.isdigit():
                chunk = maybe_host
        if chunk and chunk not in rules:
            rules.append(chunk)
    return rules


def scope_hint(target):
    """Explicit allowed-hosts block for the agent prompt, so the model never
    has to guess which hosts URL tools accept."""
    in_rules = parse_scope_list(target.in_scope)
    out_rules = parse_scope_list(target.out_of_scope)
    if in_rules:
        lines = ["URL-based tools may ONLY use these hosts/patterns:"]
        lines += [f"  - {r}" for r in in_rules]
    else:
        lines = [f"No in-scope list given: URL tools may use {target.domain} and its subdomains only."]
    if out_rules:
        lines.append("NEVER touch (out of scope): " + ", ".join(out_rules))
    return "\n".join(lines)


def host_in_scope(host, target):
    """True when `host` is covered by in_scope (or the target domain as
    fallback) and not excluded by out_of_scope. Raises ReconError if out."""
    host = host.lower()
    out_rules = parse_scope_list(target.out_of_scope)
    if any(_match_host(host, rule) for rule in out_rules):
        raise ReconError(f"Host {host} is OUT of scope — refusing.")
    in_rules = parse_scope_list(target.in_scope)
    if in_rules:
        if any(_match_host(host, rule) for rule in in_rules):
            return True
        raise ReconError(
            f"Host {host} is not in the in-scope asset list — refusing."
        )
    domain = (target.domain or "").strip().lower()
    if host == domain or (domain and host.endswith("." + domain)):
        return True
    raise ReconError(
        f"Host {host} is outside the target ({target.domain}) — refusing."
    )


def _check_url(raw_url, target):
    """Validate a URL arg: http(s) only, host in scope. Returns (url, host)."""
    parts = urllib.parse.urlparse((raw_url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ReconError(f"Invalid URL {raw_url!r}: need http(s)://host/….")
    host = clean_host(parts.hostname)
    host_in_scope(host, target)
    return parts._replace(netloc=host + (f":{parts.port}" if parts.port else "")).geturl(), host


def _tls_hint(exc, insecure):
    """Operator-actionable suffix for TLS failures (lab targets only).

    Small models tend to declare the target dead on a cert error instead of
    pivoting — this names the exact fix. Only shown when verification is on;
    with insecure=True a TLS failure is a real transport problem.
    """
    if insecure:
        return ""
    low = str(exc).lower()
    if "certificate" in low or "self-signed" in low or "self signed" in low:
        return (" [TLS hint: certificate verification failed — if this is an "
                "authorized lab target with a self-signed/expired cert, enable "
                "'Allow insecure TLS' on the target (Edit target) and retry; "
                "meanwhile plain-http and non-TLS recon still work.]")
    return ""


def _http_get(url, extra_headers=None, insecure=False, timeout=HTTP_TIMEOUT_SECONDS):
    """GET with a bounty-recon UA (+ optional session auth headers).
    Returns (status, headers, body, chain)."""
    headers = {"User-Agent": "Cydar/1.0 (authorized recon)"}
    headers.update(extra_headers or {})
    req = urllib.request.Request(url, headers=headers)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    # Insecure: skip TLS certificate verification (for self-signed lab targets).
    if insecure:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_HTTP_BYTES + 1)
            body = raw[:MAX_HTTP_BYTES].decode("utf-8", "replace")
            if len(raw) > MAX_HTTP_BYTES:
                body += "\n…[body truncated]"
            return resp.status, dict(resp.headers.items()), body, []
    except urllib.error.HTTPError as exc:
        # Plain urllib raises for 3xx when redirects are disabled — that IS
        # the signal redirect checks need (status + Location header).
        if exc.code in (301, 302, 303, 307, 308):
            return exc.code, dict(exc.headers.items()), "", []
        raw = (exc.read(MAX_HTTP_BYTES + 1) or b"")[:MAX_HTTP_BYTES]
        return exc.code, dict(exc.headers.items()), raw.decode("utf-8", "replace"), []
    except Exception as exc:
        raise ReconError(f"HTTP fetch failed for {url}: {exc}.{_tls_hint(exc, insecure)}")


def _http_post(url, data, content_type, extra_headers=None, insecure=False):
    """POST (no redirect following, so 302s are observable). Returns
    (status, headers, body) — error bodies (400/415/…) are captured, not raised."""
    headers = {"User-Agent": "Cydar/1.0 (authorized recon)",
               "Content-Type": content_type}
    headers.update(extra_headers or {})

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    # Insecure: skip TLS certificate verification (for self-signed lab targets).
    if insecure:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read(MAX_HTTP_BYTES + 1)
            body = raw[:MAX_HTTP_BYTES].decode("utf-8", "replace")
            return resp.status, dict(resp.headers.items()), body
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return exc.code, dict(exc.headers.items()), ""
        raw = (exc.read(MAX_HTTP_BYTES + 1) or b"")[:MAX_HTTP_BYTES]
        return exc.code, dict(exc.headers.items()), raw.decode("utf-8", "replace")
    except Exception as exc:
        raise ReconError(f"HTTP POST failed for {url}: {exc}.{_tls_hint(exc, insecure)}")


def _run_http_fetch(args, target):
    hh = ((args.get("host_header") or "").strip()
          if isinstance(args.get("host_header"), str) else "")
    url, conn_host = _check_url(args.get("url", ""), target)
    host = conn_host
    # vhost via Host header (curl -H 'Host:' style) for names with no DNS.
    extra = dict(_auth_headers(target, conn_host))
    via = ""
    if hh:
        host = clean_host(hh)
        host_in_scope(host, target)
        extra = dict(_auth_headers(target, host))
        extra["Host"] = host
        via = f" (Host: {host})"
    insecure = getattr(target, "allow_insecure_tls", False)
    authed = " (with saved session)" if creds_for_host(target, host) else ""
    status, headers, body, _ = _http_get(url, extra, insecure)
    head = "\n".join(f"{k}: {v}" for k, v in headers.items())
    return f"GET {url}{via}{authed}", f"HTTP {status}\n--- headers ---\n{head}\n--- body (first 50k) ---\n{body}"


_FETCH_MANY_MAX_URLS = 10
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _run_fetch_many(args, target):
    """Batch-follow a gobuster/dir list (or any URL set) in ONE tool call and
    ONE semi-auto approval: compact status/title table instead of N fetches."""
    urls = args.get("urls", [])
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list) or not urls:
        raise ReconError("fetch_many needs 'urls': a non-empty list of URLs (max 10).")
    if len(urls) > _FETCH_MANY_MAX_URLS:
        raise ReconError(f"fetch_many takes at most {_FETCH_MANY_MAX_URLS} URLs per call.")
    insecure = getattr(target, "allow_insecure_tls", False)
    lines = [f"fetch-many: {len(urls)} URLs (per-host sessions applied)"]
    for raw in urls:
        raw = str(raw or "").strip()
        if not raw:
            continue
        try:
            url, host = _check_url(raw, target)
        except ReconError as exc:
            lines.append(f"-- {raw}: SKIPPED ({exc})")
            continue
        try:
            status, headers, body, _ = _http_get(url, _auth_headers(target, host), insecure)
        except ReconError as exc:
            lines.append(f"-- {url}: FAILED ({exc})")
            continue
        loc = headers.get("Location", "")
        ctype = headers.get("Content-Type", "?").split(";")[0].strip()
        title = ""
        m = _TITLE_RE.search(body or "")
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
        summary = f"-- {url}: HTTP {status}, {ctype}, {len(body or '')} bytes"
        if loc:
            summary += f", Location: {loc}"
        if title:
            summary += f", title: {title!r}"
        lines.append(summary)
    lines.append("Follow up: 200s with forms/params → reflection/sqli probes; "
                 "redirects to new hosts → header_audit them; 403 on sensitive "
                 "names → leads with manual verify steps.")
    return f"fetch-many ({len(urls)} urls)", "\n".join(lines)


def _run_dns_lookup(args, target):
    host = clean_host(args.get("host", ""))
    host_in_scope(host, target)
    rtype = (args.get("type") or "A").strip().upper()
    if rtype in ("A", "AAAA"):
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ReconError(f"DNS lookup failed for {host}: {exc}.")
        addrs = sorted({i[4][0] for i in infos})
        return f"dns {rtype} {host}", f"{host} {rtype}: {', '.join(addrs) or '(no records)'}"
    if shutil.which("dig") is None:
        raise ReconError("Only A/AAAA lookups are supported (no dig on server).")
    if rtype not in ("MX", "TXT", "CNAME", "NS"):
        raise ReconError(f"Unsupported record type {rtype!r} (use A, AAAA, MX, TXT, CNAME, NS).")
    proc = subprocess.run(
        ["dig", "+short", rtype, host], capture_output=True, text=True, timeout=20
    )
    out = (proc.stdout or "").strip() or "(no records)"
    return f"dig +short {rtype} {host}", out


def _run_header_audit(args, target):
    hh = ((args.get("host_header") or "").strip()
          if isinstance(args.get("host_header"), str) else "")
    url, conn_host = _check_url(args.get("url", ""), target)
    host = conn_host
    extra = dict(_auth_headers(target, conn_host))
    via = ""
    if hh:
        host = clean_host(hh)
        host_in_scope(host, target)
        extra = dict(_auth_headers(target, host))
        extra["Host"] = host
        via = f" (Host: {host})"
    insecure = getattr(target, "allow_insecure_tls", False)
    status, headers, _, _ = _http_get(url, extra, insecure)
    h = {k.lower(): v for k, v in headers.items()}
    checks = [
        ("strict-transport-security", "HSTS"),
        ("content-security-policy", "CSP"),
        ("x-frame-options", "anti-clickjacking"),
        ("x-content-type-options", "MIME-sniffing protection"),
        ("referrer-policy", "referrer policy"),
    ]
    lines = [f"GET {url}{via} -> HTTP {status}"]
    for header, label in checks:
        lines.append(f"[{'OK' if header in h else 'MISSING'}] {label} ({header})")
    set_cookie = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
    if set_cookie:
        flags = []
        low = set_cookie.lower()
        flags.append("Secure" if "secure" in low else "NO-SECURE-FLAG")
        flags.append("HttpOnly" if "httponly" in low else "NO-HTTPONLY-FLAG")
        flags.append("SameSite" if "samesite" in low else "NO-SAMESITE")
        lines.append("cookies: " + ", ".join(flags))
    else:
        lines.append("cookies: none set")
    server = headers.get("Server", "(hidden)")
    lines.append(f"Server banner: {server}")
    return f"header-audit {url}{via}", "\n".join(lines)


def _run_reflection_test(args, target):
    """Benign canary reflection probe (detection only, no exploitation)."""
    raw_url = (args.get("url") or "").strip()
    param = (args.get("param") or "q").strip()
    if not param.replace("_", "").replace("-", "").isalnum():
        raise ReconError(f"Invalid param name {param!r}.")
    url, host = _check_url(raw_url, target)
    insecure = getattr(target, "allow_insecure_tls", False)
    canary = "bbpoc7x9canary"
    sep = "&" if "?" in url else "?"
    probe = f"{url}{sep}{urllib.parse.quote(param)}={canary}"
    status, _, body, _ = _http_get(probe, _auth_headers(target, host), insecure)
    reflected = canary in body
    ctx = []
    if reflected:
        idx = body.find(canary)
        window = body[max(0, idx - 60): idx + len(canary) + 60]
        ctx.append(f"context window: …{window}…")
        low = window.lower()
        if "<script" in low or "onerror" in low or "javascript:" in low:
            ctx.append("note: reflection is near script/event-handler context")
        else:
            ctx.append("note: reflection looks like plain HTML/text context")
    verdict = "REFLECTED (candidate for manual XSS confirmation)" if reflected else "not reflected"
    return f"reflect {param} on {url}", f"GET {probe}\nHTTP {status}\nverdict: {verdict}\n" + "\n".join(ctx)


def _run_open_redirect_check(args, target):
    """Observe (never follow) whether a param makes the target redirect out."""
    raw_url = (args.get("url") or "").strip()
    param = (args.get("param") or "next").strip()
    if not param.replace("_", "").replace("-", "").isalnum():
        raise ReconError(f"Invalid param name {param!r}.")
    url, host = _check_url(raw_url, target)
    insecure = getattr(target, "allow_insecure_tls", False)
    # Point at an unregistered canary host so a redirect is unambiguous, and
    # never fetch it (redirects are not followed).
    sink = "http://bb-poc-sink.invalid/"
    sep = "&" if "?" in url else "?"
    probe = f"{url}{sep}{urllib.parse.quote(param)}={urllib.parse.quote(sink, safe='')}"
    status, headers, _, _ = _http_get(probe, _auth_headers(target, host), insecure)
    location = headers.get("Location", "")
    if status in (301, 302, 303, 307, 308) and location:
        if "bb-poc-sink.invalid" in location:
            verdict = "OPEN REDIRECT: target redirects to attacker-supplied URL"
        else:
            verdict = f"redirects, but not to payload (Location: {location})"
    else:
        verdict = f"no redirect (HTTP {status})"
    return f"redirect-check {param} on {url}", f"GET {probe}\nHTTP {status}\nLocation: {location or '(none)'}\nverdict: {verdict}"


_SQL_ERROR_SIGS = (
    "sql syntax", "mysql", "mysqli", "odbc", "ora-", "oracle", "postgresql",
    "psql:", "sqlite", "sqlstate", "unterminated", "quoted string",
    "sql server", "jdbc", "pyodbc", "pg_query", "warning: pg_",
)


def _run_sqli_error_probe(args, target):
    """Error-based SQLi detection: quote characters appended to one param,
    response diffed for DB error signatures. Read-only GETs, no extraction."""
    raw_url = (args.get("url") or "").strip()
    param = (args.get("param") or "id").strip()
    if not param.replace("_", "").replace("-", "").isalnum():
        raise ReconError(f"Invalid param name {param!r}.")
    url, host = _check_url(raw_url, target)
    insecure = getattr(target, "allow_insecure_tls", False)
    auth = _auth_headers(target, host)
    sep = "&" if "?" in url else "?"
    base = f"{url}{sep}{urllib.parse.quote(param)}=1"
    _, _, base_body, _ = _http_get(base, auth, insecure)
    base_len = len(base_body)
    lines = [f"baseline: GET {base} -> {base_len} bytes"]
    verdict = "no difference — not injectable via error reflection"
    for payload in ("'", '"', "\\"):
        probe = f"{url}{sep}{urllib.parse.quote(param)}=1{urllib.parse.quote(payload)}"
        try:
            status, _, body, _ = _http_get(probe, auth, insecure)
        except ReconError as exc:
            lines.append(f"payload {payload!r}: request failed ({exc})")
            continue
        low = body.lower()
        hit = next((s for s in _SQL_ERROR_SIGS if s in low), None)
        delta = abs(len(body) - base_len)
        if hit:
            idx = low.find(hit)
            lines.append(
                f"payload {payload!r}: HTTP {status}, DB ERROR SIGNATURE {hit!r} "
                f"(…{body[max(0, idx-80):idx+80].strip()}…) → SQL INJECTION CANDIDATE")
            verdict = ("SQL INJECTION CANDIDATE: quote breaks the query and the "
                       "backend leaks a DB error — confirm manually with stacked/time probes")
        else:
            lines.append(f"payload {payload!r}: HTTP {status}, {len(body)} bytes "
                         f"(Δ{delta}, no DB error text)")
            if delta > max(500, base_len // 2) and "CANDIDATE" not in verdict:
                verdict = ("behavior change without DB error text (large response delta) — "
                           "weak signal, verify manually (possible blind injection)")
    lines.append(f"verdict: {verdict}")
    return f"sqli-probe {param} on {url}", "\n".join(lines)


def _run_cors_check(args, target):
    """CORS misconfig detection: sends an evil Origin (with session when
    saved) and inspects ACAO/ACAC reflection. No exploit, just headers."""
    url, host = _check_url((args.get("url") or "").strip(), target)
    insecure = getattr(target, "allow_insecure_tls", False)
    evil = "https://bb-poc-sink.invalid"
    auth = _auth_headers(target, host)
    auth.update({"Origin": evil})
    status, headers, _, _ = _http_get(url, auth, insecure)
    h = {k.lower(): v for k, v in headers.items()}
    acao = h.get("access-control-allow-origin", "")
    acac = h.get("access-control-allow-credentials", "").lower() == "true"
    lines = [f"GET {url} with Origin: {evil} -> HTTP {status}",
             f"ACAO: {acao or '(absent)'}",
             f"ACAC: {'true' if acac else 'false/absent'}"]
    if acao == evil and acac and creds_for_host(target, host):
        verdict = ("MISCONFIG: arbitrary origin reflected WITH credentials while a "
                   "session is saved — attacker site could read authed responses. HIGH interest.")
    elif acao == evil:
        verdict = ("reflects arbitrary origin without credentials — low impact alone; "
                   "re-test an authenticated endpoint with a session for the real verdict.")
    elif acao == "*":
        verdict = "wildcard ACAO (no credentials possible) — informational only."
    else:
        verdict = "origin not reflected — CORS looks restricted."
    lines.append(f"verdict: {verdict}")
    return f"cors-check {url}", "\n".join(lines)


_UPLOAD_MATRIX = [
    # (filename, content-type, content): all INERT canary text, no scripts.
    ("probe.jpg", "image/jpeg", "BBPOC_CANARY_{tag} baseline"),
    ("probe.php.jpg", "image/jpeg", "BBPOC_CANARY_{tag} double-ext"),
    ("probe.php", "application/x-php", "BBPOC_CANARY_{tag} <?php echo 'inert'; ?>"),
    ("probe.phtml", "application/x-php", "BBPOC_CANARY_{tag} inert"),
    ("probe.php5", "application/x-php", "BBPOC_CANARY_{tag} inert"),
    ("probe.PHP", "application/x-php", "BBPOC_CANARY_{tag} uppercase"),
    ("probe.jpg.php", "application/x-php", "BBPOC_CANARY_{tag} reversed-double-ext"),
    ("probe.svg", "image/svg+xml", "<svg><text>BBPOC_CANARY_{tag}</text></svg>"),
    ("probe.jpg", "image/jpeg", "BBPOC_CANARY_{tag} [content-marker test]"),
]

_LEAKED_PATH_RE = re.compile(
    r'["\']?((?:https?://[^\s"\']+|/(?:uploads|files|media|tmp|temp|attachments|images)[^\s"\']*))'
)


def _multipart(field, filename, content, ctype, boundary):
    part = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n{content}\r\n").encode()
    return part + f"--{boundary}--\r\n".encode()


def _run_upload_probe(args, target):
    """Upload-filter bypass detection: POSTs INERT canary files (plain text /
    inert SVG, zero scripts) under suspicious names, then checks whether the
    server stores and serves them back. Creates junk files on the target —
    use only where the program invites upload testing."""
    raw_url = (args.get("url") or "").strip()
    field = (args.get("param") or "file").strip()
    if not field.replace("_", "").replace("-", "").isalnum():
        raise ReconError(f"Invalid field name {field!r}.")
    url, host = _check_url(raw_url, target)
    insecure = getattr(target, "allow_insecure_tls", False)
    auth = _auth_headers(target, host)
    ep = urllib.parse.urlparse(url)
    ep_scheme, ep_netloc = ep.scheme, ep.netloc
    tag = f"{random.randint(0, 0xFFFFFF):06x}"
    canary = f"BBPOC_CANARY_{tag}"
    lines = [f"upload-probe on {url} (field={field}, canary={canary})",
             "content is INERT text — a stored+served file proves filter bypass, nothing executes."]
    best = "all suspicious names rejected — filter looks effective"
    for filename, ctype, template in _UPLOAD_MATRIX:
        content = template.format(tag=tag)
        boundary = f"----bbpoc{tag}"
        data = _multipart(field, filename, content, ctype, boundary)
        try:
            status, _, body = _http_post(
                url, data, f"multipart/form-data; boundary={boundary}", auth, insecure)
        except ReconError as exc:
            lines.append(f"{filename}: request failed ({exc})")
            continue
        snippet = body[:300].replace("\n", " ")
        if status in (200, 201):
            lines.append(f"{filename}: ACCEPTED (HTTP {status}) resp: {snippet}")
            served = _check_served(body, canary, auth, ep_scheme, ep_netloc, host, insecure)
            if served:
                best = (f"CANDIDATE: {filename} stored AND served back with canary "
                        f"intact at {served} — filter bypass PROVEN (content is inert, "
                        f"confirm manually whether script types execute server-side)")
                lines.append(best)
                break
            best = (f"SUSPICIOUS: {filename} accepted (HTTP {status}) but no retrievable "
                    f"file URL in response — verify manually whether it is stored/served")
        else:
            lines.append(f"{filename}: rejected (HTTP {status}) resp: {snippet}")
    lines.append(f"verdict: {best}")
    return f"upload-probe {field} on {url}", "\n".join(lines)


def _check_served(body, canary, auth, base_scheme, base_netloc, base_host, insecure=False):
    """Fetch leaked file URLs from an upload response; return the first
    same-host URL whose content contains our canary, else None. Cross-host
    links are never followed. base_netloc keeps the endpoint port."""
    for match in _LEAKED_PATH_RE.finditer(body or ""):
        link = match.group(1)
        parts = urllib.parse.urlparse(link)
        if parts.scheme and parts.hostname:
            try:
                if clean_host(parts.hostname) != base_host:
                    continue
            except ReconError:
                continue
            absolute = link
        elif link.startswith("/"):
            absolute = f"{base_scheme}://{base_netloc}{link}"
        else:
            continue
        try:
            status, _, content, _ = _http_get(absolute, auth, insecure)
        except ReconError:
            continue
        if status == 200 and canary in content:
            return absolute
    return None


def _run_nmap(args, target):
    preset = (args.get("preset") or "fast").strip()
    insecure = getattr(target, "allow_insecure_tls", False)
    command, output, truncated = run_preset(
        f"nmap-{preset}", target.domain, target.automation_level, insecure
    )
    if truncated:
        output += "\n[stored truncated]"
    return command, output


def _ct_subdomains(domain):
    """Passive discovery via crt.sh (certificate transparency logs)."""
    url = f"https://crt.sh/?q=%25.{domain}&output=json"
    req = urllib.request.Request(url, headers={"User-Agent": "Cydar/1.0 (authorized recon)"})
    try:
        with urllib.request.urlopen(req, timeout=_CT_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read(2_000_000).decode("utf-8", "replace"))
    except Exception as exc:
        return set(), f"crt.sh failed ({exc})"
    found = set()
    for entry in data if isinstance(data, list) else []:
        for name in str(entry.get("name_value", "")).splitlines():
            name = name.strip().lower().lstrip("*.")
            if name and (name == domain or name.endswith("." + domain)):
                found.add(name)
    return found, f"{len(found)} unique subdomains"


def _san_subdomains(domain, insecure=False):
    """Passive discovery via the target's own TLS certificate SAN list
    (stdlib getpeercert — no extra dependencies)."""
    found = set()
    try:
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((domain, 443), timeout=_SAN_TIMEOUT_SECONDS) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                cert = tls.getpeercert() or {}
    except Exception as exc:
        return found, f"TLS handshake failed ({exc}).{_tls_hint(exc, insecure)}"
    for kind, name in cert.get("subjectAltName", ()):
        if kind != "DNS":
            continue
        name = str(name).strip().lower().lstrip("*.")
        if name and (name == domain or name.endswith("." + domain)):
            found.add(name)
    return found, f"{len(found)} unique subdomains"


def _brute_subdomains(domain):
    """Active DNS brute force against a server-side subdomain wordlist."""
    wordlist = next((p for p in SUBDOMAIN_WORDLISTS if os.path.exists(p)), None)
    if wordlist is None:
        return set(), "no subdomain wordlist found on the server — skipped"
    with open(wordlist, errors="replace") as fh:
        names = [ln.strip().lower().lstrip("*." ) for ln in fh]
    names = [n for n in names if n and " " not in n][: _BRUTE_MAX_NAMES]

    def _resolves(candidate):
        try:
            socket.getaddrinfo(candidate, None, type=socket.SOCK_STREAM)
            return True
        except socket.gaierror:
            return False

    found = set()
    with ThreadPoolExecutor(max_workers=_BRUTE_WORKERS) as pool:
        for candidate, ok in zip(
            [f"{n}.{domain}" for n in names],
            pool.map(_resolves, [f"{n}.{domain}" for n in names], timeout=_BRUTE_TIMEOUT_SECONDS),
        ):
            if ok:
                found.add(candidate)
    return found, f"{len(found)} resolving subdomains (first {_BRUTE_MAX_NAMES} wordlist entries)"


def _run_subdomain_enum(args, target):
    domain = clean_host((args.get("domain") or target.domain))
    host_in_scope(domain, target)
    insecure = getattr(target, "allow_insecure_tls", False)
    lines = [f"subdomain-enum {domain} (results filtered to *.{domain})"]
    ct, ct_note = _ct_subdomains(domain)
    lines.append(f"[crt.sh] {ct_note}")
    san, san_note = _san_subdomains(domain, insecure)
    lines.append(f"[cert-SAN] {san_note}")
    brute, brute_note = _brute_subdomains(domain)
    lines.append(f"[dns-brute] {brute_note}")
    merged = sorted(ct | san | brute)
    if merged:
        lines.append(f"--- {len(merged)} unique in-scope subdomains ---")
        lines += merged
        lines.append("Follow up: header_audit + http_fetch each new host; check which serve distinct apps.")
    else:
        lines.append("No subdomains discovered — continue with the bare domain.")
    return f"subdomain-enum {domain}", "\n".join(lines)


_VHOST_MAX_NAMES = 3000
_VHOST_WORKERS = 20
_VHOST_PROBE_TIMEOUT = 10
_VHOST_TOTAL_TIMEOUT = 180


def _run_vhost_enum(args, target):
    """Virtual-host discovery via Host-header fuzzing (the ffuf step).

    Connects to the target's own web server (https first, then http) while
    sending Host: <word>.<domain> for each server-wordlist entry. Responses
    that differ from a random-host baseline are vhosts DNS/CT can never
    reveal (the THM/lab staple). Detection only — plain GETs, no payloads.
    """
    domain = clean_host((args.get("domain") or target.domain))
    host_in_scope(domain, target)
    insecure = getattr(target, "allow_insecure_tls", False)
    wordlist = next((p for p in SUBDOMAIN_WORDLISTS if os.path.exists(p)), None)
    if wordlist is None:
        raise ReconError("No subdomain wordlist found on the server — vhost fuzzing needs one.")
    with open(wordlist, errors="replace") as fh:
        names = [ln.strip().lower().lstrip("*.") for ln in fh]
    names = [n for n in names if n and " " not in n][:_VHOST_MAX_NAMES]

    def _probe(host_value, base_url):
        try:
            status, resp_headers, body, _ = _http_get(
                base_url,
                {"User-Agent": "Cydar/1.0 (authorized recon)", "Host": host_value},
                insecure, _VHOST_PROBE_TIMEOUT,
            )
        except ReconError:
            return None
        m = _TITLE_RE.search(body or "")
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:100] if m else ""
        return (status, len(body or ""), title, resp_headers.get("Location", ""))

    # Pick a working scheme: prefer https, fall back to http. The baseline
    # is a random nonexistent vhost — its response is the noise floor.
    baseline_host = f"zz-no-such-vhost-{random.randint(10 ** 6, 10 ** 7)}.{domain}"
    base_url = None
    baseline = None
    tls_hint = ""
    for scheme in ("https", "http"):
        candidate = f"{scheme}://{domain}/"
        try:
            baseline = _probe(baseline_host, candidate)
        except Exception:
            baseline = None
        if baseline is None and scheme == "https":
            tls_hint = ("https unreachable (connection failed or cert rejected). "
                        "Enable 'Allow insecure TLS' on the target for lab/self-signed hosts.")
            continue
        if baseline is not None:
            base_url = candidate
            break
    if base_url is None:
        raise ReconError(
            f"vhost-enum: cannot reach {domain} over https or http. " + tls_hint
        )
    lines = [f"vhost-enum {domain} via {base_url} "
             f"(baseline Host {baseline_host}: HTTP {baseline[0]}, "
             f"{baseline[1]} bytes, title {baseline[2]!r}; {len(names)} candidates)"]
    if tls_hint and base_url.startswith("http:"):
        lines.append(f"NOTE: {tls_hint}")

    def _check(name):
        cand = f"{name}.{domain}"
        res = _probe(cand, base_url)
        if res is None or res == baseline:
            return None
        return cand, res

    hits = []
    with ThreadPoolExecutor(max_workers=_VHOST_WORKERS) as pool:
        try:
            for out in pool.map(_check, names, timeout=_VHOST_TOTAL_TIMEOUT):
                if out:
                    hits.append(out)
        except Exception as exc:
            lines.append(f"(fuzzing stopped early: {exc})")

    if hits:
        lines.append(f"--- {len(hits)} vhost(s) differ from baseline (in scope: *.{domain}) ---")
        for cand, (status, size, title, loc) in hits[:50]:
            detail = f"{cand} -> HTTP {status}, {size} bytes"
            if title:
                detail += f", title: {title!r}"
            if loc:
                detail += f", Location: {loc}"
            lines.append(detail)
        if len(hits) > 50:
            lines.append(f"… +{len(hits) - 50} more (see stored output)")
        lines.append("Follow up: http_fetch each hit with host_header='<vhost>' "
                     "(works even with no DNS record), header_audit it, and pull "
                     "its cert SANs via subdomain_enum {\"domain\": \"<vhost>\"}.")
    else:
        lines.append("No vhost differences found — all candidates matched the baseline.")
    return f"vhost-enum {domain}", "\n".join(lines)


def _run_gobuster(args, target):
    mode = (args.get("mode") or "dir").strip().lower()
    if mode not in ("dir", "vhost"):
        raise ReconError(f"Unknown gobuster mode {mode!r} (use 'dir' or 'vhost').")
    insecure = getattr(target, "allow_insecure_tls", False)
    command, output, truncated = run_preset(
        f"gobuster-{'common' if mode == 'dir' else 'vhost'}",
        target.domain, target.automation_level, insecure,
    )
    if truncated:
        output += "\n[stored truncated]"
    return command, output


_CT_TIMEOUT_SECONDS = 20
_SAN_TIMEOUT_SECONDS = 15
_BRUTE_MAX_NAMES = 3000
_BRUTE_WORKERS = 50
_BRUTE_TIMEOUT_SECONDS = 180


_LOGIN_HINTS = ("login", "signin", "sign-in", "auth", "sso", "oauth", "challenge")

_REGISTER_LINK_RE = re.compile(
    r"""href\s*=\s*["']([^"']*(?:register|signup|sign-up|join|create-account)[^"']*)["']""",
    re.IGNORECASE,
)
_CAPTCHA_RE = re.compile(r"recaptcha|g-recaptcha|hcaptcha|turnstile|cf-challenge", re.IGNORECASE)
_PASSWORD_INPUT_RE = re.compile(r"""<input[^>]*type\s*=\s*["']?password""", re.IGNORECASE)
_WALL_TEXT_RE = re.compile(
    r"log in to continue|sign in to continue|please log in|login required|"
    r"authentication required|sign in required",
    re.IGNORECASE,
)


def _resolve_link(base, href):
    return urllib.parse.urljoin(base, (href or "").strip())


def _run_login_wall_probe(args, target):
    """Classify a URL: open vs login-walled (+registration discoverability).

    Detection only (one GET). When walled and no session is saved for the
    host, raises AuthNeeded so the run pauses for an operator decision
    (paste session / trial signup where allowed / skip host) — in auto
    mode too, instead of silently testing the login page as anonymous.
    """
    url, host = _check_url((args.get("url") or "").strip(), target)
    insecure = getattr(target, "allow_insecure_tls", False)
    has_session = bool(creds_for_host(target, host))
    status, headers, body, _ = _http_get(url, _auth_headers(target, host), insecure)
    location = headers.get("Location", "")
    low = body or ""
    reasons = []
    if status in (401, 403):
        reasons.append(f"HTTP {status}")
    if status in (301, 302, 303, 307, 308) and any(h in location.lower() for h in _LOGIN_HINTS):
        reasons.append(f"redirect to login ({location})")
    if _WALL_TEXT_RE.search(low):
        reasons.append("login-required text in body")
    regs = [m for m in _REGISTER_LINK_RE.findall(low) if m and not m.startswith(("javascript:", "#"))]
    reg_url = _resolve_link(url, regs[0]) if regs else ""
    captcha = bool(_CAPTCHA_RE.search(low))
    pw_form = bool(_PASSWORD_INPUT_RE.search(low))
    if not reasons:
        return (f"wall-probe {url}",
                f"GET {url}\nHTTP {status}\nverdict: OPEN — no login wall detected.")
    detail = (f"Login wall at {url} ({'; '.join(reasons)}). "
              f"Registration page: {reg_url or 'none found'}; "
              f"password form: {'yes' if pw_form else 'no'}; "
              f"CAPTCHA markers: {'yes' if captcha else 'no'}.")
    if has_session:
        raise AuthNeeded(
            f"{detail} The saved session for {host} was sent and the wall "
            f"stands — it is expired. Paste a fresh session or skip {host}.",
            host=host,
        )
    raise AuthNeeded(
        f"{detail} No session saved for {host}. Operator options: paste a "
        f"session (Edit target > Session auth), trial_signup where "
        f"self-registration is open, or skip {host}.",
        host=host,
    )


_FORM_RE = re.compile(r"<form([^>]*)>(.*?)</form>", re.IGNORECASE | re.DOTALL)
_INPUT_RE = re.compile(r"<input([^>]*)>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_VERIFY_POLL_SECONDS = 90
_DISPOSABLE_BLOCK_RE = re.compile(
    r"disposable|temporary e-?mail|temp-?mail|throwaway|blocked domain|"
    r"invalid e-?mail|email not allowed|use a (different|real|valid) email",
    re.IGNORECASE,
)
_VERIFY_OK_RE = re.compile(
    r"verif|check your (inbox|email)|confirm|activation|almost (done|there)",
    re.IGNORECASE,
)


def _parse_forms(html):
    """Extract (action, method, inputs) per form. inputs = list of dicts
    with lowercased attr names."""
    forms = []
    for m in _FORM_RE.finditer(html or ""):
        attrs = {k.lower(): (v1 or v2 or v3 or "")
                 for k, v1, v2, v3 in _ATTR_RE.findall(m.group(1))}
        inputs = []
        for im in _INPUT_RE.finditer(m.group(2)):
            iattrs = {k.lower(): (v1 or v2 or v3 or "")
                      for k, v1, v2, v3 in _ATTR_RE.findall(im.group(1))}
            inputs.append(iattrs)
        forms.append({"action": attrs.get("action", ""), "method": attrs.get("method", "get"),
                      "inputs": inputs})
    return forms


def _pick_signup_form(forms):
    """Score forms: email-ish + password fields wins. Returns form or None."""
    best, best_score = None, 0
    for f in forms:
        names = " ".join(i.get("name", "") + " " + i.get("type", "") + " " + i.get("id", "")
                         for i in f["inputs"]).lower()
        has_email = ("type=email" in names.replace(" ", "=") or "email" in names
                     or "login" in names or "username" in names)
        has_pw = any(i.get("type", "").lower() == "password" for i in f["inputs"])
        score = (2 if has_email else 0) + (3 if has_pw else 0)
        if score > best_score:
            best, best_score = f, score
    return best if best_score >= 5 else None


def _run_trial_signup(args, target):
    """Self-registration with the operator's mailbox (opt-in, aggressive).

    Discovers the registration form, creates USER+tag@mailbox, submits,
    polls the operator inbox for the verification mail, follows the link,
    and saves the captured session as the host's AuthCredential slot.
    Aborts (failed tool result, never silent) on CAPTCHA, disposable-mail
    blocks, SSO-only forms, missing mailbox config, or no captured session
    (account-verified-but-sessionless raises AuthNeeded so the operator can
    paste it). Secrets (site password, tokens, mail bodies) never enter the
    output — only the account id, host, and slot type are reported.
    """
    from targets.models import AuthCredential

    if not getattr(target, "allow_self_registration", False):
        raise ReconError(
            "Target has not opted into self-registration (Edit target > "
            "check 'Allow trial signups'). Refusing to create accounts."
        )
    st = mailbox_status()
    if not st["configured"]:
        raise ReconError(
            "Signup mailbox not configured — set SIGNUP_IMAP_HOST/USER/PASS in .env."
        )
    url, host = _check_url((args.get("url") or "").strip(), target)
    insecure = getattr(target, "allow_insecure_tls", False)
    status, _, body, _ = _http_get(url, _auth_headers(target, host), insecure)
    form = _pick_signup_form(_parse_forms(body))
    if form is None:
        regs = [m for m in _REGISTER_LINK_RE.findall(body or "")
                if m and not m.startswith(("javascript:", "#"))]
        if regs:
            reg_url, _ = _check_url(_resolve_link(url, regs[0]), target)
            _, _, body2, _ = _http_get(reg_url, _auth_headers(target, host), insecure)
            body = body2
            url = reg_url
            form = _pick_signup_form(_parse_forms(body2))
    if form is None:
        raise ReconError(
            f"No email+password registration form at {url} — SSO-only or "
            f"login-only page. Operator must supply a session or skip {host}."
        )
    if _CAPTCHA_RE.search(body or ""):
        raise ReconError(
            f"Registration at {url} is CAPTCHA-guarded — automated signup "
            f"refused. Operator must register manually and paste the session."
        )
    mailbox_user = (os.environ.get("SIGNUP_IMAP_USER") or "").strip()
    local, _, domain = mailbox_user.partition("@")
    tag = f"bb{secrets.token_hex(3)}"
    addr = f"{local}+{tag}@{domain}" if domain else mailbox_user
    password = secrets.token_urlsafe(12)
    fields = {}
    for i in form["inputs"]:
        n = (i.get("name") or "").strip()
        if not n or i.get("type", "").lower() in ("submit", "button", "checkbox", "radio"):
            continue
        t = i.get("type", "").lower()
        nl = n.lower()
        if t == "hidden":
            fields[n] = i.get("value", "")
        elif t == "email" or "email" in nl:
            fields[n] = addr
        elif t == "password":
            fields[n] = password
        elif "user" in nl or "login" in nl or "name" in nl:
            fields[n] = addr
    if not any(v == addr for v in fields.values()):
        raise ReconError(
            f"Registration form at {url} has no email/username field — cannot sign up."
        )
    action = _resolve_link(url, form["action"] or url)
    try:
        post_url, _ = _check_url(action, target)
    except ReconError:
        post_url = action
    data = urllib.parse.urlencode(fields).encode()
    try:
        pstatus, pheaders, pbody = _http_post(
            post_url, data, "application/x-www-form-urlencoded",
            {"User-Agent": "Cydar/1.0 (authorized recon)", "Referer": url}, insecure)
    except ReconError as exc:
        raise ReconError(f"Signup POST failed ({exc}).")
    plow = (pbody or "")[:4000]
    if _DISPOSABLE_BLOCK_RE.search(plow):
        raise ReconError(
            f"Target rejected the signup address as disposable/temporary "
            f"({post_url} HTTP {pstatus}). Use a non-disposable mailbox or "
            f"paste a session manually."
        )
    if _CAPTCHA_RE.search(plow):
        raise ReconError("Signup blocked by CAPTCHA at submit — register manually.")
    if pstatus == 429:
        raise ReconError("Signup rate-limited (HTTP 429) — try later or register manually.")
    from .mailbox import poll_verification_link

    try:
        link, sender = poll_verification_link(addr, sender_hint=host, timeout=_VERIFY_POLL_SECONDS)
    except MailboxError as exc:
        raise ReconError(
            f"Account {addr} submitted (HTTP {pstatus}) but {exc} "
            f"Check the inbox manually; if verified, paste the session."
        )
    try:
        link_url, link_host = _check_url(link, target)
    except ReconError:
        raise ReconError(
            f"Verification link points out of scope ({link.split('?')[0]}). "
            f"Account {addr} may be verified — confirm manually, then paste the session."
        )
    vstatus, vheaders, vbody = _http_get(link_url, _auth_headers(target, host), insecure)
    slot = None
    set_cookie = vheaders.get("Set-Cookie") or ""
    if set_cookie:
        pair = set_cookie.split(";")[0]  # name=value (Expires commas live after ;)
        cname, _, cval = pair.partition("=")
        if cname.strip() and len(cval.strip()) >= 4:
            slot = ("cookie", cname.strip(), cval.strip())
    if slot is None:
        m = re.search(r"""["'](?:token|jwt|access_token|session)["']\s*:\s*["']([^"']{8,})["']""",
                      vbody or "")
        if m:
            slot = ("bearer", "", m.group(1))
    if slot is None:
        raise AuthNeeded(
            f"Account {addr} created and verified on {host}, but no session "
            f"auto-captured (HTTP {vstatus}). Paste it (Edit target > Session "
            f"auth), then approve to continue.",
            host=host,
        )
    AuthCredential.objects.filter(target=target, host_pattern=host).delete()
    AuthCredential.objects.create(
        target=target, host_pattern=host, auth_type=slot[0], auth_name=slot[1],
        auth_value=slot[2], site_username=addr, site_password=password,
        check_url=url,
    )
    return (f"trial-signup {host}",
            f"Account {addr} registered + verified on {host} (HTTP {vstatus}). "
            f"Session saved as {slot[0]} slot for {host} (value hidden). "
            f"Continue testing authenticated surfaces with auth_check.")


def _check_url_for_host(target, host):
    """Find the check URL saved for `host`: per-host slot first, legacy
    single slot when its URL is on the same host. Returns url or ''."""
    try:
        slots = list(target.auth_credentials.all())
    except AttributeError:
        slots = []
    for s in slots:
        if s.check_url and _match_host(host, s.host_pattern):
            return s.check_url
    legacy = (getattr(target, "auth_check_url", "") or "").strip()
    if legacy:
        try:
            parts = urllib.parse.urlparse(legacy)
            if parts.hostname and clean_host(parts.hostname) == host:
                return legacy
        except ReconError:
            pass
    return ""


def _run_auth_check(args, target):
    """Validate a saved session: GET the check URL with that host's saved
    credentials. Optional `host` arg picks the per-host session; without it
    the legacy single slot is used. Raises AuthExpired when rejected."""
    host_arg = ((args.get("host") or "").strip() if isinstance(args, dict) else "")
    if host_arg:
        host = clean_host(host_arg)
        host_in_scope(host, target)
        if creds_for_host(target, host) is None:
            return "auth-check", (
                f"No session saved for {host}. Ask the operator to log in "
                f"in their browser and paste it (Edit target > Session auth), "
                f"try trial_signup where self-registration is allowed, or "
                f"treat login-walled areas as leads."
            )
        check_url = _check_url_for_host(target, host)
        if not check_url:
            raise ReconError(
                f"No check URL saved for {host} — the operator must set one "
                f"(a URL that requires login, e.g. https://{host}/dashboard)."
            )
    else:
        if not target.auth_configured:
            return "auth-check", (
                "No session credentials saved on this target. Authenticated areas "
                "cannot be tested — ask the operator to log in in their browser "
                "and paste the session (Edit target > Auth), then call auth_check again."
            )
        check_url = (target.auth_check_url or "").strip()
        if not check_url:
            raise ReconError(
                "auth_check_url is not set on the target — the operator must set it "
                "to a URL that requires login (e.g. https://host/dashboard)."
            )
    insecure = getattr(target, "allow_insecure_tls", False)
    url, host = _check_url(check_url, target)
    status, headers, body, _ = _http_get(url, _auth_headers(target, host), insecure)
    location = headers.get("Location", "")
    if status == 200:
        return f"auth-check {url}", f"GET {url}\nHTTP 200\nverdict: VALID — session accepted."
    if status in (401, 403):
        raise AuthExpired(
            f"Session rejected by {url} (HTTP {status}). Ask the operator for a fresh token."
        )
    if status in (301, 302, 303, 307, 308) and any(h in location.lower() for h in _LOGIN_HINTS):
        raise AuthExpired(
            f"Session redirected to login ({location}). Ask the operator for a fresh token."
        )
    return (
        f"auth-check {url}",
        f"GET {url}\nHTTP {status}\nLocation: {location or '(none)'}\n"
        f"verdict: UNCLEAR — not an obvious accept or reject. Treat authed results with suspicion.",
    )


TOOLS = {
    "nmap": {
        "description": "Port/service scan of the target domain. presets: fast (top 100 ports), services (versions, top 1000), scripts (default scripts + versions).",
        "args": {"preset": "'fast' | 'services' | 'scripts'"},
        "min_level": "read-only",
        "run": _run_nmap,
    },
    "gobuster": {
        "description": "Directory OR vhost enumeration of https://<target domain> with a server wordlist (~1-3 min). mode 'dir' finds paths; mode 'vhost' finds subdomains via Host-header fuzzing. Summarize-then-follow-up: interesting hits MUST be followed with fetch_many/header_audit.",
        "args": {"mode": "'dir' (default) | 'vhost'"},
        "min_level": "read-only",
        "run": _run_gobuster,
    },
    "subdomain_enum": {
        "description": "Discover subdomains of the target via crt.sh + TLS cert SANs + DNS brute force (server wordlist, first 3000 names). Results are filtered to the target domain. Run FIRST, before gobuster.",
        "args": {"domain": "(optional, defaults to target domain)"},
        "min_level": "read-only",
        "run": _run_subdomain_enum,
    },
    "vhost_enum": {
        "description": "Virtual-host discovery via Host-header fuzzing (the ffuf step): sends Host: <word>.<domain> to the target's own web server and reports responses differing from a random-host baseline. Finds vhosts DNS/CT can never see — run EARLY on lab targets. Follow EVERY hit with http_fetch (host_header) + header_audit + subdomain_enum for its cert SANs.",
        "args": {"domain": "(optional, defaults to target domain)"},
        "min_level": "read-only",
        "run": _run_vhost_enum,
    },
    "fetch_many": {
        "description": "Batch-fetch up to 10 in-scope URLs in ONE call: returns a compact status/content-type/size/title/Location table. Use it to triage gobuster hits instead of N separate http_fetch calls.",
        "args": {"urls": ["https://host/path", "…"]},
        "min_level": "read-only",
        "run": _run_fetch_many,
    },
    "http_fetch": {
        "description": "GET a URL on an in-scope host; returns status, headers, truncated body. Sends the saved session automatically when configured. Optional host_header requests a virtual host through the connection host (curl -H 'Host:' style) — use it when the vhost has no DNS record.",
        "args": {"url": "https://host/path", "host_header": "(optional) vhost, e.g. blog.example.com"},
        "min_level": "read-only",
        "run": _run_http_fetch,
    },
    "dns_lookup": {
        "description": "DNS records for an in-scope host. types: A, AAAA, MX, TXT, CNAME, NS.",
        "args": {"host": "sub.example.com", "type": "A"},
        "min_level": "read-only",
        "run": _run_dns_lookup,
    },
    "auth_check": {
        "description": "Validate a saved session against its check URL. Optional host picks the per-host session (else the legacy single slot). Call before relying on authenticated results.",
        "args": {"host": "(optional) sub.example.com"},
        "min_level": "read-only",
        "run": _run_auth_check,
    },
    "header_audit": {
        "description": "Check an in-scope URL for missing security headers (HSTS/CSP/X-Frame-Options/cookies). Detection only. Optional host_header audits a virtual host through the connection host (curl -H 'Host:' style).",
        "args": {"url": "https://host/", "host_header": "(optional) vhost, e.g. blog.example.com"},
        "min_level": "standard",
        "run": _run_header_audit,
    },
    "reflection_test": {
        "description": "Benign canary reflection probe on one query param (XSS candidate detection only, no payload execution).",
        "args": {"url": "https://host/search", "param": "q"},
        "min_level": "standard",
        "run": _run_reflection_test,
    },
    "open_redirect_check": {
        "description": "Test whether a query param makes the target redirect to an external URL (redirect is observed, never followed).",
        "args": {"url": "https://host/login", "param": "next"},
        "min_level": "standard",
        "run": _run_open_redirect_check,
    },
    "sqli_probe": {
        "description": "Error-based SQLi detection: quote chars on one param, diff for DB error signatures. Read-only GETs.",
        "args": {"url": "https://host/item", "param": "id"},
        "min_level": "standard",
        "run": _run_sqli_error_probe,
    },
    "cors_check": {
        "description": "CORS misconfig detection: evil Origin (+session when saved), inspect ACAO/ACAC reflection.",
        "args": {"url": "https://host/api/"},
        "min_level": "standard",
        "run": _run_cors_check,
    },
    "upload_probe": {
        "description": "Upload-filter bypass detection: INERT canary files under suspicious names, checks stored+served. CREATES junk files — only where the program invites upload testing.",
        "args": {"url": "https://host/upload", "param": "file"},
        "min_level": "standard",
        "run": _run_upload_probe,
    },
    "login_wall_probe": {
        "description": "Classify a URL: open vs login-walled (+registration page, CAPTCHA markers). Walled with no saved session for that host PAUSES the run for an operator decision (paste session / trial signup / skip) — in auto mode too.",
        "args": {"url": "https://host/walled-page"},
        "min_level": "read-only",
        "run": _run_login_wall_probe,
    },
    "trial_signup": {
        "description": "Self-registration with the operator's mailbox: discovers the form, creates a throwaway account, verifies via inbox, saves the session. Needs target opt-in (Allow trial signups) + aggressive level + mailbox in .env. Refuses on CAPTCHA/SSO-only/disposable-blocks.",
        "args": {"url": "https://host/register"},
        "min_level": "aggressive",
        "run": _run_trial_signup,
    },
}

_LEVEL_RANK = {"read-only": 0, "standard": 1, "aggressive": 2}


def allowed_tools(automation_level):
    """Tool names usable at this automation level, in registry order."""
    rank = _LEVEL_RANK.get(automation_level, 1)
    return [n for n, s in TOOLS.items() if _LEVEL_RANK[s["min_level"]] <= rank]


def describe(automation_level):
    """Prompt block listing usable tools + JSON arg shapes."""
    lines = []
    for name in allowed_tools(automation_level):
        spec = TOOLS[name]
        shape = json.dumps(spec["args"]) if spec["args"] else "(no args)"
        lines.append(f"- {name}{shape}: {spec['description']}")
    return "\n".join(lines)


def validate_args(name, args, target):
    """Pre-approval arg check (runs at proposal time, before the operator is
    asked). Raises ReconError with the correct shape when a required arg is
    missing, malformed, or out of scope — so broken proposals are refused
    without spending an approval, and the model learns the shape to fix."""
    spec = TOOLS.get(name)
    if spec is None:
        raise ReconError(
            f"Unknown tool {name!r}. Available: {allowed_tools(target.automation_level)}."
        )
    if not isinstance(args, dict):
        raise ReconError(f"Tool args must be a JSON object, got {args!r}.")
    shape = json.dumps(spec["args"]) if spec["args"] else "(no args)"

    def need(key):
        val = args.get(key)
        if not (isinstance(val, str) and val.strip()):
            raise ReconError(
                f"Tool {name!r} needs {key!r}, e.g. {name}{shape}. "
                f"Got args {args!r}."
            )
        return val.strip()

    if name in ("http_fetch", "header_audit", "reflection_test",
                "open_redirect_check", "sqli_probe", "cors_check",
                "upload_probe", "login_wall_probe", "trial_signup"):
        # Full URL validation (scheme + scope) up front — a doomed call
        # never reaches the approval screen.
        _check_url(need("url"), target)
        if name in ("http_fetch", "header_audit"):
            hh = ((args.get("host_header") or "").strip()
                  if isinstance(args.get("host_header"), str) else "")
            if hh:
                host_in_scope(clean_host(hh), target)
    elif name == "dns_lookup":
        host = clean_host(need("host"))
        host_in_scope(host, target)
    elif name == "auth_check":
        host_arg = ((args.get("host") or "").strip()
                    if isinstance(args.get("host"), str) else "")
        if host_arg:
            host_in_scope(clean_host(host_arg), target)
    elif name in ("subdomain_enum", "vhost_enum"):
        dom = (args.get("domain") or "").strip()
        if dom:
            dom = clean_host(dom)
            host_in_scope(dom, target)
    elif name == "fetch_many":
        urls = args.get("urls", [])
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, list) or not urls:
            raise ReconError(
                f"Tool 'fetch_many' needs 'urls': a non-empty list of URLs "
                f"(max {_FETCH_MANY_MAX_URLS}). Got args {args!r}."
            )
        if len(urls) > _FETCH_MANY_MAX_URLS:
            raise ReconError(
                f"Tool 'fetch_many' takes at most {_FETCH_MANY_MAX_URLS} "
                f"URLs per call (got {len(urls)})."
            )
        # Per-URL scope is intentionally NOT checked here — the tool skips
        # refused URLs individually and reports the rest.
    elif name == "nmap":
        preset = ((args.get("preset") or "fast").strip()
                  if isinstance(args.get("preset"), str) else "fast")
        if preset not in NMAP_PROFILES:
            raise ReconError(
                f"Unknown nmap preset {preset!r}. Valid: "
                f"{sorted(NMAP_PROFILES)}."
            )
    elif name == "gobuster":
        mode = ((args.get("mode") or "dir").strip().lower()
                if isinstance(args.get("mode"), str) else "dir")
        if mode not in ("dir", "vhost"):
            raise ReconError(
                f"Unknown gobuster mode {mode!r} (use 'dir' or 'vhost')."
            )
    return True


def execute(name, args, target):
    """Validate level + run. Raises ReconError on any refusal/failure."""
    spec = TOOLS.get(name)
    if spec is None:
        raise ReconError(
            f"Unknown tool {name!r}. Available: {allowed_tools(target.automation_level)}."
        )
    rank = _LEVEL_RANK.get(target.automation_level, 1)
    if rank < _LEVEL_RANK[spec["min_level"]]:
        raise ReconError(
            f"Tool {name!r} needs automation level '{spec['min_level']}' "
            f"(target is '{target.automation_level}')."
        )
    if not isinstance(args, dict):
        raise ReconError(f"Tool args must be a JSON object, got {args!r}.")
    return spec["run"](args, target)


def dry_command(name, args, target):
    """Preview the command without running (nmap/gobuster argv, else a
    one-line summary). Used for the semi-auto approval screen."""
    if name == "nmap":
        preset = (args.get("preset") or "fast").strip()
        flags = {"fast": "-T4 -F", "services": "-T4 -sV --top-ports 1000",
                 "scripts": "-T4 -sV -sC --top-ports 1000"}.get(preset, "?")
        return f"nmap {flags} {target.domain}"
    if name == "gobuster":
        mode = ((args.get("mode") or "dir") if isinstance(args, dict) else "dir")
        if mode == "vhost":
            return f"gobuster vhost -u https://{target.domain} --append-domain -w <server wordlist>"
        return f"gobuster dir -u https://{target.domain} -w <server wordlist>"
    if name == "subdomain_enum":
        return f"subdomain-enum {(args.get('domain') or target.domain) if isinstance(args, dict) else target.domain} (crt.sh + cert SAN + DNS brute)"
    if name == "vhost_enum":
        return f"vhost-enum {(args.get('domain') or target.domain) if isinstance(args, dict) else target.domain} (Host-header fuzz vs random-host baseline)"
    if name in ("http_fetch", "header_audit"):
        hh = (args.get("host_header", "") or "") if isinstance(args, dict) else ""
        suffix = f" [Host: {hh}]" if hh else ""
        return f"GET {args.get('url', '')} (+saved session){suffix}"
    if name == "login_wall_probe":
        return f"wall-probe: GET {args.get('url', '')} (pauses for operator if walled)"
    if name == "trial_signup":
        return f"trial-signup on {args.get('url', '')} (creates account via operator mailbox)"
    if name == "fetch_many":
        urls = args.get("urls", []) if isinstance(args, dict) else []
        n = len(urls) if isinstance(urls, list) else 1
        return f"fetch-many ({n} urls, +saved session)"
    if name == "auth_check":
        return "auth-check <target check URL>"
    if name == "dns_lookup":
        return f"dns {args.get('type', 'A')} {args.get('host', '')}"
    if name == "reflection_test":
        return f"canary-reflection probe: {args.get('url', '')}?{args.get('param', 'q')}=<canary>"
    if name == "open_redirect_check":
        return f"redirect probe: {args.get('url', '')}?{args.get('param', 'next')}=<sink>"
    if name == "sqli_probe":
        return f"sqli-probe: {args.get('url', '')}?{args.get('param', 'id')}=1[quote]"
    if name == "cors_check":
        return f"cors-check: GET {args.get('url', '')} with evil Origin"
    if name == "upload_probe":
        return f"upload-probe: {args.get('param', 'file')} on {args.get('url', '')} (9 inert canary files)"
    return f"{name} {json.dumps(args)}"
