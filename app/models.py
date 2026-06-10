import uuid

from django.conf import settings
from django.db import models
from django.db.models import CheckConstraint, Q, UniqueConstraint
from django.utils import timezone


class RuleType(models.TextChoices):
    BINARY = "BINARY"
    CERTIFICATE = "CERTIFICATE"
    SIGNINGID = "SIGNINGID"
    TEAMID = "TEAMID"
    CDHASH = "CDHASH"


class Policy(models.TextChoices):
    ALLOWLIST = "ALLOWLIST"
    ALLOWLIST_COMPILER = "ALLOWLIST_COMPILER"
    BLOCKLIST = "BLOCKLIST"
    SILENT_BLOCKLIST = "SILENT_BLOCKLIST"
    REMOVE = "REMOVE"
    CEL = "CEL"


class ClientMode(models.TextChoices):
    MONITOR = "MONITOR"
    LOCKDOWN = "LOCKDOWN"
    STANDALONE = "STANDALONE"


class SyncType(models.TextChoices):
    NORMAL = "NORMAL"
    CLEAN = "CLEAN"
    CLEAN_ALL = "CLEAN_ALL"
    CLEAN_STANDALONE = "CLEAN_STANDALONE"
    CLEAN_RULES = "CLEAN_RULES"
    CLEAN_FILE_ACCESS_RULES = "CLEAN_FILE_ACCESS_RULES"


CLEAN_SYNC_TYPES = {
    SyncType.CLEAN,
    SyncType.CLEAN_ALL,
    SyncType.CLEAN_STANDALONE,
    SyncType.CLEAN_RULES,
    SyncType.CLEAN_FILE_ACCESS_RULES,
}


class Audience(models.TextChoices):
    TEACHER = "teacher"
    MIDDLE_SCHOOL = "middle_school"
    UPPER_SCHOOL = "upper_school"


class Rule(models.Model):
    identifier = models.CharField(
        max_length=255,
        db_index=True,
        help_text=(
            "The value being matched. Format depends on Rule type: "
            "BINARY → 64-char SHA-256 of the file; "
            "CERTIFICATE → 64-char SHA-256 of the leaf signing certificate; "
            "TEAMID → 10-char Apple Developer Team ID (e.g. EQHXZ8M8AV); "
            "SIGNINGID → TeamID:bundle-id (e.g. EQHXZ8M8AV:com.google.Chrome), "
            "or platform:&lt;name&gt; for macOS-bundled binaries; "
            "CDHASH → 40-char hex code-directory hash from the signature."
        ),
    )
    rule_type = models.CharField(
        max_length=16,
        choices=RuleType.choices,
        help_text=(
            "How the identifier is interpreted. "
            "BINARY = exact file (very narrow). "
            "CERTIFICATE = anything signed by that cert. "
            "TEAMID = anything signed by that Apple Developer Team (survives cert rotation, broadest). "
            "SIGNINGID = a specific signed binary by its signing ID (narrower than TEAMID). "
            "CDHASH = a specific signed binary version (tightest signed-binary match)."
        ),
    )
    policy = models.CharField(
        max_length=24,
        choices=Policy.choices,
        help_text=(
            "What Santa does on match. "
            "ALLOWLIST = allow. "
            "ALLOWLIST_COMPILER = allow and treat outputs of this binary as locally allowed "
            "(needs transitive rules enabled; use sparingly). "
            "BLOCKLIST = deny and show the block dialog. "
            "SILENT_BLOCKLIST = deny with no dialog. "
            "REMOVE = delete any previously-synced rule with the same identifier and type. "
            "CEL = evaluate the CEL expression below at decision time."
        ),
    )
    custom_msg = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text=(
            "Optional. Shown to the user in the block dialog when this rule fires. "
            "Overrides Santa's default block message. Used only for BLOCK_* policies."
        ),
    )
    custom_url = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text=(
            "Optional. URL behind the \"Open\" button on the block dialog. "
            "Supports Santa placeholders like %file_sha%, %machine_id%, %username%."
        ),
    )
    notification_app_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=(
            "Optional. When this rule un-blocks a previously-blocked app, Santa shows a "
            "\"now allowed\" notification using this name. Set it to the user-visible app name "
            "(e.g. \"Slack\")."
        ),
    )
    cel_expr = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Required only when Policy = CEL. A Common Expression Language expression evaluated "
            "at execution time; must return ALLOWLIST/BLOCKLIST or a boolean. Leave blank for "
            "non-CEL policies."
        ),
    )
    applies_to_middle_school = models.BooleanField(
        default=False,
        help_text='Send this rule to machines whose machine_id starts with "MS-".',
    )
    applies_to_upper_school = models.BooleanField(
        default=False,
        help_text=(
            'Send this rule to machines whose machine_id starts with "US-" (also the default '
            "audience for unrecognized machines)."
        ),
    )
    applies_to_teachers = models.BooleanField(
        default=False,
        help_text=(
            'Send this rule to machines whose machine_id contains "EMP". '
            "(At least one audience must be checked.)"
        ),
    )
    comment = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Internal notes for admins (why this rule exists, ticket links, etc.). "
            "Never sent to Santa clients."
        ),
    )
    creation_time = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["identifier", "rule_type"], name="uniq_identifier_type"),
            CheckConstraint(
                condition=(
                    Q(applies_to_middle_school=True)
                    | Q(applies_to_upper_school=True)
                    | Q(applies_to_teachers=True)
                ),
                name="rule_has_audience",
            ),
        ]
        indexes = [
            models.Index(fields=["updated_at", "id"]),
            models.Index(fields=["applies_to_middle_school"]),
            models.Index(fields=["applies_to_upper_school"]),
            models.Index(fields=["applies_to_teachers"]),
        ]
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.rule_type}:{self.policy}:{self.identifier[:40]}"


