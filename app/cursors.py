import secrets

from .models import SyncCursor


def cursor_for_position(session, last_rule_pk, last_updated_at, sent_before):
    """Token for a resume point, allocated once per (session, position).

    Reusing the row means a client that retries a ruledownload request gets the
    same page and the same next token instead of silently rewinding to the
    start of the ruleset.
    """
    cursor, _ = SyncCursor.objects.get_or_create(
        session=session,
        last_rule_pk=last_rule_pk,
        defaults={
            "token": secrets.token_urlsafe(32),
            "last_updated_at": last_updated_at,
            "sent_before": sent_before,
        },
    )
    return cursor


def get_cursor(token):
    if not token:
        return None
    try:
        return SyncCursor.objects.select_related("session").get(token=token)
    except SyncCursor.DoesNotExist:
        return None
