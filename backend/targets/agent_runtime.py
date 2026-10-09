"""Live runtime for autonomous agent sessions.

One Session per AgentRun lives here: a worker thread drives the AgentLoop
(model -> proposal -> [approval] -> tool -> result …), streaming event dicts
into a Queue that the SSE view consumes. Semi mode pauses at every proposal
until the approve view posts a decision (+ optional operator hint); auto
mode executes immediately.

Threading notes: CPython threads are fine (LLM/tool calls are I/O-bound).
Each thread closes its DB connections on exit; all ORM writes go through
short helper functions. A runaway browser disconnect doesn't kill the run —
events simply pile up (bounded) until the viewer reconnects or it finishes.
"""

import json
import queue
import re
import threading

from django.db import close_old_connections
from django.utils import timezone as dj_timezone

from agent.client import AgentAPIError
from agent.loop import (
    MAX_PARSE_RETRIES,
    AgentLoop,
    LoopError,
    looks_like_unfenced_report,
    parse_reply,
    render_report_markdown,
    validate_report,
)
from agent.providers import ProviderError
from recon.runner import ReconError
from recon.tools import AuthExpired, AuthNeeded, allowed_tools, redact

from .models import AgentAction, AgentRun, Finding, Report, Target

ACTION_OUTPUT_CHARS = 8000
APPROVAL_POLL_SECONDS = 0.5

_registry = {}
_registry_lock = threading.Lock()


def get_session(run_id):
    with _registry_lock:
        return _registry.get(run_id)


def _emit(session, event):
    try:
        session["events"].put_nowait(event)
    except queue.Full:
        pass  # viewer is behind; DB rows remain the source of truth


def _secret(session):
    loop = session.get("loop")
    target = getattr(loop, "target", None)
    return getattr(target, "auth_value", "") or ""


def _action(session, run, **fields):
    fields.setdefault("seq", session["seq"])
    session["seq"] += 1
    # Live secrets must never reach the DB, the event stream, or the model.
    secret = _secret(session)
    if fields.get("tool"):
        # AgentAction.tool is a short label — truncate model-invented names
        # instead of crashing on max_length.
        fields["tool"] = str(fields["tool"])[:30]
    if fields.get("output"):
        fields["output"] = redact(fields["output"], secret)
    if fields.get("error"):
        fields["error"] = redact(fields["error"], secret)
    action = AgentAction.objects.create(agent_run=run, **fields)
    run.iterations = session["loop"].iterations
    run.save(update_fields=["iterations"])
    return action


def _set_pending(run, payload):
    run.pending_action = json.dumps(payload) if payload else ""
    run.save(update_fields=["pending_action"])


def _finish(session, run, status, error=""):
    run.status = status
    run.error = error
    run.iterations = session["loop"].iterations
    run.pending_action = ""
    run.save(update_fields=["status", "error", "iterations", "pending_action"])
    target = run.target
    target.status = Target.STATUS_DONE if status == AgentRun.STATUS_OK else Target.STATUS_QUEUED
    target.save(update_fields=["status", "updated_at"])
    session["done"] = True
    with _registry_lock:
        _registry.pop(run.id, None)
    _emit(session, {"type": "done", "run_id": run.id, "status": status})


REAP_GRACE_SECONDS = 120


def reconcile_running():
    """Reap DB-'running' agent runs whose worker thread is gone.

    Workers live only in this process: a server restart/reload or a crashed
    thread orphans the row at status='running' forever, which wedges the
    target pill on "Running" and spams every target page with phantom "AI
    already running" banners that can never be stopped (stop/approve 409
    once the session object is gone). Runs younger than the grace window
    are left alone — the worker may just be starting.
    Returns the set of run ids with a genuinely live worker.
    """
    now = dj_timezone.now()
    with _registry_lock:
        reg = dict(_registry)
    alive = set()
    stale = []
    running = AgentRun.objects.filter(
        kind=AgentRun.KIND_AGENT, status=AgentRun.STATUS_RUNNING
    ).select_related("target")
    for run in running:
        if (now - run.created_at).total_seconds() < REAP_GRACE_SECONDS:
            alive.add(run.id)
            continue
        thread = (reg.get(run.id) or {}).get("thread")
        if thread is not None and thread.is_alive():
            alive.add(run.id)
        else:
            stale.append(run)
    stale_ids = [r.id for r in stale]
    for run in stale:
        run.status = AgentRun.STATUS_STOPPED
        if not run.error:
            run.error = "Worker exited without finishing (server restart or crash)."
        run.pending_action = ""
        run.save(update_fields=["status", "error", "pending_action"])
        other_live = AgentRun.objects.filter(
            target_id=run.target_id, kind=AgentRun.KIND_AGENT,
            status=AgentRun.STATUS_RUNNING,
        ).exclude(id__in=stale_ids).exists()
        if not other_live:
            Target.objects.filter(id=run.target_id).update(status=Target.STATUS_QUEUED)
        with _registry_lock:
            _registry.pop(run.id, None)
    return alive


