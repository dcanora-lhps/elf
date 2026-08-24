from datetime import timedelta

from django.db import migrations
from django.utils import timezone

# Every sync is a CLEAN sync now, so a queued CLEAN is redundant, and the types
# Santa's client never honored (it downgrades them to NORMAL and reports NORMAL,
# so the request could never clear) become CLEAN_ALL.
SUPERSEDED_CLEAN_TYPES = ["CLEAN", "CLEAN_STANDALONE", "CLEAN_RULES", "CLEAN_FILE_ACCESS_RULES"]

# A sync stage times out client-side after 30s; anything open for an hour is
# a session whose client went away without postflighting.
STALE_AFTER = timedelta(hours=1)


def forwards(apps, schema_editor):
    CleanSyncFlag = apps.get_model("app", "CleanSyncFlag")
    SyncSession = apps.get_model("app", "SyncSession")

    CleanSyncFlag.objects.filter(requested_sync_type__in=SUPERSEDED_CLEAN_TYPES).update(
        requested_sync_type="CLEAN_ALL"
    )

    now = timezone.now()
    SyncSession.objects.filter(
        completed_at__isnull=True, abandoned_at__isnull=True, started_at__lt=now - STALE_AFTER
    ).update(abandoned_at=now)


def backwards(apps, schema_editor):
    SyncSession = apps.get_model("app", "SyncSession")
    SyncSession.objects.filter(abandoned_at__isnull=False).update(abandoned_at=None)


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0017_synccursor_sent_before_syncsession_abandoned_at_and_more"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
