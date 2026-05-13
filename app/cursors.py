import secrets

from .models import SyncCursor


def new_cursor(session, last_rule):
    return SyncCursor.objects.create(
        token=secrets.token_urlsafe(32),
        session=session,
        last_rule_pk=last_rule.id,
        last_updated_at=last_rule.updated_at,
    )


def get_cursor(token):
    if not token:
        return None
    try:
        return SyncCursor.objects.select_related("session").get(token=token)
    except SyncCursor.DoesNotExist:
        return None