def _wait_approval(session, run, proposal):
    """Block until the approve view posts {decision, hint} or stop arrives."""
    _set_pending(run, proposal)
    session["decided"] = False
    _emit(session, {"type": "pending", **proposal})
    while True:
        if session["stop"].is_set():
            return None
        try:
            decision = session["approvals"].get(timeout=APPROVAL_POLL_SECONDS)
            session["decided"] = False  # consumed — a new decision may be posted
            return decision
        except queue.Empty:
            continue


def _execute_tool(session, run, loop, thought, name, args, preview):
    """Run the tool, save the action, feed the result back. Returns output.

    AuthExpired/AuthNeeded propagate to the caller (they need session-level
    handling); all other ReconErrors are recorded inline as failed tool
    results (and DNS deaths are memorized so the host is never retried).
    """
    try:
        command, output = loop.run_tool(name, args)
    except (AuthExpired, AuthNeeded):
        raise
    except ReconError as exc:
        loop.note_tool_failure(name, args, str(exc))
        _action(session, run, thought=thought, tool=name,
                args_json=json.dumps(args), command=preview,
                status=AgentAction.STATUS_ERROR, error=str(exc))
        loop.tool_result_note(name, f"TOOL REFUSED/FAILED: {exc}")
        _emit(session, {"type": "tool_result", "tool": name, "ok": False,
                        "error": str(exc)})
        return None
    output = redact(output, _secret(session))
    stored = output if len(output) <= ACTION_OUTPUT_CHARS else (
        output[:ACTION_OUTPUT_CHARS] + "\n…[stored truncated]")
    _action(session, run, thought=thought, tool=name,
            args_json=json.dumps(args), command=command, output=stored)
    loop.tool_result_note(name, output)
    _emit(session, {"type": "tool_result", "tool": name, "ok": True,
                    "command": command, "output": stored})
    return output


def _auth_wall_flow(session, run, loop, name, args, preview, exc):
    """Login wall or expired session: log it, then PAUSE (semi and auto)
    for an operator decision scoped to the wall host — Approve (after
    pasting a fresh session), or Skip (remembered for this session, so
    employee-only logins are asked about once, never nagged).
    Returns "stopped" if the run was halted while waiting, else "continue".
    """
    host = getattr(exc, "host", "") or ""
    _action(session, run, thought="", tool=name,
            args_json=json.dumps(args), command=preview,
            status=AgentAction.STATUS_ERROR, error=str(exc))
    if host and host in session.get("skipped_hosts", set()):
        # Already skipped this session — don't pause again, just route around.
        loop.note(f"OPERATOR skipped auth for {host} earlier — continue "
                  f"unauthenticated there; list its login-walled areas as leads.")
        _emit(session, {"type": "auth_needed", "tool": name, "host": host,
                        "detail": str(exc), "skipped": True})
        return "continue"
    _emit(session, {"type": "auth_needed", "tool": name, "host": host, "detail": str(exc)})
    session["awaiting"] = "auth"
    session["auth_host"] = host
    decision = _wait_approval(
        session, run,
        {"kind": "auth", "tool": name, "host": host, "detail": str(exc),
         "thought": (f"Login wall on {host or 'the target'}. Paste a session via "
                     "Edit target > Session auth, then Approve — or Skip to "
                     "continue unauthenticated on this host (employee-only "
                     "logins should always be skipped).")},
    )
    session["awaiting"] = None
    session.pop("auth_host", None)
    _set_pending(run, None)
    if decision is None:  # stopped while waiting
        _finish(session, run, AgentRun.STATUS_STOPPED, "Stopped by operator.")
        return "stopped"
    hint = redact((decision.get("hint") or "").strip(), _secret(session))
    if decision.get("decision") in ("reject", "skip"):
        if host:
            session.setdefault("skipped_hosts", set()).add(host)
        loop.note(f"OPERATOR skipped auth for {host or 'the target'}."
                  + (f" Guidance: {hint}" if hint else "") +
                  " Continue unauthenticated there: do not call auth_check "
                  "for it again, do not claim authed findings — list "
                  "login-walled areas as leads.")
        _emit(session, {"type": "rejected", "tool": name, "hint": hint})
        return "continue"
    _emit(session, {"type": "approved", "tool": name, "hint": hint})
    if hint:
        _action(session, run, thought="", tool="", command="",
                output=hint, status=AgentAction.STATUS_NOTE)
        loop.note(f"OPERATOR GUIDANCE (take into account next): {hint}")
    loop.note(
        "Operator approved continuing — freshly saved sessions (if any) are "
        "picked up automatically since the target is re-read every step. "
        "Re-run auth_check for the host before trusting authenticated results."
    )
    return "continue"


