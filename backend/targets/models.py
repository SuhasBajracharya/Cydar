"""DB schema for the MVP: targets, agent plan runs, findings, and recon scans.

- Target: one row per "New Target" form submission (domain + scope rules).
- AgentRun: every plan generation is saved here (the "agent logs" side of
  the timeline). `system_prompt` is snapshotted so prompt iterations don't
  rewrite history.
- Finding: manual/placeholder findings for now (real scanner findings land
  here in the next milestone). Dashboard "findings count" = this table.
- Scan: local recon output (nmap/gobuster run on this box via recon/runner.py).
"""

from django.conf import settings
from django.db import models


class Target(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_DONE, "Done"),
    ]

    AUTOMATION_READONLY = "read-only"
    AUTOMATION_STANDARD = "standard"
    AUTOMATION_AGGRESSIVE = "aggressive"
    AUTOMATION_CHOICES = [
        (AUTOMATION_READONLY, "Read-only recon"),
        (AUTOMATION_STANDARD, "Standard automation"),
        (AUTOMATION_AGGRESSIVE, "Aggressive (where allowed)"),
    ]

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="targets"
    )
    domain = models.CharField(max_length=255)
    scope_rules = models.TextField(help_text="Program scope rules, pasted verbatim.")
    in_scope = models.TextField(blank=True, default="")
    out_of_scope = models.TextField(blank=True, default="")
    automation_level = models.CharField(
        max_length=20, choices=AUTOMATION_CHOICES, default=AUTOMATION_STANDARD
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    # Last-used LLM selection (OpenCode-style `provider/model`). Blank model
    # means "provider default". Updated on every run from the request.
    provider = models.CharField(max_length=20, default="deepseek")
    model = models.CharField(max_length=100, blank=True, default="")
    # Operator-supplied session credentials so tools can reach authenticated
    # surfaces (the agent never logs in itself — CAPTCHA/bot defenses block
    # that; the human authenticates in a browser and pastes the session).
    # SECURITY: `auth_value` is a live secret. It is never returned by any
    # API (_target_json omits it) and is scrubbed from tool output/events by
    # targets.agent_runtime.redact(). It is stored in SQLite in plaintext —
    # acceptable for this local single-user dev tool, NOT for shared hosting.
    AUTH_NONE = ""
    AUTH_BEARER = "bearer"
    AUTH_HEADER = "header"
    AUTH_COOKIE = "cookie"
    AUTH_CHOICES = [
        (AUTH_NONE, "None"),
        (AUTH_BEARER, "Bearer token"),
        (AUTH_HEADER, "Custom header"),
        (AUTH_COOKIE, "Session cookie"),
    ]
    auth_type = models.CharField(max_length=10, choices=AUTH_CHOICES, blank=True, default="")
    auth_name = models.CharField(max_length=100, blank=True, default="")  # header/cookie name
    auth_value = models.TextField(blank=True, default="")  # the secret
    auth_check_url = models.CharField(max_length=500, blank=True, default="")  # known-authed URL
    # Self-registration opt-in (trial_signup tool): creating accounts litters
    # the target's DB, so it needs this flag AND aggressive automation level.
    allow_self_registration = models.BooleanField(default=False)
    # Lab/self-signed cert mode: skip TLS cert verification for HTTPS tools.
    # Intended for THM/HTB/internal labs only — NOT for production bounty targets.
    allow_insecure_tls = models.BooleanField(
        default=False,
        help_text="Skip TLS certificate verification for HTTPS tools (lab/self-signed targets only).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.domain} ({self.status})"

    @property
    def auth_configured(self):
        return bool(self.auth_type and self.auth_value)


class AuthCredential(models.Model):
    """One saved session for ONE host (or *.domain pattern) on a target.

    A scope can span several subdomains with different logins/JWTs, so a
    target holds a list of these instead of a single global session. Values
    are write-only via the API (never serialized); matching reuses the same
    substring/wildcard rules as scope lists. The legacy single-slot fields
    on Target keep working as a fallback for old rows.
    """

    target = models.ForeignKey(Target, on_delete=models.CASCADE, related_name="auth_credentials")
    host_pattern = models.CharField(max_length=255, help_text="bare host or *.example.com")
    auth_type = models.CharField(max_length=10, choices=Target.AUTH_CHOICES, default=Target.AUTH_BEARER)
    auth_name = models.CharField(max_length=100, blank=True, default="")
    auth_value = models.TextField()  # the secret (JWT / cookie / token)
    site_username = models.CharField(max_length=255, blank=True, default="")  # trial-signup login id
    site_password = models.TextField(blank=True, default="")  # throwaway site password (re-login)
    check_url = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["host_pattern"]

    def __str__(self):
        return f"{self.host_pattern} ({self.auth_type})"


class ProviderConfig(models.Model):
    """Operator-supplied API key + optional base URL override for a provider.

    Complements (and takes precedence over) env-var configuration.
    Keys are write-only in the API — never returned in GET responses.
    Plaintext SQLite storage is dev-only (same posture as AuthCredential).
    """

    provider_id = models.CharField(max_length=30, unique=True)  # matches PROVIDERS keys
    api_key = models.TextField(blank=True, default="")  # the secret — write-only via API
    base_url = models.CharField(max_length=500, blank=True, default="")  # optional override
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider_id"]

    def __str__(self):
        has_key = "✓ key" if self.api_key else "no key"
        return f"{self.provider_id} ({has_key})"

    @property
    def has_key(self):
        return bool(self.api_key)


class AgentRun(models.Model):
    """One saved plan generation (the agent's reasoning output / log)."""

    STATUS_OK = "ok"
    STATUS_ERROR = "error"
    STATUS_STOPPED = "stopped"
    STATUS_RUNNING = "running"  # transient: session thread still working
    STATUS_CHOICES = [
        (STATUS_OK, "OK"),
        (STATUS_ERROR, "Error"),
        (STATUS_STOPPED, "Stopped"),
        (STATUS_RUNNING, "Running"),
    ]

    KIND_PLAN = "plan"  # one-shot LLM recon plan (no tools)
    KIND_AGENT = "agent"  # autonomous loop session (tool calls + report)
    KIND_CHOICES = [(KIND_PLAN, "Plan"), (KIND_AGENT, "Agent")]

    target = models.ForeignKey(Target, on_delete=models.CASCADE, related_name="runs")
    system_prompt = models.TextField()  # snapshot of agent/prompts.py at run time
    user_prompt = models.TextField()  # the composed scope message
    output = models.TextField(blank=True, default="")
    is_streamed = models.BooleanField(default=False)
    # Snapshot of exactly which provider/model produced this run.
    provider = models.CharField(max_length=20, default="deepseek")
    model = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_OK)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    # Autonomous-loop fields (blank for KIND_PLAN rows).
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_PLAN)
    autonomy_mode = models.CharField(max_length=10, blank=True, default="")  # semi|auto
    iterations = models.IntegerField(default=0)
    # JSON of the tool call currently awaiting approval in semi mode
    # (null otherwise) — lets the UI recover the pending state on reload.
    pending_action = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Run {self.id} for {self.target.domain} ({self.status})"


