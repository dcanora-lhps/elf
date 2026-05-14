from django.contrib import admin

from .models import (
    AuxiliaryEvent,
    CleanSyncFlag,
    Event,
    Rule,
    ServerSettings,
    SyncCursor,
    SyncSession,
    UnknownMachine,
)


@admin.register(ServerSettings)
class ServerSettingsAdmin(admin.ModelAdmin):
    list_display = ("monitor_only", "mobileconfig_client_mode", "organization")

    def has_add_permission(self, request):
        return not ServerSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Rule)
class RuleAdmin(admin.ModelAdmin):
    list_display = (
        "identifier",
        "rule_type",
        "policy",
        "applies_to_middle_school",
        "applies_to_upper_school",
        "applies_to_teachers",
        "updated_at",
    )
    list_filter = (
        "rule_type",
        "policy",
        "applies_to_middle_school",
        "applies_to_upper_school",
        "applies_to_teachers",
    )
    search_fields = ("identifier", "custom_msg", "notification_app_name", "comment")
    readonly_fields = ("updated_at",)


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("received_at", "machine_id", "audience", "decision", "file_name", "file_sha256")
    list_filter = ("audience", "decision", "signing_status")
    search_fields = ("machine_id", "file_name", "file_sha256", "signing_id", "team_id", "cdhash")
    date_hierarchy = "received_at"

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(AuxiliaryEvent)
class AuxiliaryEventAdmin(admin.ModelAdmin):
    list_display = ("received_at", "machine_id", "audience", "kind")
    list_filter = ("kind", "audience")
    search_fields = ("machine_id",)
    readonly_fields = ("machine_id", "audience", "kind", "payload", "received_at")

    def has_add_permission(self, request):
        return False


@admin.register(UnknownMachine)
class UnknownMachineAdmin(admin.ModelAdmin):
    list_display = ("machine_id", "hit_count", "first_seen", "last_seen")
    search_fields = ("machine_id", "notes")
    readonly_fields = ("machine_id", "first_seen", "last_seen", "hit_count")


@admin.register(SyncSession)
class SyncSessionAdmin(admin.ModelAdmin):
    list_display = (
        "machine_id",
        "audience",
        "sync_type",
        "client_mode",
        "started_at",
        "completed_at",
        "rules_sent",
        "rules_received",
        "rules_processed",
    )
    list_filter = ("audience", "sync_type", "client_mode")
    search_fields = ("machine_id",)
    readonly_fields = [f.name for f in SyncSession._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(SyncCursor)
class SyncCursorAdmin(admin.ModelAdmin):
    list_display = ("token", "session", "created_at")
    readonly_fields = ("token", "session", "last_rule_pk", "last_updated_at", "created_at")

    def has_add_permission(self, request):
        return False


@admin.register(CleanSyncFlag)
class CleanSyncFlagAdmin(admin.ModelAdmin):
    list_display = ("machine_id", "requested_sync_type", "reason", "set_by", "set_at")
    search_fields = ("machine_id", "reason")
    readonly_fields = ("set_at",)

    def save_model(self, request, obj, form, change):
        if obj.set_by_id is None:
            obj.set_by = request.user
        super().save_model(request, obj, form, change)
