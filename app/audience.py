from .models import AUDIENCE_RULE_FIELDS, Audience, Rule


def audience_for(machine_id: str) -> str:
    """Classify a machine. Routing rules:

    - machine_id contains "EMP"          → teacher
    - machine_id starts with "MS-"       → middle school
    - machine_id starts with "US-"       → upper school
    - anything else                       → upper school (default)
    """
    mid = machine_id or ""
    if "EMP" in mid:
        return Audience.TEACHER
    if mid.startswith("MS-"):
        return Audience.MIDDLE_SCHOOL
    if mid.startswith("US-"):
        return Audience.UPPER_SCHOOL
    return Audience.UPPER_SCHOOL


def is_recognized_machine_id(machine_id: str) -> bool:
    """True if machine_id has an explicit EMP / MS- / US- marker. Used only for
    UnknownMachine tracking — unrecognized machines still get the upper-school
    ruleset by default; this just surfaces them in the admin UI for triage.
    """
    mid = machine_id or ""
    return "EMP" in mid or mid.startswith("MS-") or mid.startswith("US-")


class UnknownAudience(Exception):
    """Raised when a machine can't be mapped to one of the three rulesets.

    Never returned as an empty ruleset: every sync is a clean sync, so an empty
    download tells the client to drop every rule it holds. A routing bug must
    fail the request instead.
    """


def rules_queryset_for(audience: str):
    field = AUDIENCE_RULE_FIELDS.get(audience)
    if not field:
        raise UnknownAudience(audience)
    return Rule.objects.filter(**{field: True}).order_by("updated_at", "id")