class AgentAction(models.Model):
    """One step of an autonomous loop: the model's proposal + what happened.

    The audit trail of an agent session — every tool call, rejection,
    guidance note, and the final report, in order.
    """

    STATUS_OK = "ok"
    STATUS_ERROR = "error"
    STATUS_REJECTED = "rejected"  # user refused it in semi mode (never ran)
    STATUS_NOTE = "note"  # user guidance text, no tool involved
    STATUS_CHOICES = [
        (STATUS_OK, "OK"),
        (STATUS_ERROR, "Error"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_NOTE, "Note"),
    ]

    agent_run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="actions")
    seq = models.IntegerField()  # 1-based step number within the run
    thought = models.TextField(blank=True, default="")
    tool = models.CharField(max_length=30, blank=True, default="")
    args_json = models.TextField(blank=True, default="")
    command = models.TextField(blank=True, default="")  # preview or executed argv
    output = models.TextField(blank=True, default="")  # truncated tool output
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_OK)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["agent_run_id", "seq"]

    def __str__(self):
        return f"Action {self.seq} of run {self.agent_run_id} ({self.tool or 'note'})"


class Report(models.Model):
    """Bounty-ready report produced at the end of an autonomous session.

    `body` is the full markdown; `findings` were also copied to Finding rows
    so they appear in counts and stay editable/deletable individually.
    """

    target = models.ForeignKey(Target, on_delete=models.CASCADE, related_name="reports")
    agent_run = models.ForeignKey(
        AgentRun, on_delete=models.SET_NULL, null=True, blank=True, related_name="reports"
    )
    title = models.CharField(max_length=255)
    body = models.TextField()
    findings_count = models.IntegerField(default=0)
    leads_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Report {self.id} for {self.target.domain} ({self.findings_count} findings, {self.leads_count} leads)"


class Finding(models.Model):
    SEVERITY_CHOICES = [
        ("info", "Info"),
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("critical", "Critical"),
    ]

    target = models.ForeignKey(Target, on_delete=models.CASCADE, related_name="findings")
    title = models.CharField(max_length=255)
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default="info")
    detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.severity}] {self.title}"


class Scan(models.Model):
    """One local recon run (nmap/gobuster output) on the timeline.

    The exact argv is snapshotted in `command` so history stays readable.
    """

    STATUS_OK = "ok"
    STATUS_ERROR = "error"
    STATUS_CHOICES = [(STATUS_OK, "OK"), (STATUS_ERROR, "Error")]

    target = models.ForeignKey(Target, on_delete=models.CASCADE, related_name="scans")
    tool = models.CharField(max_length=20)  # "nmap" | "gobuster"
    preset = models.CharField(max_length=50, default="")
    command = models.TextField()
    output = models.TextField(blank=True, default="")
    truncated = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_OK)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Scan {self.id} ({self.tool}) for {self.target.domain} ({self.status})"
