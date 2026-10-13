"""JSON API for the MVP (session auth, single user).

Auth: Django session cookies. The SPA first GETs /api/auth/me (which sets
the CSRF cookie), then sends that cookie's value as X-CSRFToken on every
POST (see frontend src/api.js). Unauthenticated API calls get 401 JSON.

Routes (all under /api/, see urls.py):
  providers: GET /providers/  (picker data: configured flags + model lists)
  auth:    POST login/  POST logout/  GET me/
  targets: GET/POST /targets/  GET /targets/<id>/
  runs:    POST /targets/<id>/run/         (one-shot plan, JSON response)
           POST /targets/<id>/run-stream/  (SSE stream, saves run at end)
           GET  /targets/<id>/runs/         (timeline of past runs)
           DELETE /targets/<id>/runs/<run_id>/  (delete one timeline run)
  findings: GET/POST /targets/<id>/findings/
            DELETE /targets/<id>/findings/<finding_id>/
  scans:   GET/POST /targets/<id>/scans/    (local nmap/gobuster recon)
           DELETE /targets/<id>/scans/<scan_id>/

Run posts accept optional {"provider", "model"} (OpenCode-style
`provider/model`); omitted fields reuse the target's saved choice.
"""

import json

from django.contrib.auth import authenticate, login, logout
from django.db.models import Count
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from agent.client import AgentAPIError, build_user_prompt, generate_plan, stream_plan
from agent.prompts import SYSTEM_PROMPT
from agent.providers import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    ProviderError,
    fetch_remote_models,
    provider_status,
    resolve,
    test_provider_key,
)
from recon.runner import ReconError, allowed_presets, clean_host, run_preset
from recon.tools import parse_scope_list

from .models import AgentRun, AuthCredential, Finding, ProviderConfig, Report, Scan, Target


# --- helpers ---------------------------------------------------------------


def _body(request):
    """Parse a JSON request body, returning {} on empty/invalid input."""
    try:
        return json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _unauthorized(request):
    if not request.user.is_authenticated:
        return JsonResponse({"detail": "Login required."}, status=401)
    return None


def _target_json(target):
    creds = []
    try:
        slots = list(target.auth_credentials.all())
    except AttributeError:
        slots = []
    for s in slots:
        creds.append({
            "id": s.id,
            "host_pattern": s.host_pattern,
            "auth_type": s.auth_type,
            "auth_name": s.auth_name,
            "check_url": s.check_url,
            "site_username": s.site_username,
            # auth_value / site_password are write-only — never serialized.
            "configured": bool(s.auth_value),
            "updated_at": s.updated_at.isoformat(),
        })
    return {
        "id": target.id,
        "domain": target.domain,
        "scope_rules": target.scope_rules,
        "in_scope": target.in_scope,
        "out_of_scope": target.out_of_scope,
        "automation_level": target.automation_level,
        "status": target.status,
        "provider": target.provider,
        "model": target.model,
        # Auth metadata only — auth_value is a live secret, never serialized.
        "auth_type": target.auth_type,
        "auth_name": target.auth_name,
        "auth_check_url": target.auth_check_url,
        "auth_configured": target.auth_configured,
        "auth_credentials": creds,
        "allow_self_registration": target.allow_self_registration,
        "allow_insecure_tls": target.allow_insecure_tls,
        "findings_count": getattr(target, "findings_count", target.findings.count()),
        "created_at": target.created_at.isoformat(),
    }


def _run_json(run):
    return {
        "id": run.id,
        "status": run.status,
        "is_streamed": run.is_streamed,
        "provider": run.provider,
        "model": run.model,
        "kind": run.kind,
        "autonomy_mode": run.autonomy_mode,
        "iterations": run.iterations,
        "output": run.output,
        "error": run.error,
        "created_at": run.created_at.isoformat(),
    }


def _finding_json(finding):
    return {
        "id": finding.id,
        "title": finding.title,
        "severity": finding.severity,
        "detail": finding.detail,
        "created_at": finding.created_at.isoformat(),
    }


def _report_json(report):
    return {
        "id": report.id,
        "title": report.title,
        "findings_count": report.findings_count,
        "leads_count": report.leads_count,
        "agent_run": report.agent_run_id,
        "body": report.body,
        "created_at": report.created_at.isoformat(),
    }


