from django.urls import path

from . import agent_views, views

urlpatterns = [
    # Auth (session cookies; login also sets the CSRF cookie).
    path("auth/login/", views.login_view, name="login"),
    path("auth/logout/", views.logout_view, name="logout"),
    path("auth/me/", views.me_view, name="me"),
    # Targets dashboard + creation/editing/deletion.
    path("targets/", views.targets_list, name="targets-list"),
    path("targets/<int:target_id>/", views.target_detail, name="target-detail"),
    path("stats/", views.stats, name="stats"),
    path("timeline/", views.timeline_all, name="timeline"),
    # Provider/model picker data (configured flags only, never keys).
    path("providers/", views.providers_list, name="providers-list"),
    # Agent plan generation: one-shot JSON or SSE stream, plus history.
    path("targets/<int:target_id>/run/", views.run_plan, name="run-plan"),
    path("targets/<int:target_id>/run-stream/", views.run_plan_stream, name="run-plan-stream"),
    path("targets/<int:target_id>/runs/", views.runs_list, name="runs-list"),
    path("targets/<int:target_id>/runs/<int:run_id>/", views.run_delete, name="run-delete"),
    # Findings (manual for now; scanner findings land here next milestone).
    path("targets/<int:target_id>/findings/", views.findings_list, name="findings-list"),
    path(
        "targets/<int:target_id>/findings/<int:finding_id>/",
        views.finding_delete,
        name="finding-delete",
    ),
    # Local recon (nmap/gobuster run on this box) + scan history.
    path("targets/<int:target_id>/scans/", views.scans_list, name="scans-list"),
    path(
        "targets/<int:target_id>/scans/<int:scan_id>/",
        views.scan_delete,
        name="scan-delete",
    ),
    # Autonomous agent sessions (tool-using loop + bounty reports).
    # NOTE: "agent/active/" must come before the <run_id> routes so
    # "active" isn't captured as a run id.
    path("agent/active/", agent_views.active_sessions, name="agent-active"),
    path("signup-mailbox/", agent_views.signup_mailbox_status, name="signup-mailbox"),
    path("targets/<int:target_id>/agent/start/", agent_views.agent_start, name="agent-start"),
    path(
        "targets/<int:target_id>/agent/<int:run_id>/events/",
        agent_views.agent_events,
        name="agent-events",
    ),
    path(
        "targets/<int:target_id>/agent/<int:run_id>/approve/",
        agent_views.agent_approve,
        name="agent-approve",
    ),
    path(
        "targets/<int:target_id>/agent/<int:run_id>/stop/",
        agent_views.agent_stop,
        name="agent-stop",
    ),
    path(
        "targets/<int:target_id>/agent/<int:run_id>/actions/",
        agent_views.agent_actions,
        name="agent-actions",
    ),
    path("targets/<int:target_id>/reports/", agent_views.reports_list, name="reports-list"),
]
