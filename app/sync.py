import logging
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.db.models import F, Q
from django.http import JsonResponse
from django.utils import timezone

from .audience import audience_for, is_recognized_machine_id, rules_queryset_for
from .auth import json_endpoint
from .cursors import get_cursor, new_cursor
from .models import (
    Audience,
    AuxiliaryEvent,
    CLEAN_SYNC_TYPES,
    CleanSyncFlag,
    ClientMode,
    Event,
    Machine,
    MachinePolicy,
    ServerSettings,
    SyncSession,
    SyncType,
    UnknownMachine,
)

log = logging.getLogger("santa.sync")


def _epoch_to_dt(value):
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=dt_timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _track_unknown(machine_id):
    obj, created = UnknownMachine.objects.get_or_create(machine_id=machine_id)
    UnknownMachine.objects.filter(pk=obj.pk).update(
        hit_count=F("hit_count") + 1, last_seen=timezone.now()
    )


_AUDIENCE_CLIENT_MODE_FIELDS = {
    Audience.MIDDLE_SCHOOL: "middle_school_client_mode",
    Audience.UPPER_SCHOOL: "upper_school_client_mode",
    Audience.TEACHER: "teacher_client_mode",
}


def _resolve_client_mode(machine_id, server_settings, audience):
    """Decide which client_mode to push to a client at preflight.

    Precedence: monitor_only (emergency switch) > MachinePolicy override >
    per-audience override > global default.
    """
    if server_settings.monitor_only:
        return ClientMode.MONITOR
    override = (
        MachinePolicy.objects.filter(machine_id=machine_id)
        .exclude(client_mode="")
        .values_list("client_mode", flat=True)
        .first()
    )
    if override:
        return override
    field = _AUDIENCE_CLIENT_MODE_FIELDS.get(audience)
    if field:
        audience_mode = getattr(server_settings, field, "") or ""
        if audience_mode:
            return audience_mode
    return server_settings.default_client_mode


def _current_session(machine_id):
    return (
        SyncSession.objects.filter(machine_id=machine_id, completed_at__isnull=True)
        .order_by("-started_at")
        .first()
    )


_MACHINE_IDENTITY_FIELDS = (
    "machine_owner",
    "primary_user",
    "hostname",
    "serial_num",
    "os_version",
    "os_build",
    "model_identifier",
    "santa_version",
)


def _touch_machine(machine_id, audience, identity):
    """Upsert the Machine roster row at preflight time and bump sync_count."""
    now = timezone.now()
    defaults = {"audience": audience, "last_seen": now, **identity}
    _, created = Machine.objects.get_or_create(
        machine_id=machine_id,
        defaults={**defaults, "first_seen": now, "sync_count": 1},
    )
    if not created:
        Machine.objects.filter(machine_id=machine_id).update(
            sync_count=F("sync_count") + 1, **defaults
        )


@json_endpoint
def preflight(request, body, machine_id):
    audience = audience_for(machine_id)
    if not is_recognized_machine_id(machine_id):
        _track_unknown(machine_id)

    server_settings = ServerSettings.get()

    flag = CleanSyncFlag.objects.filter(machine_id=machine_id).first()
    sync_type = flag.requested_sync_type if flag else SyncType.NORMAL

    client_mode = _resolve_client_mode(machine_id, server_settings, audience)

    identity = {
        "machine_owner": str(body.get("machine_owner") or "")[:255],
        "primary_user": str(body.get("primary_user") or "")[:255],
        "hostname": str(body.get("hostname") or "")[:255],
        "serial_num": str(body.get("serial_num") or "")[:128],
        "os_version": str(body.get("os_version") or "")[:64],
        "os_build": str(body.get("os_build") or "")[:64],
        "model_identifier": str(body.get("model_identifier") or "")[:128],
        "santa_version": str(body.get("santa_version") or "")[:64],
    }
    session = SyncSession.objects.create(
        machine_id=machine_id,
        audience=audience,
        sync_type=sync_type,
        client_mode=client_mode,
        batch_size=settings.SANTA_DEFAULT_BATCH_SIZE,
        client_rules_hash=str(body.get("rules_hash") or ""),
        consumed_clean_flag=bool(flag),
        **identity,
    )
    _touch_machine(machine_id, audience, identity)

    response = {
        "client_mode": session.client_mode,
        "sync_type": session.sync_type,
        "batch_size": session.batch_size,
        "enable_bundles": settings.SANTA_ENABLE_BUNDLES,
        "enable_transitive_rules": settings.SANTA_ENABLE_TRANSITIVE,
        "full_sync_interval": settings.SANTA_FULL_SYNC_INTERVAL_SECONDS,
    }
    if server_settings.allowed_path_regex:
        response["allowed_path_regex"] = server_settings.allowed_path_regex
    for src, dst in _DIALOG_PREFLIGHT_KEYS:
        v = getattr(server_settings, src) or ""
        if v:
            response[dst] = v
    return JsonResponse(response)


