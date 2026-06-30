from django.core.management.base import BaseCommand

from reservations.services import expire_pending_reservations


class Command(BaseCommand):
    help = "Report pending reservation expiration status. Automatic expiration is currently disabled."

    def handle(self, *args, **options):
        result = expire_pending_reservations()
        self.stdout.write(
            self.style.SUCCESS(
                "Expired without payment: {expired_without_payment}. "
                "Marked for review: {marked_for_review}.".format(**result)
            )
        )
