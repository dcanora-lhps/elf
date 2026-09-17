"""Retention for SyncSession rows.

One sync per machine every SANTA_FULL_SYNC_INTERVAL_SECONDS means 144 rows per
machine per day at the default ten-minute interval, and nothing in the app ever
deleted them. This command is the other half of that: it drops sessions past a
retention window so the table stays proportional to the fleet rather than to
how long the server has been up.

Deleting old sessions loses no state the app depends on. The Machine roster row
is the durable record -- sync_count, first_seen, last_seen and last_completed
are all maintained incrementally at preflight and postflight, not aggregated
from sessions -- so no counter needs rebuilding afterwards. What goes away is
per-sync forensics beyond the window: the rule tallies, the rules_hash pair,
the client_mode actually served, and the identity a machine reported at the
time. The machine page shows the 50 most recent sessions for a machine, so any
window worth keeping is already far wider than the UI reads.
"""

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from ...models import SyncCursor, SyncSession

log = logging.getLogger("santa.purge")

DEFAULT_DAYS = 30
DEFAULT_BATCH_SIZE = 5000

# Rows between progress lines. A first run against a table that was never
# pruned can be millions of rows, and silence for that long reads as a hang.
PROGRESS_EVERY = 100_000


class Command(BaseCommand):
    help = "Delete SyncSession rows (and their cursors) older than --days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=DEFAULT_DAYS,
            help=f"Retention window in days (default {DEFAULT_DAYS}).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help=(
                "Sessions deleted per transaction "
                f"(default {DEFAULT_BATCH_SIZE}). Lower it to shorten each lock."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted and change nothing.",
        )
        parser.add_argument(
            "--vacuum",
            action="store_true",
            help=(
                "Run VACUUM (ANALYZE) on the two tables afterwards (PostgreSQL "
                "only). Reclaims dead tuples for reuse; it does not return disk "
                "to the OS -- a one-off VACUUM FULL does that."
            ),
        )

    def handle(self, *args, **options):
        days = options["days"]
        batch_size = options["batch_size"]

        # Age alone is a safe predicate, with no need to also require that the
        # session reached postflight. Santa runs one sync at a time and gives
        # up on a stage after five retries, so a session older than a day
        # cannot still be live -- and deleting the stale open rows is the
        # point, since a session left open by a machine that never came back is
        # exactly what inflates the dashboard's in-flight count. Anything under
        # a day starts to race a sync in progress, so refuse it.
        if days < 1:
            raise CommandError("--days must be at least 1.")
        if batch_size < 1:
            raise CommandError("--batch-size must be at least 1.")

        cutoff = timezone.now() - timedelta(days=days)
        window = f"started before {cutoff:%Y-%m-%d %H:%M:%S%z} (--days {days})"

        if options["dry_run"]:
            sessions = SyncSession.objects.filter(started_at__lt=cutoff).count()
            cursors = SyncCursor.objects.filter(session__started_at__lt=cutoff).count()
            self.stdout.write(
                f"Would delete {sessions} sessions and {cursors} cursors {window}."
            )
            return

        sessions, cursors = self._purge(cutoff, batch_size)

        summary = f"Deleted {sessions} sessions and {cursors} cursors {window}."
        log.info(summary)
        self.stdout.write(self.style.SUCCESS(summary))

        if options["vacuum"]:
            self._vacuum()

    def _purge(self, cutoff, batch_size):
        """Delete in committed batches, oldest first.

        One statement for millions of rows would hold a transaction open long
        enough to stall autovacuum on the same table and write the whole delete
        to the WAL before any of it could be reclaimed, so each batch commits
        on its own. Ordering by started_at both walks the index the filter
        already uses -- the model's default `-started_at` ordering would
        otherwise sort the entire matching set on every pass -- and makes a run
        that dies partway through leave a contiguous, still-prunable tail.

        Cursors go first and explicitly: Django emulates `on_delete=CASCADE` in
        Python rather than in the schema, so the FK carries no ON DELETE clause
        and the child rows have to be cleared before the parents either way.
        """
        total_sessions = 0
        total_cursors = 0
        next_progress = PROGRESS_EVERY

        while True:
            pks = list(
                SyncSession.objects.filter(started_at__lt=cutoff)
                .order_by("started_at")
                .values_list("pk", flat=True)[:batch_size]
            )
            if not pks:
                break

            with transaction.atomic():
                _, cursors_by_model = SyncCursor.objects.filter(session_id__in=pks).delete()
                _, sessions_by_model = SyncSession.objects.filter(pk__in=pks).delete()

            total_cursors += cursors_by_model.get("app.SyncCursor", 0)
            deleted = sessions_by_model.get("app.SyncSession", 0)
            total_sessions += deleted

            if not deleted:
                # Rows matched but none were removed, so another writer holds
                # them. Stop rather than spin on the same batch forever.
                log.warning("purge made no progress on a batch of %d; stopping", len(pks))
                break

            if total_sessions >= next_progress:
                log.info("purge progress: %d sessions deleted", total_sessions)
                next_progress += PROGRESS_EVERY

        return total_sessions, total_cursors

    def _vacuum(self):
        if connection.vendor != "postgresql":
            self.stdout.write(f"--vacuum skipped: not supported on {connection.vendor}.")
            return
        # Outside any atomic block: VACUUM cannot run inside a transaction, and
        # management commands are autocommit unless something wraps them.
        with connection.cursor() as cursor:
            for table in (SyncCursor._meta.db_table, SyncSession._meta.db_table):
                cursor.execute(f"VACUUM (ANALYZE) {connection.ops.quote_name(table)}")
        self.stdout.write("Vacuumed and analyzed the session tables.")
