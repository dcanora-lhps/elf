"""Tests for the Santa sync endpoints.

The sync flow is exercised through the real HTTP endpoints so the auth
decorator, JSON handling and session bookkeeping are all in the loop.

Every sync this server hands out is a CLEAN sync: the client drops the rules it
holds and applies exactly what the download delivers. That makes two properties
load-bearing, and most of what follows tests them — a download must never be
silently short, and it must never be silently repeated.
"""

import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from .audience import UnknownAudience, rules_queryset_for
from .models import (
    Audience,
    CleanSyncFlag,
    ClientMode,
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
