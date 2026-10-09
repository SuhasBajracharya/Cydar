"""HTTP layer for autonomous agent sessions (see agent_runtime.py).

  POST /targets/<id>/agent/start/            begin a session (semi|auto)
  GET  /targets/<id>/agent/<run>/events/     SSE stream of session events
  POST /targets/<id>/agent/<run>/approve/    semi-mode decision (+hint)
  POST /targets/<id>/agent/<run>/stop/       halt a running session
  GET  /targets/<id>/agent/<run>/actions/    persisted audit trail
  GET  /targets/<id>/reports/                bounty-ready reports

Event types over SSE: status, thought, tool_call, pending, malformed,
approved, rejected, tool_result, report_rejected, budget, report, error, done.
"""

import json
import queue

from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods

from agent.providers import ProviderError
from recon.runner import ReconError, clean_host
from recon.tools import parse_scope_list

from .agent_runtime import get_session, reconcile_running, start_session
from .models import AgentRun, Report, Target
from .views import _body, _report_json, _resolve_selection, _run_json, _sse, _unauthorized

DEFAULT_MAX_ITERATIONS = 25
DEFAULT_TIME_BUDGET = 1800  # local models are free — generous by default


def _action_json(action):
    return {
        "id": action.id,
        "seq": action.seq,
        "thought": action.thought,
        "tool": action.tool,
        "args": action.args_json,
        "command": action.command,
        "output": action.output,
        "status": action.status,
        "error": action.error,
        "created_at": action.created_at.isoformat(),
    }


def _session_or_404(request, target_id, run_id):
    target = get_object_or_404(Target, id=target_id, owner=request.user)
    run = get_object_or_404(AgentRun, id=run_id, target=target, kind=AgentRun.KIND_AGENT)
    session = get_session(run.id)
    return target, run, session


@require_http_methods(["POST"])
def agent_start(request, target_id):
    """Begin an autonomous session: saves an AgentRun, spawns the worker."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    target = get_object_or_404(Target, id=target_id, owner=request.user)
    data = _body(request)
    mode = (data.get("mode") or "semi").strip()
    if mode not in ("semi", "auto"):
        return JsonResponse({"detail": "mode must be 'semi' or 'auto'."}, status=400)
    # Fail fast on unusable scope instead of burning model steps on refusals:
    # a non-blank in-scope box must parse to at least one host rule, and the
    # domain itself must be a valid scan target.
    if (target.in_scope or "").strip() and not parse_scope_list(target.in_scope):
        return JsonResponse(
            {"detail": "In-scope list has no usable hosts (one host/pattern per line, commas ok). Edit the target first."},
            status=400,
        )
    try:
        clean_host(target.domain)
    except ReconError as exc:
        return JsonResponse(
            {"detail": f"Target domain is not scannable ({exc}). Edit the target first."},
            status=400,
        )
    try:
        max_iterations = int(data.get("max_iterations") or DEFAULT_MAX_ITERATIONS)
    except (TypeError, ValueError):
        return JsonResponse({"detail": "max_iterations must be a number."}, status=400)
    max_iterations = max(1, min(max_iterations, 100))
    try:
        provider_id, model_id = _resolve_selection(data, target)
    except ProviderError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    # One session per target: concurrent runs on the same target split GPU,
    # duplicate recon work, and confuse the live feed. The UI disables Run
    # while a session is active; this 409 is the backstop (incl. races).
    reconcile_running()
    existing = (
        AgentRun.objects.filter(
            target=target, kind=AgentRun.KIND_AGENT, status=AgentRun.STATUS_RUNNING
        )
        .order_by("-created_at")
        .first()
    )
    if existing is not None:
        return JsonResponse(
            {"detail": f"Session #{existing.id} is already running on this target. Stop it before starting a new one."},
            status=409,
        )
    run = AgentRun.objects.create(        target=target,
        system_prompt="(agentic loop prompt; see agent/prompts.py)",
        user_prompt=f"Autonomous engagement on {target.domain} ({mode} mode).",
        status=AgentRun.STATUS_RUNNING,
        kind=AgentRun.KIND_AGENT,
        autonomy_mode=mode,
        provider=provider_id,
        model=model_id,
    )
    target.status = Target.STATUS_RUNNING
    target.provider = provider_id
    target.model = model_id
    target.save(update_fields=["status", "provider", "model", "updated_at"])
    start_session(run, max_iterations=max_iterations, time_budget=DEFAULT_TIME_BUDGET)
    return JsonResponse({"run_id": run.id, "mode": mode,
                         "run": _run_json(run)}, status=202)


@require_http_methods(["GET"])
def agent_events(request, target_id, run_id):
    """SSE stream of live session events. Ends after the done frame."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    _, run, session = _session_or_404(request, target_id, run_id)
    if session is None:
        # Viewer arrived late (or server restarted): replay the ending.
        def late():
            yield _sse({"type": "done", "run_id": run.id, "status": run.status})
        return StreamingHttpResponse(late(), content_type="text/event-stream")

    def stream():
        while True:
            try:
                event = session["events"].get(timeout=25)
            except queue.Empty:
                yield ": heartbeat\n\n"  # keep proxies from killing idle streams
                if session["done"] and session["events"].empty():
                    return
                continue
            yield _sse(event)
            if event.get("type") == "done":
                return

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    return response


