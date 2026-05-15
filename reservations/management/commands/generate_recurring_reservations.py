from django.core.management.base import BaseCommand

from reservations.services import generate_recurring_reservations


class Command(BaseCommand):
    help = "Generate CLASS reservations from active recurring rules."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days-ahead",
            type=int,
            default=90,
            help="How many days ahead to generate reservations.",
        )

    def handle(self, *args, **options):
        created = generate_recurring_reservations(days_ahead=options["days_ahead"])
        self.stdout.write(self.style.SUCCESS(f"Generated {created} recurring reservations."))