# (ServerSettings field name, preflight JSON key). Only fields present in
# northpolesec santa.sync.v1 PreflightResponse — other Santa block-dialog keys
# (unknown_block_message, banned_block_message, mode_notification_*) are
# configuration-profile only and cannot be delivered via sync.
_DIALOG_PREFLIGHT_KEYS = (
    ("event_detail_url", "event_detail_url"),
    ("event_detail_text", "event_detail_text"),
)


_EVENT_DIRECT_FIELDS = (
    "file_sha256",
    "file_path",
    "file_name",
    "decision",
    "executing_user",
    "file_bundle_id",
    "file_bundle_name",
    "file_bundle_path",
    "file_bundle_executable_rel_path",
    "file_bundle_version",
    "file_bundle_version_string",
    "file_bundle_hash",
    "parent_name",
    "signing_id",
    "team_id",
    "cdhash",
    "quarantine_data_url",
    "quarantine_referer_url",
    "quarantine_agent_bundle_id",
    "signing_status",
)
_EVENT_INT_FIELDS = ("file_bundle_hash_millis", "file_bundle_binary_count", "pid", "ppid", "cs_flags")
_EVENT_EPOCH_FIELDS = ("execution_time", "quarantine_timestamp", "secure_signing_time", "signing_time")
_EVENT_LIST_FIELDS = ("logged_in_users", "current_sessions", "signing_chain")


def _build_event(machine_id, audience, raw):
    if not isinstance(raw, dict):
        return None
    fields = {f: (raw.get(f) or "") for f in _EVENT_DIRECT_FIELDS}
    for f in _EVENT_INT_FIELDS:
        v = raw.get(f)
        fields[f] = v if isinstance(v, (int, float)) else None
        if fields[f] is not None:
            fields[f] = int(fields[f])
    for f in _EVENT_EPOCH_FIELDS:
        fields[f] = _epoch_to_dt(raw.get(f))
    for f in _EVENT_LIST_FIELDS:
        v = raw.get(f)
        fields[f] = v if isinstance(v, list) else []
    ent = raw.get("entitlement_info")
    fields["entitlement_info"] = ent if isinstance(ent, dict) else {}
    fields["static_rule"] = bool(raw.get("static_rule"))
    return Event(
        machine_id=machine_id,
        audience=audience,
        raw=raw,
        **fields,
    )


