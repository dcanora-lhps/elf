"""Tests for the Santa sync endpoints.

The sync flow is exercised through the real HTTP endpoints so the auth
decorator, JSON handling and session bookkeeping are all in the loop.

Every sync this server hands out is a CLEAN sync: the client drops the rules it
holds and applies exactly what the download delivers. That makes two properties
load-bearing, and most of what follows tests them — a download must never be
silently short, and it must never be silently repeated.
"""

import json
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from .audience import UnknownAudience, rules_queryset_for
from .models import (
    Audience,
    Event,
    CleanSyncFlag,
    ClientMode,
    Machine,
    Policy,
    Rule,
    RuleType,
    ServerSettings,
    SyncCursor,
    SyncSession,
    SyncType,
)

TOKEN = "test-sync-token"


@override_settings(SANTA_SYNC_TOKEN=TOKEN, SANTA_DEFAULT_BATCH_SIZE=5)
class SyncTestCase(TestCase):
    """Helpers for driving the sync stages as a Santa client would."""

    def post(self, stage, machine_id, payload=None):
        return self.client.post(
            f"/{stage}/{machine_id}",
            data=json.dumps(payload or {}),
            content_type="application/json",
            headers={"authorization": f"Bearer {TOKEN}"},
        )

    def json(self, stage, machine_id, payload=None):
        response = self.post(stage, machine_id, payload)
        self.assertEqual(response.status_code, 200, response.content)
        return json.loads(response.content)

    def make_rule(self, identifier, **flags):
        scope = {
            "applies_to_middle_school": False,
            "applies_to_upper_school": False,
            "applies_to_teachers": False,
        }
        scope.update(flags)
        return Rule.objects.create(
            identifier=identifier,
            rule_type=RuleType.BINARY,
            policy=Policy.ALLOWLIST,
            **scope,
        )

    def download_all(self, machine_id):
        """Run the ruledownload stage to completion, returning every rule sent."""
        rules, cursor, pages = [], "", 0
        while True:
            body = self.json("ruledownload", machine_id, {"cursor": cursor} if cursor else {})
            rules.extend(body["rules"])
            pages += 1
            cursor = body["cursor"]
            if not cursor:
                return rules, pages
            self.assertLess(pages, 50, "ruledownload did not terminate")

    def session(self, machine_id="US-1"):
        return SyncSession.objects.filter(machine_id=machine_id).latest("started_at")

    def login(self):
        User.objects.create_superuser("admin", "a@example.com", "pw")
        self.client.force_login(User.objects.get(username="admin"))


class EventUploadSettingsTests(SyncTestCase):
    """Preflight must always state both event-upload switches.

    Santa only overwrites a sync-state value when the key is present in the
    preflight response, and sync state outranks the configuration profile. A
    key we omit keeps whatever the client last latched onto, so a machine that
    ever received DisableUnknownEventUpload=true would stop uploading
    ALLOW_UNKNOWN events permanently, with no way for this server to undo it.
    """

    def test_preflight_always_sends_both_keys(self):
        body = self.json("preflight", "US-1")
        self.assertIn("disableUnknownEventUpload", body)
        self.assertIn("enableAllEventUpload", body)

    def test_defaults_keep_unknown_events_flowing(self):
        body = self.json("preflight", "US-1")
        self.assertIs(body["disableUnknownEventUpload"], False)
        self.assertIs(body["enableAllEventUpload"], False)

    def test_false_is_sent_explicitly_not_omitted(self):
        # The whole point: a false value must travel as false, never as an
        # absent key, or it cannot clear a stale client-side setting.
        settings_obj = ServerSettings.get()
        settings_obj.disable_unknown_event_upload = False
        settings_obj.enable_all_event_upload = False
        settings_obj.save()
        raw = json.loads(self.post("preflight", "US-1").content)
        self.assertEqual(raw["disableUnknownEventUpload"], False)
        self.assertEqual(raw["enableAllEventUpload"], False)

    def test_settings_are_reflected_when_enabled(self):
        settings_obj = ServerSettings.get()
        settings_obj.disable_unknown_event_upload = True
        settings_obj.enable_all_event_upload = True
        settings_obj.save()
        body = self.json("preflight", "US-1")
        self.assertIs(body["disableUnknownEventUpload"], True)
        self.assertIs(body["enableAllEventUpload"], True)


