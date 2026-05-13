from django.db import migrations


def backfill(apps, schema_editor):
    Event = apps.get_model("app", "Event")
    AuxiliaryEvent = apps.get_model("app", "AuxiliaryEvent")
    SyncSession = apps.get_model("app", "SyncSession")
    Event.objects.filter(audience="unknown").update(audience="student")
    AuxiliaryEvent.objects.filter(audience="unknown").update(audience="student")
    SyncSession.objects.filter(audience="unknown").update(audience="student")


def noop_reverse(apps, schema_editor):
    # No reverse: we can't tell which of the now-student rows used to be unknown.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0005_serversettings_allowed_path_regex"),
    ]

    operations = [
        migrations.RunPython(backfill, noop_reverse),
    ]