def _scan_json(scan):
    return {
        "id": scan.id,
        "tool": scan.tool,
        "preset": scan.preset,
        "command": scan.command,
        "output": scan.output,
        "truncated": scan.truncated,
        "status": scan.status,
        "error": scan.error,
        "created_at": scan.created_at.isoformat(),
    }


def _sse(payload):
    """Encode one server-sent-event dict as an SSE `data:` frame."""
    return f"data: {json.dumps(payload)}\n\n"


# --- auth ------------------------------------------------------------------


@ensure_csrf_cookie
@require_http_methods(["POST"])
def login_view(request):
    data = _body(request)
    user = authenticate(request, username=data.get("username"), password=data.get("password"))
    if user is None:
        return JsonResponse({"detail": "Invalid username or password."}, status=400)
    login(request, user)
    return JsonResponse({"username": user.username})


@require_http_methods(["POST"])
def logout_view(request):
    if (denied := _unauthorized(request)) is not None:
        return denied
    logout(request)
    return JsonResponse({"ok": True})


@ensure_csrf_cookie
@require_http_methods(["GET"])
def me_view(request):
    if not request.user.is_authenticated:
        return JsonResponse({"authenticated": False}, status=401)
    return JsonResponse({"authenticated": True, "username": request.user.username})


# --- providers -----------------------------------------------------------


@require_http_methods(["GET"])
def providers_list(request):
    """Picker data: every known provider or configured-only providers.

    Pass ?all=1 or ?configured_only=0 to get all available providers (for the add modal).
    Default without params returns all providers with their configured state, or can be filtered.
    """
    if (denied := _unauthorized(request)) is not None:
        return denied
    all_status = provider_status()
    if request.GET.get("configured_only") == "1":
        filtered = [p for p in all_status if p.get("configured")]
        return JsonResponse(filtered, safe=False)
    return JsonResponse(all_status, safe=False)


@require_http_methods(["PUT", "DELETE"])
def provider_config_detail(request, provider_id):
    """Save or remove an operator-supplied API key + optional base URL override.

    PUT  — save/update key (and optional base_url).
    DELETE — remove DB-stored config (reverts to env var if one exists).
    Keys are write-only: never returned in any response.
    """
    if (denied := _unauthorized(request)) is not None:
        return denied
    if provider_id not in PROVIDERS:
        return JsonResponse({"detail": f"Unknown provider {provider_id!r}."}, status=404)

    if request.method == "DELETE":
        deleted, _ = ProviderConfig.objects.filter(provider_id=provider_id).delete()
        return JsonResponse({"deleted": bool(deleted)})

    data = _body(request)
    api_key = (data.get("api_key") or "").strip()
    base_url = (data.get("base_url") or "").strip()
    if not api_key and PROVIDERS[provider_id].get("requires_key", True):
        return JsonResponse({"detail": "api_key is required for this provider."}, status=400)

    obj, _created = ProviderConfig.objects.update_or_create(
        provider_id=provider_id,
        defaults={"api_key": api_key, "base_url": base_url},
    )
    return JsonResponse({
        "provider_id": provider_id,
        "has_key": bool(obj.api_key),
        "base_url": obj.base_url,
        "source": "database",
    })


@require_http_methods(["POST"])
def provider_test(request, provider_id):
    """Validate a provider's key by hitting its /v1/models endpoint."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    if provider_id not in PROVIDERS:
        return JsonResponse({"detail": f"Unknown provider {provider_id!r}."}, status=404)
    success, detail = test_provider_key(provider_id)
    return JsonResponse({"success": success, "detail": detail})


@require_http_methods(["GET"])
def provider_models(request, provider_id):
    """Live-fetch available models from the provider's /v1/models endpoint."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    if provider_id not in PROVIDERS:
        return JsonResponse({"detail": f"Unknown provider {provider_id!r}."}, status=404)
    models, error = fetch_remote_models(provider_id)
    if error:
        return JsonResponse({"models": [], "error": error})
    return JsonResponse({"models": models, "error": None})


# --- targets ---------------------------------------------------------------