class EventIngestTests(SyncTestCase):
    """Decoding one uploaded event into an Event row.

    Santa 2026.5+ sends a mix of wire spellings in a single event, and Postgres
    rejects an overlong value rather than truncating it — with bulk_create that
    would fail the whole batch and lose every event in the upload.
    """

    def upload(self, event):
        self.json("eventupload", "US-1", {"events": [event]})
        return Event.objects.latest("received_at")

    def test_camel_case_fields_are_accepted(self):
        e = self.upload(
            {
                "decision": "ALLOW_UNKNOWN",
                "csFlags": 637631233,
                "signingTime": 1785586260,
                "signingStatus": "SIGNING_STATUS_PRODUCTION",
                "entitlementInfo": {"entitlementsFiltered": True},
            }
        )
        self.assertEqual(e.cs_flags, 637631233)
        self.assertEqual(e.signing_status, "SIGNING_STATUS_PRODUCTION")
        self.assertIsNotNone(e.signing_time)
        self.assertEqual(e.entitlement_info, {"entitlementsFiltered": True})

    def test_snake_case_still_wins(self):
        e = self.upload({"decision": "BLOCK_BINARY", "cs_flags": 7, "csFlags": 9})
        self.assertEqual(e.cs_flags, 7)

    def test_overlong_values_are_truncated_not_rejected(self):
        e = self.upload({"decision": "ALLOW_UNKNOWN", "file_path": "/x" * 2000})
        self.assertEqual(len(e.file_path), 1024)

    def test_raw_payload_is_kept_untruncated(self):
        path = "/x" * 2000
        e = self.upload({"decision": "ALLOW_UNKNOWN", "file_path": path})
        self.assertEqual(e.raw["file_path"], path)

    def test_allow_unknown_round_trips(self):
        e = self.upload({"decision": "ALLOW_UNKNOWN", "file_name": "FileZilla"})
        self.assertEqual(e.decision, "ALLOW_UNKNOWN")
        self.assertEqual(e.file_name, "FileZilla")


class EventUploadLoggingTests(SyncTestCase):
    """The upload path must never accept events silently.

    A 200 tells Santa the events are delivered and it drops them from its local
    queue, so anything we ignore without a log line is unrecoverable.
    """

    def test_logs_keys_and_decisions(self):
        with self.assertLogs("santa.sync", level="INFO") as logs:
            self.json("eventupload", "US-1", {"events": [{"decision": "ALLOW_UNKNOWN"}]})
        line = "\n".join(logs.output)
        self.assertIn("events=1", line)
        self.assertIn("ALLOW_UNKNOWN", line)

    def test_warns_when_client_sends_an_empty_batch(self):
        with self.assertLogs("santa.sync", level="WARNING") as logs:
            self.json("eventupload", "US-1", {"events": []})
        self.assertIn("sent none", "\n".join(logs.output))

    def test_warns_on_unrecognised_top_level_keys(self):
        with self.assertLogs("santa.sync", level="WARNING") as logs:
            self.json("eventupload", "US-1", {"execution_events": [{"decision": "X"}]})
        self.assertIn("execution_events", "\n".join(logs.output))

    def test_known_keys_do_not_warn(self):
        with self.assertLogs("santa.sync", level="INFO") as logs:
            self.json(
                "eventupload",
                "US-1",
                {"machine_id": "US-1", "events": [{"decision": "BLOCK_BINARY"}]},
            )
        self.assertNotIn("unrecognised", "\n".join(logs.output))


