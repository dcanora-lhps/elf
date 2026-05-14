from django.db import migrations, models
from django.db.models import Q


def backfill_rule_audiences(apps, schema_editor):
    Rule = apps.get_model("app", "Rule")
    Rule.objects.filter(applies_to_students=True).update(
        applies_to_middle_school=True,
        applies_to_upper_school=True,
    )


def backfill_event_audiences(apps, schema_editor):
    """Existing student-tagged rows had no MS/US distinction. The new default for
    unrecognized machines is upper_school, so we map "student" → "upper_school".
    """
    Event = apps.get_model("app", "Event")
    AuxiliaryEvent = apps.get_model("app", "AuxiliaryEvent")
    SyncSession = apps.get_model("app", "SyncSession")
    Event.objects.filter(audience="student").update(audience="upper_school")
    AuxiliaryEvent.objects.filter(audience="student").update(audience="upper_school")
    SyncSession.objects.filter(audience="student").update(audience="upper_school")


def noop_reverse(apps, schema_editor):
    # Cannot reverse: we lose the original MS/US distinction once collapsed back.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0007_alter_auxiliaryevent_audience_alter_event_audience_and_more"),
    ]

    operations = [
        # 1) Add the new rule audience flags.
        migrations.AddField(
            model_name="rule",
            name="applies_to_middle_school",
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Send this rule to machines whose machine_id starts with "MS-", and '
                    "include it in the Middle School .mobileconfig download."
                ),
            ),
        ),
        migrations.AddField(
            model_name="rule",
            name="applies_to_upper_school",
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Send this rule to machines whose machine_id starts with "US-" (also '
                    "the default audience for unrecognized machines), and include it in "
                    "the Upper School .mobileconfig download."
                ),
            ),
        ),

        # 2) Backfill.
        migrations.RunPython(backfill_rule_audiences, noop_reverse),
        migrations.RunPython(backfill_event_audiences, noop_reverse),

        # 3) Drop old constraint (it references applies_to_students).
        migrations.RemoveConstraint(
            model_name="rule",
            name="rule_has_audience",
        ),

        # 4) Drop old field + index.
        migrations.RemoveIndex(
            model_name="rule",
            name="app_rule_applies_f41a3b_idx",
        ),
        migrations.RemoveField(
            model_name="rule",
            name="applies_to_students",
        ),

        # 5) Update teachers help_text wording.
        migrations.AlterField(
            model_name="rule",
            name="applies_to_teachers",
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Send this rule to machines whose machine_id contains "EMP", and include '
                    "it in the Teachers .mobileconfig download. (At least one audience must "
                    "be checked.)"
                ),
            ),
        ),

        # 6) Add new constraint + indexes.
        migrations.AddConstraint(
            model_name="rule",
            constraint=models.CheckConstraint(
                condition=(
                    Q(applies_to_middle_school=True)
                    | Q(applies_to_upper_school=True)
                    | Q(applies_to_teachers=True)
                ),
                name="rule_has_audience",
            ),
        ),
        migrations.AddIndex(
            model_name="rule",
            index=models.Index(
                fields=["applies_to_middle_school"], name="app_rule_applies_4d28bc_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="rule",
            index=models.Index(
                fields=["applies_to_upper_school"], name="app_rule_applies_37586d_idx"
            ),
        ),

        # 7) Update audience choices on Event / AuxiliaryEvent / SyncSession.
        migrations.AlterField(
            model_name="event",
            name="audience",
            field=models.CharField(
                choices=[
                    ("teacher", "Teacher"),
                    ("middle_school", "Middle School"),
                    ("upper_school", "Upper School"),
                ],
                db_index=True,
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="auxiliaryevent",
            name="audience",
            field=models.CharField(
                choices=[
                    ("teacher", "Teacher"),
                    ("middle_school", "Middle School"),
                    ("upper_school", "Upper School"),
                ],
                db_index=True,
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="syncsession",
            name="audience",
            field=models.CharField(
                choices=[
                    ("teacher", "Teacher"),
                    ("middle_school", "Middle School"),
                    ("upper_school", "Upper School"),
                ],
                max_length=16,
            ),
        ),
    ]