@require_http_methods(["GET", "POST"])
def targets_list(request):
    if (denied := _unauthorized(request)) is not None:
        return denied
    if request.method == "POST":
        data = _body(request)
        if not data.get("domain") or not data.get("scope_rules"):
            return JsonResponse(
                {"detail": "`domain` and `scope_rules` are required."}, status=400
            )
        try:
            domain = clean_host(data["domain"])  # strips https://…/path clutter
        except ReconError as exc:
            return JsonResponse({"detail": str(exc)}, status=400)
        target = Target.objects.create(
            owner=request.user,
            domain=domain,
            scope_rules=data["scope_rules"],
            in_scope=data.get("in_scope", ""),
            out_of_scope=data.get("out_of_scope", ""),
            automation_level=data.get("automation_level", Target.AUTOMATION_STANDARD),
        )
        target.findings_count = 0
        return JsonResponse(_target_json(target), status=201)
    targets = (
        Target.objects.filter(owner=request.user)
        .annotate(findings_count=Count("findings"))
        .order_by("-created_at")
    )
    return JsonResponse([_target_json(t) for t in targets], safe=False)


def _save_credentials(target, entries):
    """Replace-all save of per-host session slots. Returns error string or None.

    Each entry: {id? (existing row), host_pattern, auth_type, auth_name?,
    auth_value? (required for new rows; omitted keeps the stored secret),
    check_url?}. Rows absent from the payload are deleted.
    """
    if not isinstance(entries, list):
        return "`auth_credentials` must be a list."
    if len(entries) > 20:
        return "At most 20 session slots per target."
    seen_ids = set()
    for e in entries:
        if not isinstance(e, dict):
            return "Each credential must be an object."
        pattern = (e.get("host_pattern") or "").strip().lower()
        if not pattern:
            return "Each credential needs `host_pattern`."
        bare = pattern[2:] if pattern.startswith("*.") else pattern
        try:
            clean_host(bare)
        except ReconError as exc:
            return f"Bad host_pattern {pattern!r}: {exc}."
        atype = (e.get("auth_type") or "").strip()
        if atype not in dict(Target.AUTH_CHOICES) or not atype:
            return f"Bad auth_type {e.get('auth_type')!r} for {pattern}."
        aname = (e.get("auth_name") or "").strip()
        if atype in (Target.AUTH_HEADER, Target.AUTH_COOKIE) and not aname:
            return f"auth_name is required for {atype} ({pattern})."
        if e.get("id"):
            try:
                row = AuthCredential.objects.get(id=e["id"], target=target)
            except AuthCredential.DoesNotExist:
                return f"Unknown credential id {e['id']}."
            row.host_pattern = pattern
            row.auth_type = atype
            row.auth_name = aname
            if e.get("auth_value"):
                row.auth_value = e["auth_value"]
            if not row.auth_value:
                return f"auth_value is required ({pattern})."
            row.check_url = (e.get("check_url") or "").strip()
            row.save()
            seen_ids.add(row.id)
        else:
            if not e.get("auth_value"):
                return f"auth_value is required for new slot ({pattern})."
            row = AuthCredential.objects.create(
                target=target, host_pattern=pattern, auth_type=atype,
                auth_name=aname, auth_value=e["auth_value"],
                check_url=(e.get("check_url") or "").strip(),
            )
            seen_ids.add(row.id)
    AuthCredential.objects.filter(target=target).exclude(id__in=seen_ids).delete()
    return None