class CleanSyncTests(SyncTestCase):
    """Every sync is clean, so scope changes need no server-side bookkeeping."""

    def test_preflight_asks_for_a_clean_sync(self):
        self.assertEqual(self.json("preflight", "US-1")["sync_type"], SyncType.CLEAN)

    def test_preflight_uses_clean_not_clean_all(self):
        # CLEAN_ALL would also drop transitive and standalone-approved rules.
        self.assertNotEqual(self.json("preflight", "US-1")["sync_type"], SyncType.CLEAN_ALL)

    def test_unscoping_an_audience_stops_sending_the_rule(self):
        rule = self.make_rule(
            "aa", applies_to_upper_school=True, applies_to_middle_school=True
        )
        self.json("preflight", "US-1")
        rules, _ = self.download_all("US-1")
        self.assertEqual([r["identifier"] for r in rules], ["aa"])

        rule.applies_to_upper_school = False
        rule.save()

        self.json("preflight", "US-1")
        rules, _ = self.download_all("US-1")
        self.assertEqual(rules, [])

        # Middle school still holds the rule.
        self.json("preflight", "MS-1")
        rules, _ = self.download_all("MS-1")
        self.assertEqual([r["identifier"] for r in rules], ["aa"])

    def test_deleting_a_rule_stops_sending_it(self):
        rule = self.make_rule("aa", applies_to_upper_school=True)
        self.make_rule("bb", applies_to_upper_school=True)
        rule.delete()

        self.json("preflight", "US-1")
        rules, _ = self.download_all("US-1")
        self.assertEqual([r["identifier"] for r in rules], ["bb"])


class LoudFailureTests(SyncTestCase):
    """A short download is applied verbatim, so never return one by accident."""

    def setUp(self):
        for i in range(12):
            self.make_rule(f"rule-{i:02d}", applies_to_upper_school=True)

    def test_unknown_cursor_fails_instead_of_truncating(self):
        self.json("preflight", "US-1")
        self.json("ruledownload", "US-1")

        response = self.post("ruledownload", "US-1", {"cursor": "nope"})

        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.session().rules_sent, 5)

    def test_cursor_from_a_superseded_session_fails(self):
        self.json("preflight", "US-1")
        stale_cursor = self.json("ruledownload", "US-1")["cursor"]
        self.json("preflight", "US-1")  # client restarted; old session is done

        response = self.post("ruledownload", "US-1", {"cursor": stale_cursor})

        self.assertEqual(response.status_code, 500)

    def test_an_unroutable_audience_raises_rather_than_returning_nothing(self):
        # Defensive: audience_for only ever yields the three real audiences, so
        # this guards a future routing change from quietly wiping a fleet.
        with self.assertRaises(UnknownAudience):
            rules_queryset_for("nonsense")

    def test_unroutable_audience_fails_the_download(self):
        self.json("preflight", "US-1")
        with mock.patch("app.sync.audience_for", return_value="nonsense"):
            with self.assertLogs("santa.sync", level="ERROR"):
                response = self.post("ruledownload", "US-1", {})

        self.assertEqual(response.status_code, 500)

    def test_monitor_only_still_returns_an_empty_ruleset(self):
        # Deliberate: with clean syncs this drops every rule on the machine.
        obj = ServerSettings.get()
        obj.monitor_only = True
        obj.save()

        self.json("preflight", "US-1")
        self.assertEqual(self.json("ruledownload", "US-1"), {"rules": [], "cursor": ""})

    def test_an_empty_audience_ruleset_is_served_as_empty(self):
        self.json("preflight", "MS-1")
        self.assertEqual(self.json("ruledownload", "MS-1"), {"rules": [], "cursor": ""})

    def test_ruledownload_without_preflight_serves_the_full_ruleset(self):
        rules, _ = self.download_all("US-1")

        self.assertEqual(len(rules), 12)
        session = self.session()
        self.assertEqual(session.sync_type, SyncType.CLEAN)
        self.assertEqual(session.rules_sent, 12)


