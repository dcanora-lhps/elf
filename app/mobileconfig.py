"""Generate Santa configuration profiles (.mobileconfig) per audience.

References:
- NPS Santa profile schema: docs/docs/deployment/profile-configuration.md in northpolesec/santa
- Rule dict keys: Source/common/SNTSyncConstants.mm (identifier, policy, rule_type, custom_msg, custom_url)
- ClientMode integer values: SNTClientMode enum (1=Monitor, 2=Lockdown, 3=Standalone)
"""

import plistlib
import uuid

from .audience import rules_queryset_for
from .models import Audience, Policy, ServerSettings


SANTA_PAYLOAD_TYPE = "com.northpolesec.santa"

AUDIENCE_LABELS = {
    Audience.MIDDLE_SCHOOL: "Middle School",
    Audience.UPPER_SCHOOL: "Upper School",
    Audience.TEACHER: "Teachers",
}

# Policies that don't translate to StaticRules.
#   REMOVE: only meaningful for sync (it deletes a previously synced rule); a static rule
#           can't be "removed" because it's part of the profile itself.
EXCLUDED_POLICIES = {Policy.REMOVE}


def _static_rules_for(audience):
    rules = []
    skipped_remove = 0
    skipped_cel_no_expr = 0
    for r in rules_queryset_for(audience):
        if r.policy in EXCLUDED_POLICIES:
            skipped_remove += 1
            continue
        if r.policy == Policy.CEL and not r.cel_expr:
            skipped_cel_no_expr += 1
            continue
        d = {
            "identifier": r.identifier,
            "policy": r.policy,
            "rule_type": r.rule_type,
        }
        if r.custom_msg:
            d["custom_msg"] = r.custom_msg
        if r.custom_url:
            d["custom_url"] = r.custom_url
        rules.append(d)
    return rules, {"skipped_remove": skipped_remove, "skipped_cel_no_expr": skipped_cel_no_expr}


def build_mobileconfig(audience):
    """Return (xml_bytes, stats_dict) for the given audience.

    audience: one of Audience.MIDDLE_SCHOOL, Audience.UPPER_SCHOOL, Audience.TEACHER.
    """
    if audience not in AUDIENCE_LABELS:
        raise ValueError(f"unsupported audience: {audience}")

    settings = ServerSettings.get()
    label = AUDIENCE_LABELS[audience]
    prefix = settings.payload_identifier_prefix.rstrip(".")
    profile_uuid = str(uuid.uuid4())
    santa_payload_uuid = str(uuid.uuid4())
    rules, stats = _static_rules_for(audience)

    santa_payload = {
        "PayloadType": SANTA_PAYLOAD_TYPE,
        "PayloadVersion": 1,
        "PayloadIdentifier": f"{prefix}.{audience}.{santa_payload_uuid}",
        "PayloadUUID": santa_payload_uuid,
        "PayloadDisplayName": f"Santa Configuration — {label}",
        "ClientMode": int(settings.mobileconfig_client_mode),
        "StaticRules": rules,
    }
    if settings.allowed_path_regex:
        santa_payload["AllowedPathRegex"] = settings.allowed_path_regex

    profile = {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": f"{prefix}.{audience}",
        "PayloadUUID": profile_uuid,
        "PayloadDisplayName": f"Santa — {label}",
        "PayloadDescription": f"Santa configuration and static rules for {label.lower()}.",
        "PayloadOrganization": settings.organization,
        "PayloadScope": "System",
        "PayloadRemovalDisallowed": True,
        "PayloadContent": [santa_payload],
    }

    stats["rule_count"] = len(rules)
    return plistlib.dumps(profile, fmt=plistlib.FMT_XML, sort_keys=True), stats
