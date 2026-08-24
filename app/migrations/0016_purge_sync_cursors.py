from django.db import migrations


def forwards(apps, schema_editor):
    """Empty the cursor table before 0017 makes (session, last_rule_pk) unique.

    Cursors used to be one-shot tickets deleted as soon as a page was served,
    and a client retrying a request it had already been answered made the
    server restart the download and mint a second cursor at the same position.
    So existing rows can hold duplicate (session, last_rule_pk) pairs that the
    new constraint would reject, failing the migration partway through.

    Dropping them costs nothing: they carry no `sent_before` tally and cannot
    be resumed under the new scheme anyway. A client mid-download during the
    deploy gets a 500 on its next page, abandons that sync with its rules
    untouched, and starts over on the next check-in.
    """
    apps.get_model("app", "SyncCursor").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0015_machine"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop, elidable=True),
    ]