class RuleDownloadPagingTests(SyncTestCase):
    """rules_sent must count rules, not retries."""

    def setUp(self):
        for i in range(12):
            self.make_rule(f"rule-{i:02d}", applies_to_upper_school=True)

    def test_paging_sends_each_rule_once(self):
        self.json("preflight", "US-1")
        rules, pages = self.download_all("US-1")

        self.assertEqual(len(rules), 12)
        self.assertEqual(len({r["identifier"] for r in rules}), 12)
        self.assertEqual(pages, 3)  # batch size 5
        self.assertEqual(self.session().rules_sent, 12)

    def test_retrying_a_page_returns_the_same_page(self):
        self.json("preflight", "US-1")
        token = self.json("ruledownload", "US-1")["cursor"]

        second = self.json("ruledownload", "US-1", {"cursor": token})
        # The client's response never arrived, so it retries the same request.
        retry = self.json("ruledownload", "US-1", {"cursor": token})

        self.assertEqual(second, retry)
        self.assertEqual(self.session().rules_sent, 10)

    def test_retrying_the_first_page_does_not_rewind_or_double_count(self):
        self.json("preflight", "US-1")
        first = self.json("ruledownload", "US-1")
        again = self.json("ruledownload", "US-1")

        self.assertEqual(first, again)
        self.assertEqual(self.session().rules_sent, 5)

    def test_a_ruleset_that_fills_the_last_page_exactly_ends_cleanly(self):
        Rule.objects.filter(identifier__gt="rule-09").delete()  # leaves 10, batch 5

        self.json("preflight", "US-1")
        rules, pages = self.download_all("US-1")

        self.assertEqual(len(rules), 10)
        self.assertEqual(pages, 2)

    def test_postflight_clears_the_session_cursors(self):
        self.json("preflight", "US-1")
        self.json("ruledownload", "US-1")
        self.assertTrue(SyncCursor.objects.exists())

        self.json("postflight", "US-1", {"rules_received": 5, "rules_processed": 5,
                                         "sync_type": SyncType.CLEAN})

        self.assertFalse(SyncCursor.objects.exists())


class SessionLifecycleTests(SyncTestCase):
    def test_a_new_preflight_abandons_the_previous_open_session(self):
        self.json("preflight", "US-1")
        first = SyncSession.objects.get()

        self.json("preflight", "US-1")

        first.refresh_from_db()
        self.assertIsNotNone(first.abandoned_at)
        self.assertIsNone(first.completed_at)
        self.assertEqual(
            SyncSession.objects.filter(
                completed_at__isnull=True, abandoned_at__isnull=True
            ).count(),
            1,
        )

    def test_rule_pages_land_on_the_current_session(self):
        self.make_rule("aa", applies_to_upper_school=True)
        self.json("preflight", "US-1")
        stale = SyncSession.objects.get()
        self.json("preflight", "US-1")

        self.download_all("US-1")

        stale.refresh_from_db()
        self.assertEqual(stale.rules_sent, 0)
        self.assertEqual(SyncSession.objects.exclude(pk=stale.pk).get().rules_sent, 1)

    def test_postflight_records_counts(self):
        self.json("preflight", "US-1")
        self.json(
            "postflight",
            "US-1",
            {"rules_received": 7, "rules_processed": 6, "sync_type": SyncType.CLEAN,
             "rules_hash": "abc"},
        )

        session = SyncSession.objects.get()
        self.assertEqual((session.rules_received, session.rules_processed), (7, 6))
        self.assertEqual(session.final_rules_hash, "abc")
        self.assertIsNotNone(session.completed_at)