_REPORT_SKELETON = (
    "Re-emit the COMPLETE report JSON (all keys, even when a list is empty):\n"
    "```report\n"
    '{"title": "...", "executive_summary": "...", "methodology": "...", '
    '"scope_notes": "... (never empty — name untested/blocked areas)", '
    '"confirmed": [{"title": "...", "severity": "low", "description": "...", '
    '"steps_to_reproduce": ["1. ..."], "evidence": "<paste tool output>", '
    '"impact": "...", "remediation": "..."}], '
    '"leads": [{"title": "...", "suspicion": "...", "why_suspicious": "...", '
    '"how_to_verify": ["1. ..."], "confidence": "low"}]}\n'
    "```"
)


_GROUND_WINDOW = 40


def _session_corpus(run):
    """Whitespace-normalized blob of everything this session's tools
    returned (outputs, errors, executed commands) — the ground truth that
    confirmed evidence must be quoted from."""
    parts = []
    for a in AgentAction.objects.filter(agent_run=run).only("output", "error", "command"):
        parts += [a.output or "", a.error or "", a.command or ""]
    return re.sub(r"\s+", " ", "\n".join(parts)).strip()


def _evidence_grounded(evidence, corpus):
    """True when a 40+ char verbatim chunk of the evidence occurs in the
    session's recorded tool output. Paraphrase ("http_fetch showed…")
    never matches; paste does."""
    ev = re.sub(r"\s+", " ", evidence or "").strip()
    if len(ev) < _GROUND_WINDOW or not corpus:
        return False
    return any(ev[i:i + _GROUND_WINDOW] in corpus
               for i in range(len(ev) - _GROUND_WINDOW + 1))