class Event(models.Model):
    machine_id = models.CharField(max_length=255, db_index=True)
    audience = models.CharField(max_length=16, choices=Audience.choices, db_index=True)

    file_sha256 = models.CharField(max_length=64, blank=True, default="", db_index=True)
    file_path = models.CharField(max_length=1024, blank=True, default="")
    file_name = models.CharField(max_length=512, blank=True, default="", db_index=True)
    decision = models.CharField(max_length=48, blank=True, default="", db_index=True)
    executing_user = models.CharField(max_length=128, blank=True, default="")
    execution_time = models.DateTimeField(null=True, blank=True, db_index=True)
    logged_in_users = models.JSONField(default=list, blank=True)
    current_sessions = models.JSONField(default=list, blank=True)

    file_bundle_id = models.CharField(max_length=255, blank=True, default="")
    file_bundle_name = models.CharField(max_length=255, blank=True, default="")
    file_bundle_path = models.CharField(max_length=1024, blank=True, default="")
    file_bundle_executable_rel_path = models.CharField(max_length=1024, blank=True, default="")
    file_bundle_version = models.CharField(max_length=64, blank=True, default="")
    file_bundle_version_string = models.CharField(max_length=64, blank=True, default="")
    file_bundle_hash = models.CharField(max_length=64, blank=True, default="")
    file_bundle_hash_millis = models.IntegerField(null=True, blank=True)
    file_bundle_binary_count = models.IntegerField(null=True, blank=True)

    pid = models.IntegerField(null=True, blank=True)
    ppid = models.IntegerField(null=True, blank=True)
    parent_name = models.CharField(max_length=255, blank=True, default="")

    signing_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    team_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    cdhash = models.CharField(max_length=64, blank=True, default="", db_index=True)

    quarantine_data_url = models.CharField(max_length=1024, blank=True, default="")
    quarantine_referer_url = models.CharField(max_length=1024, blank=True, default="")
    quarantine_timestamp = models.DateTimeField(null=True, blank=True)
    quarantine_agent_bundle_id = models.CharField(max_length=255, blank=True, default="")

    signing_chain = models.JSONField(default=list, blank=True)
    entitlement_info = models.JSONField(default=dict, blank=True)
    cs_flags = models.BigIntegerField(null=True, blank=True)
    signing_status = models.CharField(max_length=32, blank=True, default="")
    secure_signing_time = models.DateTimeField(null=True, blank=True)
    signing_time = models.DateTimeField(null=True, blank=True)
    static_rule = models.BooleanField(default=False)

    raw = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["machine_id", "received_at"]),
            models.Index(fields=["audience", "decision"]),
        ]

    def __str__(self):
        return f"{self.received_at:%Y-%m-%d %H:%M} {self.decision} {self.file_name}"


