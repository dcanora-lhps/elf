from .models import Audience, Rule


def audience_for(machine_id: str) -> str:
    """Classify a machine. Students are the default — only machines explicitly marked
    with "EMP" in their machine_id are treated as teachers; everyone else is STUDENT.
    """
    if "EMP" in (machine_id or ""):
        return Audience.TEACHER
    return Audience.STUDENT


def is_recognized_machine_id(machine_id: str) -> bool:
    """True if machine_id has an explicit EMP or STU marker. Used only for
    UnknownMachine tracking — these machines still get the student ruleset by
    default; this just surfaces them in the admin UI for triage.
    """
    mid = machine_id or ""
    return "EMP" in mid or "STU" in mid


def rules_queryset_for(audience: str):
    qs = Rule.objects.all().order_by("updated_at", "id")
    if audience == Audience.TEACHER:
        return qs.filter(applies_to_teachers=True)
    return qs.filter(applies_to_students=True)