@json_endpoint
def eventupload(request, body, machine_id):
    audience = audience_for(machine_id)
    if not is_recognized_machine_id(machine_id):
        _track_unknown(machine_id)

    events_in = body.get("events") or []
    if isinstance(events_in, list) and events_in:
        rows = [_build_event(machine_id, audience, e) for e in events_in]
        rows = [r for r in rows if r is not None]
        if rows:
            Event.objects.bulk_create(rows, batch_size=200)

    aux_rows = []
    for ae in body.get("audit_events") or []:
        if isinstance(ae, dict):
            aux_rows.append(
                AuxiliaryEvent(
                    machine_id=machine_id,
                    audience=audience,
                    kind=AuxiliaryEvent.KIND_AUDIT,
                    payload=ae,
                )
            )
    for fe in body.get("file_access_events") or []:
        if isinstance(fe, dict):
            aux_rows.append(
                AuxiliaryEvent(
                    machine_id=machine_id,
                    audience=audience,
                    kind=AuxiliaryEvent.KIND_FILE_ACCESS,
                    payload=fe,
                )
            )
    if aux_rows:
        AuxiliaryEvent.objects.bulk_create(aux_rows, batch_size=200)

    return JsonResponse({})


def _serialize_rule(rule):
    out = {
        "identifier": rule.identifier,
        "rule_type": rule.rule_type,
        "policy": rule.policy,
        "creation_time": rule.creation_time.timestamp(),
    }
    if rule.custom_msg:
        out["custom_msg"] = rule.custom_msg
    if rule.custom_url:
        out["custom_url"] = rule.custom_url
    if rule.notification_app_name:
        out["notification_app_name"] = rule.notification_app_name
    if rule.cel_expr:
        out["cel_expr"] = rule.cel_expr
    return out


@json_endpoint
def ruledownload(request, body, machine_id):
    audience = audience_for(machine_id)
    if not is_recognized_machine_id(machine_id):
        _track_unknown(machine_id)

    cursor_token = (body.get("cursor") or "").strip()
    cursor = get_cursor(cursor_token) if cursor_token else None

    if cursor:
        session = cursor.session
    else:
        session = _current_session(machine_id)
        if session is None:
            log.warning("ruledownload without preflight for %s", machine_id)
            session = SyncSession.objects.create(
                machine_id=machine_id,
                audience=audience,
                sync_type=SyncType.NORMAL,
                client_mode=_resolve_client_mode(machine_id, ServerSettings.get(), audience),
                batch_size=settings.SANTA_DEFAULT_BATCH_SIZE,
            )

    if ServerSettings.get().monitor_only:
        return JsonResponse({"rules": [], "cursor": ""})

    qs = rules_queryset_for(audience)
    if cursor:
        qs = qs.filter(
            Q(updated_at__gt=cursor.last_updated_at)
            | (Q(updated_at=cursor.last_updated_at) & Q(id__gt=cursor.last_rule_pk))
        )

    batch = list(qs[: session.batch_size + 1])
    has_more = len(batch) > session.batch_size
    page = batch[: session.batch_size]

    next_token = ""
    if has_more:
        next_cursor = new_cursor(session, page[-1])
        next_token = next_cursor.token

    if cursor:
        cursor.delete()

    SyncSession.objects.filter(pk=session.pk).update(rules_sent=F("rules_sent") + len(page))

    return JsonResponse({"rules": [_serialize_rule(r) for r in page], "cursor": next_token})


@json_endpoint
def postflight(request, body, machine_id):
    audience = audience_for(machine_id)
    if not is_recognized_machine_id(machine_id):
        _track_unknown(machine_id)

    session = _current_session(machine_id)
    if session is None:
        log.warning("postflight without active session for %s", machine_id)
        return JsonResponse({})

    received = int(body.get("rules_received") or 0)
    processed = int(body.get("rules_processed") or 0)
    completed_sync_type = str(body.get("sync_type") or "")
    rules_hash = str(body.get("rules_hash") or "")

    now = timezone.now()
    session.rules_received = received
    session.rules_processed = processed
    session.postflight_sync_type = completed_sync_type
    session.final_rules_hash = rules_hash
    session.completed_at = now
    session.save()

    Machine.objects.filter(machine_id=machine_id).update(last_completed=now)

    SyncCursor = session.cursors.model
    SyncCursor.objects.filter(session=session).delete()

    if session.consumed_clean_flag and completed_sync_type in CLEAN_SYNC_TYPES:
        CleanSyncFlag.objects.filter(machine_id=machine_id).delete()

    return JsonResponse({})
