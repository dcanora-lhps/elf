from .models import Audience, Rule


def audience_for(machine_id: str) -> str:
    # If a machine_id somehow contains both substrings, "EMP" (teacher) wins.
    mid = machine_id or ""
    if "EMP" in mid:
        return Audience.TEACHER
    if "STU" in mid:
        return Audience.STUDENT
    return Audience.UNKNOWN


def rules_queryset_for(audience: str):
    qs = Rule.objects.all().order_by("updated_at", "id")
    if audience == Audience.TEACHER:
        return qs.filter(applies_to_teachers=True)
    if audience == Audience.STUDENT:
        return qs.filter(applies_to_students=True)
    return Rule.objects.none()
