from __future__ import annotations

import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from django.core.management.base import BaseCommand

from reservations.services import generate_recurring_reservations

logger = logging.getLogger(__name__)

DEFAULT_LOCK_TIMEOUT_SECONDS = 15 * 60


class ScheduledTasksAlreadyRunning(Exception):
    pass


@contextmanager
def scheduled_task_lock(lock_dir: str | None = None, timeout_seconds: int = DEFAULT_LOCK_TIMEOUT_SECONDS):
    lock_path = Path(lock_dir or tempfile.gettempdir()) / "csitenis_run_scheduled_tasks.lock"
    acquired = False
    try:
        try:
            os.mkdir(lock_path)
            acquired = True
        except FileExistsError:
            lock_age = time.time() - lock_path.stat().st_mtime
            if lock_age <= timeout_seconds:
                raise ScheduledTasksAlreadyRunning
            logger.warning("Removing stale scheduled tasks lock at %s.", lock_path)
            os.rmdir(lock_path)
            os.mkdir(lock_path)
            acquired = True

        yield
    finally:
        if acquired:
            os.rmdir(lock_path)


class Command(BaseCommand):
    help = "Run all scheduled maintenance tasks for the reservations backend."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days-ahead",
            type=int,
            default=90,
            help="How many days ahead to generate recurring reservations.",
        )
        parser.add_argument(
            "--skip-recurring",
            action="store_true",
            help="Skip generating recurring reservations.",
        )
        parser.add_argument(
            "--no-lock",
            action="store_true",
            help="Run without the scheduler lock.",
        )
        parser.add_argument(
            "--lock-dir",
            default=None,
            help="Directory where the scheduler lock directory is created.",
        )
        parser.add_argument(
            "--lock-timeout-seconds",
            type=int,
            default=DEFAULT_LOCK_TIMEOUT_SECONDS,
            help="Seconds before an existing scheduler lock is considered stale.",
        )

    def handle(self, *args, **options):
        try:
            if options["no_lock"]:
                result = self._run_tasks(options)
            else:
                with scheduled_task_lock(
                    lock_dir=options["lock_dir"],
                    timeout_seconds=options["lock_timeout_seconds"],
                ):
                    result = self._run_tasks(options)
        except ScheduledTasksAlreadyRunning:
            message = "Scheduled tasks are already running; skipping this execution."
            logger.warning(message)
            self.stdout.write(self.style.WARNING(message))
            return

        self.stdout.write(
            self.style.SUCCESS(
                "Scheduled tasks finished. "
                "Expired without payment: {expired_without_payment}. "
                "Marked for review: {marked_for_review}. "
                "Generated recurring reservations: {generated_recurring_reservations}.".format(**result)
            )
        )

    def _run_tasks(self, options):
        result = {
            "expired_without_payment": 0,
            "marked_for_review": 0,
            "generated_recurring_reservations": 0,
        }

        if not options["skip_recurring"]:
            created = generate_recurring_reservations(days_ahead=options["days_ahead"])
            result["generated_recurring_reservations"] = created
            logger.info("Generated recurring reservations. created=%s", created)

        return result