class AuxiliaryEvent(models.Model):
    KIND_AUDIT = "audit"
    KIND_FILE_ACCESS = "file_access"
    KIND_CHOICES = [(KIND_AUDIT, "audit"), (KIND_FILE_ACCESS, "file_access")]

    machine_id = models.CharField(max_length=255, db_index=True)
    audience = models.CharField(max_length=16, choices=Audience.choices, db_index=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, db_index=True)
    payload = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-received_at"]


class Machine(models.Model):
    """Roster row, one per machine_id. Maintained by sync preflight/postflight.

    Identity fields cache the most recent values reported in preflight so the
    Machines tab can render without aggregating SyncSession on every request.
    """

    machine_id = models.CharField(max_length=255, unique=True)
    audience = models.CharField(max_length=16, choices=Audience.choices, db_index=True)

    machine_owner = models.CharField(max_length=255, blank=True, default="", db_index=True)
    primary_user = models.CharField(max_length=255, blank=True, default="")
    hostname = models.CharField(max_length=255, blank=True, default="")
    serial_num = models.CharField(max_length=128, blank=True, default="")
    os_version = models.CharField(max_length=64, blank=True, default="")
    os_build = models.CharField(max_length=64, blank=True, default="")
    model_identifier = models.CharField(max_length=128, blank=True, default="")
    santa_version = models.CharField(max_length=64, blank=True, default="")

    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now, db_index=True)
    last_completed = models.DateTimeField(null=True, blank=True)
    sync_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-last_seen"]
        indexes = [
            models.Index(fields=["-last_seen"]),
            models.Index(fields=["-sync_count"]),
        ]

    def __str__(self):
        return self.machine_owner or self.hostname or self.machine_id


class UnknownMachine(models.Model):
    machine_id = models.CharField(max_length=255, unique=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    hit_count = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-last_seen"]

    def __str__(self):
        return self.machine_id


class SyncSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    machine_id = models.CharField(max_length=255, db_index=True)
    audience = models.CharField(max_length=16, choices=Audience.choices)
    sync_type = models.CharField(max_length=32, choices=SyncType.choices, default=SyncType.NORMAL)
    client_mode = models.CharField(max_length=16, choices=ClientMode.choices, default=ClientMode.MONITOR)
    batch_size = models.PositiveIntegerField(default=50)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    rules_sent = models.PositiveIntegerField(default=0)
    rules_received = models.PositiveIntegerField(default=0)
    rules_processed = models.PositiveIntegerField(default=0)
    postflight_sync_type = models.CharField(max_length=32, blank=True, default="")
    client_rules_hash = models.CharField(max_length=128, blank=True, default="")
    final_rules_hash = models.CharField(max_length=128, blank=True, default="")
    consumed_clean_flag = models.BooleanField(default=False)
    # Identity reported by the Santa client in preflight. machine_owner is the
    # configured Configuration Profile owner; primary_user is the logged-in user.
    machine_owner = models.CharField(max_length=255, blank=True, default="", db_index=True)
    primary_user = models.CharField(max_length=255, blank=True, default="")
    hostname = models.CharField(max_length=255, blank=True, default="")
    serial_num = models.CharField(max_length=128, blank=True, default="")
    os_version = models.CharField(max_length=64, blank=True, default="")
    os_build = models.CharField(max_length=64, blank=True, default="")
    model_identifier = models.CharField(max_length=128, blank=True, default="")
    santa_version = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["machine_id", "completed_at"]),
        ]


class SyncCursor(models.Model):
    token = models.CharField(max_length=64, primary_key=True)
    session = models.ForeignKey(SyncSession, on_delete=models.CASCADE, related_name="cursors")
    last_rule_pk = models.BigIntegerField()
    last_updated_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)


