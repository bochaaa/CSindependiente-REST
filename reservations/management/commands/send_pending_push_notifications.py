from django.core.management.base import BaseCommand

from reservations.services import send_pending_push_notifications


class Command(BaseCommand):
    help = "Send pending push notification logs through the configured push provider."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum pending push notifications to process.",
        )

    def handle(self, *args, **options):
        result = send_pending_push_notifications(limit=options["limit"])
        self.stdout.write(
            self.style.SUCCESS(
                "Push notifications processed. "
                "sent={sent} failed={failed} skipped={skipped}".format(**result)
            )
        )