@require_http_methods(["GET", "PUT", "DELETE"])
def target_detail(request, target_id):
    if (denied := _unauthorized(request)) is not None:
        return denied
    target = get_object_or_404(Target, id=target_id, owner=request.user)
    if request.method == "DELETE":
        target.delete()  # cascades to runs/actions/scans/findings/reports
        return JsonResponse({"ok": True})
    if request.method == "PUT":
        data = _body(request)
        if "domain" in data:
            try:
                target.domain = clean_host(data["domain"])
            except ReconError as exc:
                return JsonResponse({"detail": str(exc)}, status=400)
        for field in ("scope_rules", "in_scope", "out_of_scope"):
            if field in data:
                setattr(target, field, data[field] or "")
        if "automation_level" in data:
            if data["automation_level"] not in dict(Target.AUTOMATION_CHOICES):
                return JsonResponse(
                    {"detail": f"Bad automation_level {data['automation_level']!r}."},
                    status=400,
                )
            target.automation_level = data["automation_level"]
        # Session credentials (write-only: the secret is never returned).
        # Clearing = send auth_type "". Editing other fields without
        # auth_value keeps the stored secret.
        if "auth_type" in data:
            atype = (data["auth_type"] or "").strip()
            if atype not in dict(Target.AUTH_CHOICES):
                return JsonResponse(
                    {"detail": f"Bad auth_type {data['auth_type']!r}."}, status=400)
            if not atype:
                target.auth_type = ""
                target.auth_name = ""
                target.auth_value = ""
            else:
                target.auth_type = atype
                if "auth_name" in data:
                    target.auth_name = (data["auth_name"] or "").strip()
                if data.get("auth_value"):
                    target.auth_value = data["auth_value"]
                if not target.auth_value:
                    return JsonResponse(
                        {"detail": "auth_value is required when auth_type is set."},
                        status=400,
                    )
                if atype in (Target.AUTH_HEADER, Target.AUTH_COOKIE) and not target.auth_name:
                    return JsonResponse(
                        {"detail": "auth_name (header/cookie name) is required."},
                        status=400,
                    )
        elif data.get("auth_value"):
            # Secret rotation without touching the type.
            if not target.auth_type:
                return JsonResponse(
                    {"detail": "Set auth_type before saving a secret."}, status=400)
            target.auth_value = data["auth_value"]
        if "auth_name" in data and "auth_type" not in data:
            target.auth_name = (data["auth_name"] or "").strip()
        if "auth_check_url" in data:
            target.auth_check_url = (data["auth_check_url"] or "").strip()
        if "allow_self_registration" in data:
            target.allow_self_registration = bool(data["allow_self_registration"])
        if "allow_insecure_tls" in data:
            target.allow_insecure_tls = bool(data["allow_insecure_tls"])
        if "auth_credentials" in data:
            err = _save_credentials(target, data["auth_credentials"])
            if err is not None:
                return JsonResponse({"detail": err}, status=400)
        target.save()
        return JsonResponse(_target_json(target))
    return JsonResponse(_target_json(target))


@require_http_methods(["GET"])
def stats(request):
    """Dashboard aggregates for one user (single query set each)."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    user = request.user
    findings = Finding.objects.filter(target__owner=user)
    by_severity = {
        sev: findings.filter(severity=sev).count()
        for sev, _ in Finding.SEVERITY_CHOICES
    }
    return JsonResponse({
        "targets": Target.objects.filter(owner=user).count(),
        "findings": findings.count(),
        "by_severity": by_severity,
        "reports": Report.objects.filter(target__owner=user).count(),
        "agent_sessions": AgentRun.objects.filter(
            target__owner=user, kind=AgentRun.KIND_AGENT).count(),
        "scans": Scan.objects.filter(target__owner=user).count(),
    })


# --- global timeline -------------------------------------------------------


@require_http_methods(["GET"])
def timeline_all(request):
    """Cross-target activity: recent runs, findings, and reports for all of
    the operator's targets, newest first. Each item carries its target's id
    and domain so the UI can link back (and reuse the target-scoped delete
    endpoints). Capped per kind to keep the payload small."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    user = request.user
    runs = (
        AgentRun.objects.filter(target__owner=user)
        .select_related("target")
        .order_by("-created_at")[:40]
    )
    findings = (
        Finding.objects.filter(target__owner=user)
        .select_related("target")
        .order_by("-created_at")[:40]
    )
    reports = (
        Report.objects.filter(target__owner=user)
        .select_related("target")
        .order_by("-created_at")[:40]
    )
    return JsonResponse({
        "runs": [
            {**_run_json(r), "target_id": r.target_id, "domain": r.target.domain}
            for r in runs
        ],
        "findings": [
            {**_finding_json(f), "target_id": f.target_id, "domain": f.target.domain}
            for f in findings
        ],
        "reports": [
            {**_report_json(r), "target_id": r.target_id, "domain": r.target.domain}
            for r in reports
        ],
    })