class ServerSettings(models.Model):
    """Singleton row controlling cross-cutting server behavior. Use ServerSettings.get()."""

    SINGLETON_ID = 1

    id = models.PositiveIntegerField(primary_key=True, default=SINGLETON_ID, editable=False)
    default_client_mode = models.CharField(
        max_length=16,
        choices=ClientMode.choices,
        default=ClientMode.MONITOR,
        help_text=(
            "Fallback mode pushed to clients when neither a per-machine override nor "
            "an audience-level override applies. Per-machine overrides are set from "
            "each machine's detail page."
        ),
    )
    middle_school_client_mode = models.CharField(
        max_length=16,
        choices=ClientMode.choices,
        blank=True,
        default="",
        help_text="Mode for MS-* machines. Leave blank to fall back to the global default.",
    )
    upper_school_client_mode = models.CharField(
        max_length=16,
        choices=ClientMode.choices,
        blank=True,
        default="",
        help_text=(
            "Mode for US-* machines (and unrecognized machine IDs, which route to "
            "upper school). Leave blank to fall back to the global default."
        ),
    )
    teacher_client_mode = models.CharField(
        max_length=16,
        choices=ClientMode.choices,
        blank=True,
        default="",
        help_text="Mode for machines whose ID contains \"EMP\". Leave blank to fall back to the global default.",
    )
    monitor_only = models.BooleanField(
        default=False,
        help_text=(
            "Emergency switch. When on, sync forces MONITOR and ruledownload returns no rules — "
            "overrides both the global default and any per-machine settings."
        ),
    )
    allowed_path_regex = models.CharField(
        max_length=1024,
        blank=True,
        default=r"^/(?:System|usr/(?:bin|libexec|sbin))/",
        help_text=(
            "ICU regex. Executions from paths matching this pattern are allowed when no "
            "other rule matches. Emitted as <code>allowed_path_regex</code> in preflight "
            "responses. Leave blank to omit. Note: anything writable into these paths "
            "effectively bypasses Santa, so keep it tight."
        ),
    )
    # Block-dialog customization pushed via preflight. Per-rule custom_msg/custom_url
    # still wins when set on a matching Rule.
    event_detail_url = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text=(
            'URL template for the "Open Event" button in the block dialog. Supports Santa '
            "placeholders: <code>%machine_id%</code>, <code>%username%</code>, "
            "<code>%file_identifier%</code>, <code>%file_bundle_id%</code>, "
            "<code>%signing_id%</code>, <code>%team_id%</code>. Leave blank to hide the button."
        ),
    )
    event_detail_text = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text='Label for the "Open Event" button. Defaults to Santa\'s built-in text when blank.',
    )

    class Meta:
        verbose_name = "Server settings"
        verbose_name_plural = "Server settings"

    def save(self, *args, **kwargs):
        self.id = self.SINGLETON_ID
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Singleton cannot be deleted.
        return

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(id=cls.SINGLETON_ID)
        return obj

    def __str__(self):
        return "Server settings"


class MachinePolicy(models.Model):
    """Per-machine overrides applied at preflight. Currently only client_mode."""

    machine_id = models.CharField(max_length=255, unique=True)
    client_mode = models.CharField(
        max_length=16,
        choices=ClientMode.choices,
        blank=True,
        default="",
        help_text="Leave blank to use the global default.",
    )
    notes = models.CharField(max_length=255, blank=True, default="")
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["machine_id"]

    def __str__(self):
        return f"{self.machine_id} → {self.client_mode or 'default'}"


class CleanSyncFlag(models.Model):
    machine_id = models.CharField(max_length=255, unique=True)
    requested_sync_type = models.CharField(
        max_length=32,
        choices=[(t.value, t.label) for t in SyncType if t.value in {s.value for s in (SyncType.CLEAN, SyncType.CLEAN_ALL, SyncType.CLEAN_STANDALONE, SyncType.CLEAN_RULES, SyncType.CLEAN_FILE_ACCESS_RULES)}],
        default=SyncType.CLEAN,
    )
    reason = models.CharField(max_length=255, blank=True, default="")
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    set_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-set_at"]