class CleanAllEscalationTests(SyncTestCase):
    """The queued flag now only escalates CLEAN to CLEAN_ALL."""

    def queue(self, machine_id="US-1"):
        return CleanSyncFlag.objects.create(
            machine_id=machine_id, requested_sync_type=SyncType.CLEAN_ALL
        )

    def test_preflight_escalates_to_clean_all(self):
        self.queue()
        self.assertEqual(self.json("preflight", "US-1")["sync_type"], SyncType.CLEAN_ALL)

    def test_flag_clears_after_the_client_reports_clean_all(self):
        self.queue()
        self.json("preflight", "US-1")
        self.json("postflight", "US-1", {"sync_type": "CLEAN_ALL", "rules_received": 0})

        self.assertFalse(CleanSyncFlag.objects.exists())
        self.assertEqual(SyncSession.objects.get().clean_flag_kept_reason, "")

    def test_flag_clears_on_a_lowercase_sync_type(self):
        self.queue()
        self.json("preflight", "US-1")
        self.json("postflight", "US-1", {"sync_type": "clean_all"})

        self.assertFalse(CleanSyncFlag.objects.exists())

    def test_flag_clears_when_the_client_omits_the_sync_type(self):
        self.queue()
        self.json("preflight", "US-1")
        self.json("postflight", "US-1", {"rules_received": 3})

        self.assertFalse(CleanSyncFlag.objects.exists())

    def test_a_plain_clean_does_not_satisfy_a_clean_all_request(self):
        self.queue()
        self.json("preflight", "US-1")
        self.json("postflight", "US-1", {"sync_type": "CLEAN"})

        self.assertTrue(CleanSyncFlag.objects.exists())
        self.assertIn("CLEAN_ALL", SyncSession.objects.get().clean_flag_kept_reason)

    def test_flag_survives_when_the_client_only_did_a_normal_sync(self):
        self.queue()
        self.json("preflight", "US-1")
        self.json("postflight", "US-1", {"sync_type": "NORMAL"})

        self.assertTrue(CleanSyncFlag.objects.exists())

    def test_an_ordinary_clean_sync_needs_no_flag(self):
        self.json("preflight", "US-1")
        self.json("postflight", "US-1", {"sync_type": "CLEAN"})

        self.assertEqual(SyncSession.objects.get().clean_flag_kept_reason, "")

    def test_a_flag_queued_mid_sync_is_not_swallowed(self):
        self.queue()
        self.json("preflight", "US-1")
        CleanSyncFlag.objects.all().delete()
        later = self.queue()

        self.json("postflight", "US-1", {"sync_type": "CLEAN_ALL"})

        self.assertEqual(CleanSyncFlag.objects.get().pk, later.pk)

    def test_an_unfinished_escalation_stays_queued(self):
        self.queue()
        self.json("preflight", "US-1")
        self.json("preflight", "US-1")  # client gave up and started over

        self.assertEqual(self.json("preflight", "US-1")["sync_type"], SyncType.CLEAN_ALL)