# --- agent runs ------------------------------------------------------------


def _compose_prompt(target):
    return build_user_prompt(
        domain=target.domain,
        scope_rules=target.scope_rules,
        in_scope=target.in_scope,
        out_of_scope=target.out_of_scope,
        automation_level=target.automation_level,
    )


def _resolve_selection(data, target):
    """Return (provider_id, model_id) for a run request.

    Explicit `provider`/`model` in the body win; otherwise the target's saved
    choice is reused (dashboard "Run Agent" sends {}). Raises ProviderError
    (→ HTTP 400) on unknown provider, missing key, or empty model.
    """
    provider_id = (data.get("provider") or target.provider or DEFAULT_PROVIDER).strip()
    model = data["model"] if "model" in data else (target.model or "")
    _, _, model_id, _ = resolve(provider_id, model)
    return provider_id, model_id


@require_http_methods(["POST"])
def run_plan(request, target_id):
    """One-shot (non-streaming) plan generation. Saves an AgentRun, returns it."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    target = get_object_or_404(Target, id=target_id, owner=request.user)
    try:
        provider_id, model_id = _resolve_selection(_body(request), target)
    except ProviderError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    user_prompt = _compose_prompt(target)

    target.status = Target.STATUS_RUNNING
    target.provider = provider_id
    target.model = model_id
    target.save(update_fields=["status", "provider", "model", "updated_at"])
    try:
        output = generate_plan(user_prompt, system_prompt=SYSTEM_PROMPT, model=model_id, provider=provider_id)
    except AgentAPIError as exc:
        target.status = Target.STATUS_QUEUED  # back to queued so it can be retried
        target.save(update_fields=["status", "updated_at"])
        run = AgentRun.objects.create(
            target=target,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            status=AgentRun.STATUS_ERROR,
            error=str(exc),
            provider=provider_id,
            model=model_id,
        )
        return JsonResponse({"detail": str(exc), "run": _run_json(run)}, status=502)

    target.status = Target.STATUS_DONE
    target.save(update_fields=["status", "updated_at"])
    run = AgentRun.objects.create(
        target=target,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        output=output,
        status=AgentRun.STATUS_OK,
        provider=provider_id,
        model=model_id,
    )
    return JsonResponse(_run_json(run))


@require_http_methods(["POST"])
def run_plan_stream(request, target_id):
    """Streaming plan generation as SSE. Same DB result as run_plan.

    Events: {"type":"status"} -> {"type":"token","text":...} * ->
    {"type":"done","run_id":N} (or {"type":"error","detail":...}).
    The full output is saved as one AgentRun when the stream finishes.
    """
    if (denied := _unauthorized(request)) is not None:
        return denied
    target = get_object_or_404(Target, id=target_id, owner=request.user)
    try:
        provider_id, model_id = _resolve_selection(_body(request), target)
    except ProviderError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)
    user_prompt = _compose_prompt(target)

    def events():
        target.status = Target.STATUS_RUNNING
        target.provider = provider_id
        target.model = model_id
        target.save(update_fields=["status", "provider", "model", "updated_at"])
        yield _sse({"type": "status", "status": target.status})
        chunks = []
        try:
            for delta in stream_plan(user_prompt, system_prompt=SYSTEM_PROMPT, model=model_id, provider=provider_id):
                chunks.append(delta)
                yield _sse({"type": "token", "text": delta})
        except AgentAPIError as exc:
            target.status = Target.STATUS_QUEUED
            target.save(update_fields=["status", "updated_at"])
            run = AgentRun.objects.create(
                target=target,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
                status=AgentRun.STATUS_ERROR,
                error=str(exc),
                is_streamed=True,
                provider=provider_id,
                model=model_id,
            )
            yield _sse({"type": "error", "detail": str(exc), "run_id": run.id})
            return
        run = AgentRun.objects.create(
            target=target,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            output="".join(chunks),
            status=AgentRun.STATUS_OK,
            is_streamed=True,
            provider=provider_id,
            model=model_id,
        )
        target.status = Target.STATUS_DONE
        target.save(update_fields=["status", "updated_at"])
        yield _sse({"type": "done", "run_id": run.id})

    response = StreamingHttpResponse(events(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    return response


@require_http_methods(["GET"])
def runs_list(request, target_id):
    """Past runs for the per-target timeline (newest first, cap 50)."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    target = get_object_or_404(Target, id=target_id, owner=request.user)
    runs = target.runs.all()[:50]
    return JsonResponse([_run_json(r) for r in runs], safe=False)