def _finalize_report(session, run, loop, thought, data, require_work=True):
    """Validate, save Report + Findings, emit. Returns True on success."""
    try:
        report = validate_report(data)
    except LoopError as exc:
        loop.note(f"REPORT REJECTED: {exc}\n{_REPORT_SKELETON}")
        _emit(session, {"type": "report_rejected", "detail": str(exc)})
        return False
    if report["confirmed"] and not loop.tool_calls:
        # Proven findings with zero executed tools = narrated, not tested.
        # (Empty confirmed + honest scope_notes stays legal: "nothing was
        # testable" is a valid outcome.)
        detail = ("0 tools ran this session, yet the report lists confirmed "
                  "findings. Run tools first (subdomain_enum, then follow the "
                  "methodology) — or empty `confirmed` and explain in "
                  "`scope_notes` why nothing was testable.")
        loop.note(f"REPORT REJECTED: {detail}")
        _emit(session, {"type": "report_rejected", "detail": detail})
        return False
    if report["confirmed"]:
        corpus = _session_corpus(run)
        ungrounded = [f["title"] for f in report["confirmed"]
                      if not _evidence_grounded(f.get("evidence", ""), corpus)]
        if ungrounded:
            detail = ("evidence is not pasted tool output for: "
                      + "; ".join(ungrounded[:3])
                      + ". Quote a verbatim chunk (40+ chars) from this "
                        "session's tool results, or demote the item to a lead.")
            loop.note(f"REPORT REJECTED: {detail}")
            _emit(session, {"type": "report_rejected", "detail": detail})
            return False
    if require_work and not report["confirmed"] and not report["leads"]:
        # A completely empty report after barely trying is a give-up, not a
        # result. Force either more recon or an honest lead per blocked area.
        # (The budget-exhausted auto-save bypasses this: it is already honest
        # about being interrupted.)
        corpus = _session_corpus(run).lower()
        cert_blocked = ("certificate" in corpus
                        and ("verify" in corpus or "self-signed" in corpus
                             or "self signed" in corpus))
        if cert_blocked and not getattr(loop.target, "allow_insecure_tls", False):
            detail = ("this session hit TLS certificate failures while 'Allow "
                      "insecure TLS' is OFF on the target. Do NOT file an empty "
                      "report: record the blocked HTTPS surface as a lead "
                      "(suspicion = the exact TLS error line, how_to_verify = "
                      "operator enables 'Allow insecure TLS' on the target, then "
                      "re-run vhost_enum + http_fetch), and meanwhile test what "
                      "is reachable over plain http (http:// fetches, nmap).")
        elif loop.tool_calls < 2:
            detail = (f"only {loop.tool_calls} successful tool call(s) ran and the "
                      "report has no confirmed findings and no leads. Run more "
                      "recon first (vhost_enum, http:// fetches, nmap services) "
                      "or document concrete leads with exact verify steps — a "
                      "fully empty report must earn its keep.")
        else:
            detail = ""
        if detail:
            loop.note(f"REPORT REJECTED: {detail}")
            _emit(session, {"type": "report_rejected", "detail": detail})
            return False
    meta = {
        "date": dj_timezone.now().date().isoformat(),
        "provider": loop.provider,
        "model": loop.model_id,
        "mode": run.autonomy_mode,
        "tool_calls": loop.tool_calls,
    }
    body = redact(render_report_markdown(report, run.target, meta), _secret(session))
    saved = Report.objects.create(
        target=run.target, agent_run=run, title=report["title"][:255],
        body=body, findings_count=len(report["confirmed"]),
        leads_count=len(report["leads"]),
    )
    for f in report["confirmed"]:
        detail = redact(
            f"{f['description']}\n\nSteps to reproduce:\n"
            + "\n".join(f"{n}. {s}" for n, s in enumerate(f["steps_to_reproduce"], 1))
            + f"\n\nEvidence:\n{f['evidence']}\n\nImpact:\n{f['impact']}"
            f"\n\nRemediation:\n{f['remediation']}",
            _secret(session),
        )
        Finding.objects.create(
            target=run.target, title=f["title"][:255],
            severity=f["severity"], detail=detail,
        )
    _action(session, run, thought=thought, tool="report",
            output=body[:ACTION_OUTPUT_CHARS])
    _finish(session, run, AgentRun.STATUS_OK)
    _emit(session, {"type": "report", "report_id": saved.id,
                    "findings": len(report["confirmed"]),
                    "leads": len(report["leads"]), "body": body})
    # "done" was already emitted by _finish; keep report last for the client.
    return True


def _save_interrupted_report(session, run, loop):
    """Last-resort Report so a budget-killed session keeps its work.

    Builds an honest interrupted report from the action history (no model
    output needed): what ran, zero confirmed, scope_notes naming the
    interruption. Returns True when saved."""
    steps = [f"#{a.seq} {a.tool or 'note'}: {(a.command or a.error or '')[:100]}"
             for a in AgentAction.objects.filter(agent_run=run).order_by("seq")]
    data = {
        "title": f"Interrupted recon of {run.target.domain} (budget exhausted)",
        "executive_summary": (
            f"Session ran {loop.tool_calls} tool calls then hit the step/time "
            f"budget before the model produced a report. No confirmed findings; "
            f"the steps below are preserved for a follow-up run with more budget."
        ),
        "methodology": "Executed steps:\n" + ("\n".join(steps) if steps else "(no tools ran)"),
        "scope_notes": ("AUTO-GENERATED: budget exhausted — rerun with a higher "
                        "step budget to convert these steps into findings/leads."),
        "confirmed": [],
        "leads": [],
    }
    try:
        return _finalize_report(session, run, loop, "auto-generated (budget exhausted)", data,
                                require_work=False)
    except Exception:
        return False