class PreflightTests(SyncTestCase):
    def test_audience_client_mode_override_wins_over_the_default(self):
        settings_obj = ServerSettings.get()
        settings_obj.default_client_mode = ClientMode.MONITOR
        settings_obj.upper_school_client_mode = ClientMode.LOCKDOWN
        settings_obj.save()

        self.assertEqual(self.json("preflight", "US-1")["client_mode"], ClientMode.LOCKDOWN)
        self.assertEqual(self.json("preflight", "MS-1")["client_mode"], ClientMode.MONITOR)

    def test_unrecognized_machine_ids_route_to_upper_school(self):
        self.json("preflight", "laptop-42")
        self.assertEqual(SyncSession.objects.get().audience, Audience.UPPER_SCHOOL)

    def test_requests_without_a_token_are_rejected(self):
        response = self.client.post(
            "/preflight/US-1", data="{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 401)


class MachineViewTests(SyncTestCase):
    def test_machine_page_labels_the_rule_counts(self):
        self.login()
        self.json("preflight", "US-1")
        self.json("postflight", "US-1", {"rules_received": 4, "rules_processed": 4})

        response = self.client.get("/machines/US-1/")

        self.assertContains(response, "Rules received")
        self.assertNotContains(response, "Events received")

    def test_abandoned_sessions_are_labelled(self):
        self.login()
        self.json("preflight", "US-1")
        self.json("preflight", "US-1")

        response = self.client.get("/machines/US-1/")

        self.assertContains(response, "abandoned")

    def test_machine_list_offers_clean_all(self):
        self.login()
        self.json("preflight", "US-1")

        response = self.client.get("/machines/")

        self.assertContains(response, "Queue CLEAN_ALL for all")

    def test_dashboard_does_not_count_abandoned_sessions_as_in_flight(self):
        self.login()
        self.json("preflight", "US-1")
        self.json("preflight", "US-1")

        response = self.client.get("/")

        self.assertEqual(response.context["in_flight_sessions"], 1)


class PurgeSyncSessionsTests(SyncTestCase):
    """Retention for the sync session log.

    The command's whole risk surface is which rows it picks, so these fix the
    boundary (older than the window, newer than it), the cursor cascade, and
    the refusal to accept a window short enough to race a live sync.
    """

    def age(self, session, days):
        """Backdate a session. started_at is auto_now_add, so create() can't."""
        SyncSession.objects.filter(pk=session.pk).update(
            started_at=timezone.now() - timedelta(days=days)
        )
        return session

    def purge(self, **kwargs):
        out = StringIO()
        call_command("purge_sync_sessions", stdout=out, **kwargs)
        return out.getvalue()

    def test_sessions_past_the_window_go_and_recent_ones_stay(self):
        self.json("preflight", "US-1")
        old = self.age(self.session("US-1"), days=31)
        self.json("preflight", "US-2")
        recent = self.session("US-2")

        self.purge(days=30)

        self.assertFalse(SyncSession.objects.filter(pk=old.pk).exists())
        self.assertTrue(SyncSession.objects.filter(pk=recent.pk).exists())

    def test_a_session_inside_the_window_survives_to_the_last_day(self):
        self.json("preflight", "US-1")
        self.age(self.session("US-1"), days=29)

        self.purge(days=30)

        self.assertEqual(SyncSession.objects.count(), 1)

    def test_cursors_of_a_purged_session_go_with_it(self):
        # More rules than the 5-rule test batch size, so the first page hands
        # back a cursor, and no postflight, so the cursor survives the sync --
        # postflight is what normally clears them.
        for n in range(7):
            self.make_rule(f"rule-{n}", applies_to_upper_school=True)
        self.json("preflight", "US-1")
        self.json("ruledownload", "US-1")
        session = self.session("US-1")
        self.assertTrue(SyncCursor.objects.filter(session=session).exists())
        self.age(session, days=31)

        output = self.purge(days=30)

        self.assertFalse(SyncSession.objects.exists())
        self.assertFalse(SyncCursor.objects.exists())
        self.assertIn("1 cursors", output)

    def test_an_old_session_left_open_is_purged_too(self):
        """A machine that never came back leaves a session open forever, and
        those rows are what inflate the dashboard's in-flight count."""
        self.json("preflight", "US-1")
        session = self.session("US-1")
        self.assertIsNone(session.completed_at)
        self.assertIsNone(session.abandoned_at)
        self.age(session, days=31)

        self.purge(days=30)

        self.assertFalse(SyncSession.objects.exists())

    def test_the_machine_roster_is_untouched_by_a_purge(self):
        """Machine carries the durable counters, so culling the log must not
        disturb the Machines tab."""
        self.json("preflight", "US-1")
        self.json("postflight", "US-1", {"rules_received": 3, "rules_processed": 3})
        self.age(self.session("US-1"), days=31)
        before = Machine.objects.get(machine_id="US-1")

        self.purge(days=30)

        after = Machine.objects.get(machine_id="US-1")
        self.assertEqual(after.sync_count, before.sync_count)
        self.assertEqual(after.last_completed, before.last_completed)
        self.assertFalse(SyncSession.objects.exists())

    def test_batching_deletes_every_matching_row(self):
        for n in range(5):
            self.json("preflight", f"US-{n}")
            self.age(self.session(f"US-{n}"), days=31)

        output = self.purge(days=30, batch_size=2)

        self.assertFalse(SyncSession.objects.exists())
        self.assertIn("Deleted 5 sessions", output)

    def test_dry_run_reports_without_deleting(self):
        self.json("preflight", "US-1")
        self.age(self.session("US-1"), days=31)

        output = self.purge(days=30, dry_run=True)

        self.assertIn("Would delete 1 sessions", output)
        self.assertEqual(SyncSession.objects.count(), 1)

    def test_a_window_under_a_day_is_refused(self):
        """Under a day the cutoff starts to overlap a sync in progress, whose
        session a ruledownload still needs."""
        with self.assertRaises(CommandError):
            self.purge(days=0)