@require_http_methods(["DELETE"])
def run_delete(request, target_id, run_id):
    """Delete one plan run from the timeline."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    run = get_object_or_404(AgentRun, id=run_id, target__id=target_id, target__owner=request.user)
    run.delete()
    return JsonResponse({"ok": True})


# --- findings --------------------------------------------------------------


@require_http_methods(["GET", "POST"])
def findings_list(request, target_id):
    if (denied := _unauthorized(request)) is not None:
        return denied
    target = get_object_or_404(Target, id=target_id, owner=request.user)
    if request.method == "POST":
        data = _body(request)
        if not data.get("title"):
            return JsonResponse({"detail": "`title` is required."}, status=400)
        finding = Finding.objects.create(
            target=target,
            title=data["title"].strip(),
            severity=data.get("severity", "info"),
            detail=data.get("detail", ""),
        )
        return JsonResponse(_finding_json(finding), status=201)
    return JsonResponse([_finding_json(f) for f in target.findings.all()], safe=False)


@require_http_methods(["DELETE"])
def finding_delete(request, target_id, finding_id):
    """Delete one finding from the timeline."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    finding = get_object_or_404(
        Finding, id=finding_id, target__id=target_id, target__owner=request.user
    )
    finding.delete()
    return JsonResponse({"ok": True})


# --- local recon scans -----------------------------------------------------


@require_http_methods(["GET", "POST"])
def scans_list(request, target_id):
    """Local recon timeline + runner.

    GET returns past scans (newest first, cap 50). POST {"preset"} runs one
    scan synchronously (nmap/gobuster on this box) against the target's
    domain and saves the output as a Scan row. Only presets allowed for the
    target's automation level are accepted (see recon/runner.py).
    """
    if (denied := _unauthorized(request)) is not None:
        return denied
    target = get_object_or_404(Target, id=target_id, owner=request.user)
    if request.method == "POST":
        data = _body(request)
        preset = (data.get("preset") or "").strip()
        if preset not in allowed_presets(target.automation_level):
            return JsonResponse(
                {
                    "detail": (
                        f"Unknown or disallowed preset {preset!r} for "
                        f"automation level '{target.automation_level}'. "
                        f"Valid: {allowed_presets(target.automation_level)}."
                    )
                },
                status=400,
            )
        tool = preset.split("-", 1)[0]
        try:
            command, output, truncated = run_preset(
                preset, target.domain, target.automation_level, target.allow_insecure_tls
            )
        except ReconError as exc:
            scan = Scan.objects.create(
                target=target,
                tool=tool,
                preset=preset,
                command=preset,  # never ran — keep the requested preset
                status=Scan.STATUS_ERROR,
                error=str(exc),
            )
            # 400 for bad input/refusals, 502 when the tool itself failed.
            msg = str(exc)
            status = 400 if (
                "Unknown" in msg or "needs automation" in msg
                or "Invalid" in msg or "Refusing" in msg or "Empty" in msg
            ) else 502
            return JsonResponse({"detail": msg, "scan": _scan_json(scan)}, status=status)
        scan = Scan.objects.create(
            target=target,
            tool=tool,
            preset=preset,
            command=command,
            output=output,
            truncated=truncated,
            status=Scan.STATUS_OK,
        )
        return JsonResponse(_scan_json(scan), status=201)
    scans = target.scans.all()[:50]
    return JsonResponse([_scan_json(s) for s in scans], safe=False)


@require_http_methods(["DELETE"])
def scan_delete(request, target_id, scan_id):
    """Delete one recon scan from the timeline."""
    if (denied := _unauthorized(request)) is not None:
        return denied
    scan = get_object_or_404(
        Scan, id=scan_id, target__id=target_id, target__owner=request.user
    )
    scan.delete()
    return JsonResponse({"ok": True})
