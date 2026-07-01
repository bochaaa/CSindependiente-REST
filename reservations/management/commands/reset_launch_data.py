from django.core.management.base import BaseCommand
from django.db import transaction

from reservations.models import (
    BlockedSlot,
    CancellationRequest,
    ClubSchedule,
    Court,
    NotificationDevice,
    NotificationLog,
    PaymentTransaction,
    PriceRule,
    RecurringReservationRule,
    Reservation,
    ReservationPlayer,
    SpecialSchedule,
)


CONFIRMATION_TEXT = "RESET_LAUNCH_DATA"


class Command(BaseCommand):
    help = (
        "Remove transactional test data before launch. "
        "Preserves courts, schedules, price rules and recurring reservation rules."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            default="",
            help=f"Required to delete data. Use: --confirm {CONFIRMATION_TEXT}",
        )
        parser.add_argument(
            "--delete-blocked-slots",
            action="store_true",
            help="Also delete manually blocked slots.",
        )
        parser.add_argument(
            "--delete-notification-devices",
            action="store_true",
            help="Also delete registered notification devices.",
        )

    def handle(self, *args, **options):
        counts = self._get_counts(options)
        self._write_summary(counts, options)

        if options["confirm"] != CONFIRMATION_TEXT:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run only. To delete, run with --confirm {CONFIRMATION_TEXT}."
                )
            )
            return

        with transaction.atomic():
            deleted = {}
            if options["delete_notification_devices"]:
                deleted["notification_devices"] = NotificationDevice.objects.all().delete()[0]
            if options["delete_blocked_slots"]:
                deleted["blocked_slots"] = BlockedSlot.objects.all().delete()[0]

            deleted_count, deleted_by_model = Reservation.objects.all().delete()
            deleted["reservation_cascade_total"] = deleted_count
            for model_label, count in sorted(deleted_by_model.items()):
                deleted[model_label] = count

        self.stdout.write(self.style.SUCCESS("Launch data reset completed."))
        for label, count in deleted.items():
            self.stdout.write(f"{label}: {count}")

    def _get_counts(self, options):
        return {
            "reservations": Reservation.objects.count(),
            "reservation_players": ReservationPlayer.objects.count(),
            "payment_transactions": PaymentTransaction.objects.count(),
            "cancellation_requests": CancellationRequest.objects.count(),
            "notification_logs": NotificationLog.objects.count(),
            "blocked_slots": BlockedSlot.objects.count(),
            "notification_devices": NotificationDevice.objects.count(),
            "kept_courts": Court.objects.count(),
            "kept_price_rules": PriceRule.objects.count(),
            "kept_recurring_rules": RecurringReservationRule.objects.count(),
            "kept_club_schedules": ClubSchedule.objects.count(),
            "kept_special_schedules": SpecialSchedule.objects.count(),
            "will_delete_blocked_slots": options["delete_blocked_slots"],
            "will_delete_notification_devices": options["delete_notification_devices"],
        }

    def _write_summary(self, counts, options):
        self.stdout.write("Data selected for launch reset:")
        self.stdout.write(f"reservations: {counts['reservations']}")
        self.stdout.write(f"reservation_players: {counts['reservation_players']}")
        self.stdout.write(f"payment_transactions: {counts['payment_transactions']}")
        self.stdout.write(f"cancellation_requests: {counts['cancellation_requests']}")
        self.stdout.write(f"notification_logs: {counts['notification_logs']}")
        if options["delete_blocked_slots"]:
            self.stdout.write(f"blocked_slots: {counts['blocked_slots']}")
        if options["delete_notification_devices"]:
            self.stdout.write(f"notification_devices: {counts['notification_devices']}")

        self.stdout.write("Data preserved:")
        self.stdout.write(f"courts: {counts['kept_courts']}")
        self.stdout.write(f"price_rules: {counts['kept_price_rules']}")
        self.stdout.write(f"recurring_reservation_rules: {counts['kept_recurring_rules']}")
        self.stdout.write(f"club_schedules: {counts['kept_club_schedules']}")
        self.stdout.write(f"special_schedules: {counts['kept_special_schedules']}")