@require_http_methods(["POST"])
def agent_approve(request, target_id, run_id):
    """Operator decision: approve|reject (+hint) for proposals, or
    approve|reject|skip (+hint) for auth walls. Skip remembers the host for
    this session (employee-only logins are asked about once)."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    _, run, session = _session_or_404(request, target_id, run_id)
    if session is None or session["done"]:
        return JsonResponse({"detail": "Session is no longer running."}, status=409)
    data = _body(request)
    decision = (data.get("decision") or "").strip()
    if decision not in ("approve", "reject", "skip"):
        return JsonResponse({"detail": "decision must be 'approve', 'reject' or 'skip'."}, status=400)
    pending = json.loads(run.pending_action) if run.pending_action else None
    if (pending or {}).get("kind") == "auth":
        if decision == "skip" and not (pending.get("host") or session.get("auth_host")):
            return JsonResponse({"detail": "Nothing to skip."}, status=409)
    elif decision == "skip":
        return JsonResponse({"detail": "'skip' is only for login-wall (auth) pauses."}, status=400)
    elif not run.pending_action:
        return JsonResponse({"detail": "No action awaiting approval."}, status=409)
    hint = (data.get("hint") or "").strip()[:2000]
    if session.get("decided"):
        # Double-click / double-poll: a decision is already queued for the
        # current pause. A second one would bleed into the NEXT pause.
        return JsonResponse({"detail": "Decision already recorded."}, status=409)
    try:
        session["approvals"].put_nowait({"decision": decision, "hint": hint})
        session["decided"] = True
    except queue.Full:
        return JsonResponse({"detail": "Approval queue full — try again."}, status=409)
    return JsonResponse({"ok": True, "decision": decision})


@require_http_methods(["POST"])
def agent_stop(request, target_id, run_id):
    """Halt a running session (current tool call finishes first)."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    _, run, session = _session_or_404(request, target_id, run_id)
    if session is None or session["done"]:
        return JsonResponse({"detail": "Session is no longer running."}, status=409)
    session["stop"].set()
    return JsonResponse({"ok": True})


@require_http_methods(["GET"])
def agent_actions(request, target_id, run_id):
    """Persisted audit trail of one session (survives reloads)."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    _, run, _ = _session_or_404(request, target_id, run_id)
    actions = run.actions.all()
    payload = {
        "run": _run_json(run),
        "pending": json.loads(run.pending_action) if run.pending_action else None,
        "actions": [_action_json(a) for a in actions],
    }
    return JsonResponse(payload)


@require_http_methods(["GET"])
def active_sessions(request):
    """Running agent sessions across all of the operator's targets.

    Lets the UI show "AI already running" when a session started on one
    target is still working while the operator views another target, and
    powers the dashboard's "awaiting approval" list: `has_pending` marks
    semi-mode sessions paused for an operator decision, with the proposed
    tool (or auth host) summarized inline.
    Reconciles first: rows stuck at `running` whose worker is gone (server
    restart/reload, crashed thread) are marked stopped so the UI never
    shows phantom sessions that can no longer be approved or stopped.
    """
    if (denied := _unauthorized(request)) is not None:
        return denied
    reconcile_running()
    runs = (
        AgentRun.objects.filter(
            target__owner=request.user,
            kind=AgentRun.KIND_AGENT,
            status=AgentRun.STATUS_RUNNING,
        )
        .select_related("target")
        .order_by("-created_at")
    )
    return JsonResponse(
        [
            {
                "run_id": r.id,
                "target_id": r.target_id,
                "domain": r.target.domain,
                "mode": r.autonomy_mode,
                "iterations": r.iterations,
                "provider": r.provider,
                "model": r.model,
                "created_at": r.created_at.isoformat(),
                **_pending_summary(r),
            }
            for r in runs
        ],
        safe=False,
    )


def _pending_summary(run):
    """Summarize a session's pending approval (for the awaiting list).

    Returns has_pending=False when the worker isn't paused; otherwise the
    proposal kind (proposal|auth), the proposed tool, and the auth host.
    Corrupt payloads degrade to "waiting", never a 500.
    """
    summary = {"has_pending": False, "pending_kind": None,
               "pending_tool": None, "pending_host": None}
    try:
        payload = json.loads(run.pending_action) if run.pending_action else None
    except (ValueError, TypeError):
        return summary
    if not isinstance(payload, dict):
        return summary
    summary["has_pending"] = True
    summary["pending_kind"] = payload.get("kind") or "proposal"
    summary["pending_tool"] = payload.get("tool") or None
    summary["pending_host"] = payload.get("host") or None
    return summary


@require_http_methods(["GET"])
def signup_mailbox_status(request):
    """Whether the operator's signup mailbox is configured (host flag only —
    never credentials). The trial_signup tool needs this."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    from recon.mailbox import status as mailbox_status
    return JsonResponse(mailbox_status())


@require_http_methods(["GET"])
def reports_list(request, target_id):
    """Bounty-ready reports for a target (newest first, cap 20)."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    target = get_object_or_404(Target, id=target_id, owner=request.user)
    reports = target.reports.all()[:20]
    return JsonResponse([_report_json(r) for r in reports], safe=False)
