"""Django admin for the targets app — all models registered.

Tuned for recon triage: useful list columns, filters, search, and inlines
so a target's sessions, actions, findings, scans and reports are browsable
from one place. This is a local single-user dev tool: session secrets
(auth_value / site_password) are visible here in plaintext (the API never
exposes them). Do not expose this admin beyond localhost.
"""

from django.contrib import admin

from .models import (
    AgentAction,
    AgentRun,
    AuthCredential,
    Finding,
    ProviderConfig,
    Report,
    Scan,
    Target,
)

admin.site.site_header = "Cydar — admin"
admin.site.site_title = "Cydar"
admin.site.index_title = "Targets, agent sessions & findings"


class AuthCredentialInline(admin.TabularInline):
    model = AuthCredential
    extra = 0
    fields = ("host_pattern", "auth_type", "auth_name", "check_url", "site_username", "updated_at")
    readonly_fields = ("updated_at",)


class AgentActionInline(admin.TabularInline):
    """Per-step audit trail on the run page (collapsed — can be long)."""

    model = AgentAction
    extra = 0
    can_delete = False
    show_change_link = True
    classes = ["collapse"]
    fields = ("seq", "tool", "status", "command", "created_at")
    readonly_fields = ("seq", "tool", "status", "command", "created_at")
    ordering = ("seq",)


@admin.register(Target)
class TargetAdmin(admin.ModelAdmin):
    list_display = (
        "domain", "owner", "status", "automation_level",
        "provider", "model", "allow_insecure_tls", "updated_at",
    )
    list_filter = ("status", "automation_level", "provider", "allow_insecure_tls")
    search_fields = ("domain", "owner__username")
    readonly_fields = ("created_at", "updated_at")
    inlines = [AuthCredentialInline]
    fieldsets = (
        (None, {
            "fields": ("owner", "domain", "status", "automation_level", "provider", "model"),
        }),
        ("Scope", {
            "fields": ("scope_rules", "in_scope", "out_of_scope"),
        }),
        ("Session auth (secret is write-only in the API, visible here)", {
            "fields": ("auth_type", "auth_name", "auth_value", "auth_check_url"),
        }),
        ("Lab options", {
            "fields": ("allow_self_registration", "allow_insecure_tls"),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
        }),
    )


@admin.register(AuthCredential)
class AuthCredentialAdmin(admin.ModelAdmin):
    list_display = ("host_pattern", "target", "auth_type", "site_username", "updated_at")
    list_filter = ("auth_type",)
    search_fields = ("host_pattern", "target__domain", "site_username")
    readonly_fields = ("created_at", "updated_at")


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin):
    list_display = (
        "id", "target", "kind", "autonomy_mode", "status",
        "provider", "model", "iterations", "created_at",
    )
    list_filter = ("kind", "autonomy_mode", "status", "provider")
    search_fields = ("target__domain", "provider", "model", "error")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at",)
    inlines = [AgentActionInline]
    fieldsets = (
        (None, {
            "fields": ("target", "kind", "autonomy_mode", "status", "iterations",
                       "provider", "model", "is_streamed"),
        }),
        ("Prompts & output", {
            "classes": ["collapse"],
            "fields": ("system_prompt", "user_prompt", "output", "error", "pending_action"),
        }),
        ("Timestamps", {
            "fields": ("created_at",),
        }),
    )


@admin.register(AgentAction)
class AgentActionAdmin(admin.ModelAdmin):
    list_display = ("id", "agent_run", "seq", "tool", "status", "short_command", "created_at")
    list_filter = ("status", "tool")
    search_fields = ("tool", "command", "error", "thought")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at",)
    fieldsets = (
        (None, {
            "fields": ("agent_run", "seq", "thought", "tool", "status"),
        }),
        ("Call & result", {
            "fields": ("args_json", "command", "output", "error"),
        }),
        ("Timestamps", {
            "fields": ("created_at",),
        }),
    )

    @admin.display(description="Command")
    def short_command(self, obj):
        cmd = obj.command or ""
        return cmd[:80] + ("…" if len(cmd) > 80 else "")


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ("title", "target", "agent_run", "findings_count", "leads_count", "created_at")
    search_fields = ("title", "body", "target__domain")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at",)


@admin.register(Finding)
class FindingAdmin(admin.ModelAdmin):
    list_display = ("title", "target", "severity", "created_at")
    list_filter = ("severity",)
    search_fields = ("title", "detail", "target__domain")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at",)


@admin.register(Scan)
class ScanAdmin(admin.ModelAdmin):
    list_display = ("id", "target", "tool", "preset", "status", "truncated", "created_at")
    list_filter = ("tool", "status", "truncated")
    search_fields = ("command", "output", "error", "target__domain")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at",)


@admin.register(ProviderConfig)
class ProviderConfigAdmin(admin.ModelAdmin):
    list_display = ("provider_id", "has_key_display", "base_url", "updated_at")
    search_fields = ("provider_id", "base_url")
    readonly_fields = ("created_at", "updated_at")

    @admin.display(boolean=True, description="API Key Configured")
    def has_key_display(self, obj):
        return bool(obj.api_key)