def _drive(session):
    """Worker body: think -> proposal -> [approval] -> execute -> … -> report."""
    run = session["run"]
    loop = session["loop"]
    refusal_sig = None
    refusal_count = 0
    try:
        _emit(session, {"type": "status", "status": "running"})
        while True:
            if session["stop"].is_set():
                _finish(session, run, AgentRun.STATUS_STOPPED, "Stopped by operator.")
                return
            # Re-read the target every iteration so operator edits made
            # mid-run (scope fixes, fresh session tokens) take effect.
            loop.target = Target.objects.get(id=run.target_id)
            if not session.get("wrapup_warned") and (
                    loop.max_iterations - loop.iterations <= 3):
                session["wrapup_warned"] = True
                loop.note(
                    f"BUDGET ALMOST OUT ({loop.max_iterations - loop.iterations} "
                    f"steps left): no new enumeration — follow up your top "
                    f"lead or write the final ```report block now."
                )
                _emit(session, {"type": "budget",
                                "detail": "Budget almost out — wrapping up."})
            if not loop.budget_left():
                # One last chance: demand the report instead of dying silently.
                loop.note(
                    "BUDGET EXHAUSTED (max iterations/time). No more tool calls: "
                    "write the final ```report block now with whatever evidence you have."
                )
                _emit(session, {"type": "budget", "detail": "Budget exhausted — requesting final report."})
                try:
                    raw = loop.think()
                except AgentAPIError as exc:
                    session["llm_errors"] = session.get("llm_errors", 0) + 1
                    if session["llm_errors"] >= 3:
                        _finish(session, run, AgentRun.STATUS_ERROR,
                                f"LLM failed {session['llm_errors']} times consecutively: {exc}")
                        _emit(session, {"type": "error", "detail": f"LLM failed repeatedly: {exc}"})
                        return
                    loop.note(f"LLM error (attempt {session['llm_errors']}/3): {exc}. Please try again.")
                    _emit(session, {"type": "error", "detail": f"LLM error: {exc}"})
                    # Don't return — let the loop continue and maybe save interrupted report
                else:
                    session["llm_errors"] = 0
                    kind, thought, payload = parse_reply(raw)
                    if kind == "report" and _finalize_report(session, run, loop, thought, payload):
                        return
                # The model failed at the last step — save an auto-generated
                # interrupted report from the action history so the session's
                # work survives instead of dying as a bare error.
                if _save_interrupted_report(session, run, loop):
                    return
                _finish(session, run, AgentRun.STATUS_ERROR,
                        "Budget exhausted and no valid report was produced.")
                _emit(session, {"type": "error",
                                "detail": "Budget exhausted with no valid report."})
                return
            try:
                raw = loop.think()
            except AgentAPIError as exc:
                # LLM error (empty reply, timeout, etc.) — don't kill the session,
                # just tell the model and let it retry. Count consecutive LLM errors.
                session["llm_errors"] = session.get("llm_errors", 0) + 1
                if session["llm_errors"] >= 3:
                    _finish(session, run, AgentRun.STATUS_ERROR,
                            f"LLM failed {session['llm_errors']} times consecutively: {exc}")
                    _emit(session, {"type": "error", "detail": f"LLM failed repeatedly: {exc}"})
                    return
                loop.note(f"LLM error (attempt {session['llm_errors']}/3): {exc}. Please try again.")
                _emit(session, {"type": "error", "detail": f"LLM error: {exc}"})
                continue
            session["llm_errors"] = 0  # reset on success
            kind, thought, payload = parse_reply(raw)

            if kind == "invalid":
                session["empty_turns"] = 0  # fenced attempt — parse_failures handles it
                loop.parse_failures += 1
                if loop.parse_failures > MAX_PARSE_RETRIES:
                    _finish(session, run, AgentRun.STATUS_ERROR,
                            "Model kept emitting malformed tool/report blocks.")
                    _emit(session, {"type": "error",
                                    "detail": "Malformed model output (see timeline)."})
                    return
                loop.note("Your last reply had a malformed ```tool/```report block "
                          "(bad JSON or wrong shape). Re-emit it correctly.")
                _emit(session, {"type": "thought", "text": thought,
                                "warning": "malformed block — asked to retry"})
                continue

            if kind == "thought":
                # No-fence reply: escalate fast instead of nudging forever.
                # Small models drift into prose; pull them back to protocol.
                session["empty_turns"] = session.get("empty_turns", 0) + 1
                n = session["empty_turns"]
                if looks_like_unfenced_report(raw):
                    loop.note(
                        "That reads like a report but it is NOT in a ```report "
                        "fenced block — prose reports are ignored. Re-emit the "
                        "COMPLETE report now inside one ```report JSON fence:\n"
                        + _REPORT_SKELETON)
                    _emit(session, {"type": "thought", "text": thought,
                                    "warning": "unfenced report — re-emit in a ```report fence"})
                elif n >= 5:
                    loop.note("5 replies with no tool call and no report block. "
                              "Stopping here and saving what exists.")
                    if _save_interrupted_report(session, run, loop):
                        return
                    if (thought or "").strip():
                        _emit(session, {"type": "thought", "text": thought})
                    _finish(session, run, AgentRun.STATUS_ERROR,
                            "Model stopped producing tool/report blocks.")
                    _emit(session, {"type": "error",
                                    "detail": "No tool/report blocks produced."})
                    return
                elif n >= 3:
                    loop.note(
                        f"You have replied {n} times with no tool call and no "
                        f"report block. Your next reply MUST end with exactly "
                        f"one ```tool or ```report fenced block — no "
                        f"exceptions, no prose-only replies.")
                    _emit(session, {"type": "thought", "text": thought,
                                    "warning": "fenced block required next reply"})
                elif n % 2 == 0:
                    loop.note(
                        "PROTOCOL REMINDER — every reply ends with EXACTLY ONE "
                        "fenced block. Tool call:\n```tool\n"
                        '{"name": "header_audit", '
                        '"args": {"url": "https://example.com/"}}\n```\n'
                        "or the final report as ```report {...}. "
                        "Prose alone does nothing — continue with a block.")
                    _emit(session, {"type": "thought", "text": thought})
                else:
                    loop.note("Noted. Continue: call a tool or write the final report.")
                    _emit(session, {"type": "thought", "text": thought})
                continue

            if kind == "report":
                session["empty_turns"] = 0
                if (thought or "").strip():
                    _emit(session, {"type": "thought", "text": thought})
                if _finalize_report(session, run, loop, thought, payload):
                    return
                continue  # rejected report counts as an iteration; model retries

            # kind == "tool"
            session["empty_turns"] = 0
            name = (payload.get("name") or "").strip() if isinstance(payload, dict) else ""
            args = payload.get("args", {}) if isinstance(payload, dict) else {}
            raw_name = payload.get("name") if isinstance(payload, dict) else None

            # No usable tool name — malformed proposal, not a real call.
            if not name:
                session["empty_turns"] = 0
                loop.parse_failures += 1
                detail = (
                    "Your last reply's ```tool block had no usable tool name "
                    f"(got {raw_name!r}). Include the tool name from the Available-tools "
                    'list, e.g. ```tool\n{"name": "http_fetch", "args": {"url": "..."}}\n```'
                )
                if loop.parse_failures > MAX_PARSE_RETRIES:
                    _finish(session, run, AgentRun.STATUS_ERROR,
                            "Model kept emitting malformed tool/report blocks.")
                    _emit(session, {"type": "error",
                                    "detail": "Malformed model output (see timeline)."})
                    return
                _action(session, run, thought=thought, tool=str(raw_name or ""),
                        args_json=json.dumps(args, default=str), command="",
                        status=AgentAction.STATUS_ERROR, error=detail)
                loop.note(detail)
                _emit(session, {"type": "malformed", "tool": raw_name or "",
                                "args": args,
                                "payload": payload if isinstance(payload, (dict, list)) else str(payload),
                                "thought": thought, "detail": detail})
                continue

            try:
                preview = loop.check_tool(name, args)
            except ReconError as exc:
                # Same broken proposal twice = the model isn't learning the
                # refusal — escalate the nudge instead of burning approvals.
                # For unknown tool names, key only on the name so repeated
                # unknown-tool attempts escalate even with varying args.
                allowed = allowed_tools(loop.target.automation_level)
                if name not in allowed:
                    sig = (name,)
                else:
                    sig = (name, json.dumps(args, sort_keys=True)
                           if isinstance(args, dict) else repr(args))
                if sig == refusal_sig:
                    refusal_count += 1
                else:
                    refusal_sig, refusal_count = sig, 1
                msg = f"TOOL REFUSED: {exc}"
                if refusal_count >= 2:
                    msg += (f" (you proposed this EXACT call {refusal_count}x — "
                            f"STOP repeating it; fix the args or move on)")
                loop.tool_result_note(name, msg)
                _action(session, run, thought=thought, tool=name,
                        args_json=json.dumps(args), command="",
                        status=AgentAction.STATUS_ERROR, error=str(exc))
                _emit(session, {"type": "tool_result", "tool": name, "ok": False,
                                "error": str(exc), "args": args})
                if refusal_count >= 4:
                    loop.note("You keep repeating refused calls instead of "
                              "adapting. Write the final ```report block now "
                              "with whatever evidence you have.")
                continue
            # Any proposal that passes validation resets the counter.
            refusal_sig, refusal_count = None, 0
            proposal = {"kind": "proposal", "thought": thought, "tool": name,
                        "args": args, "command": preview}
            # Narrate every turn in BOTH modes: the model's thinking and the
            # exact command print live even while a semi proposal waits for
            # approval (the approval card stays the decision point).
            if (thought or "").strip():
                _emit(session, {"type": "thought", "text": thought})
            _emit(session, {"type": "tool_call", **proposal})
            if run.autonomy_mode == "semi":
                session["awaiting"] = "proposal"
                decision = _wait_approval(session, run, proposal)
                session["awaiting"] = None
                _set_pending(run, None)
                if decision is None:  # stopped while waiting
                    _finish(session, run, AgentRun.STATUS_STOPPED, "Stopped by operator.")
                    return
                hint = redact((decision.get("hint") or "").strip(), _secret(session))
                if decision.get("decision") == "reject":
                    _action(session, run, thought=thought, tool=name,
                            args_json=json.dumps(args), command=preview,
                            status=AgentAction.STATUS_REJECTED,
                            error=(hint or "Rejected by operator."))
                    loop.note("OPERATOR REJECTED the proposed "
                              f"{name} call." + (f" Guidance: {hint}" if hint else "")
                              + " Propose a different next step.")
                    _emit(session, {"type": "rejected", "tool": name,
                                    "hint": hint})
                    continue
                # approved
                _emit(session, {"type": "approved", "tool": name, "hint": hint})
                if hint:
                    _action(session, run, thought="", tool="", command="",
                            output=hint, status=AgentAction.STATUS_NOTE)
                try:
                    _execute_tool(session, run, loop, thought, name, args, preview)
                except (AuthExpired, AuthNeeded) as exc:
                    if _auth_wall_flow(session, run, loop, name, args, preview, exc) == "stopped":
                        return
                    continue
                if hint:
                    loop.note(f"OPERATOR GUIDANCE (take into account next): {hint}")
            else:
                try:
                    _execute_tool(session, run, loop, thought, name, args, preview)
                except (AuthExpired, AuthNeeded) as exc:
                    if _auth_wall_flow(session, run, loop, name, args, preview, exc) == "stopped":
                        return
                    continue
    except ProviderError as exc:  # shouldn't happen (validated at start)
        _finish(session, run, AgentRun.STATUS_ERROR, str(exc))
        _emit(session, {"type": "error", "detail": str(exc)})
    except Exception as exc:  # never let the thread die silently
        _finish(session, run, AgentRun.STATUS_ERROR, f"Runtime failure: {exc}")
        _emit(session, {"type": "error", "detail": f"Runtime failure: {exc}"})
    finally:
        close_old_connections()


def start_session(run, max_iterations=25, time_budget=1800):
    """Create the loop, register the session, spawn the worker. Returns session."""
    loop = AgentLoop(run.target, run.provider, run.model,
                     max_iterations=max_iterations, time_budget=time_budget)
    session = {
        "run_id": run.id,
        "run": run,
        "loop": loop,
        "events": queue.Queue(maxsize=500),
        "approvals": queue.Queue(maxsize=10),
        "stop": threading.Event(),
        "done": False,
        "seq": 1,
        "skipped_hosts": set(),  # auth skipped for these hosts this session
    }
    with _registry_lock:
        _registry[run.id] = session
    thread = threading.Thread(target=_drive, args=(session,), daemon=True,
                              name=f"agent-run-{run.id}")
    session["thread"] = thread
    thread.start()
    return session
