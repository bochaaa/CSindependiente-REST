from datetime import time
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from reservations.models import (
    ClubSchedule,
    Court,
    DayOfWeek,
    GameMode,
    PlayerType,
    PriceRule,
)


class Command(BaseCommand):
    help = "Seed initial courts, schedules and price rules for CSI Tenis."

    @transaction.atomic
    def handle(self, *args, **options):
        self._seed_courts()
        self._seed_schedule()
        self._seed_prices()
        self.stdout.write(self.style.SUCCESS("Initial data seeded successfully."))

    def _seed_courts(self):
        for index in range(1, 6):
            Court.objects.get_or_create(
                name=f"Cancha {index}",
                defaults={"active": True},
            )
        Court.objects.filter(name__in=[f"Cancha {i}" for i in range(1, 6)]).update(active=True)

    def _seed_schedule(self):
        open_time = time(hour=8, minute=0)
        close_time = time(hour=21, minute=0)
        for day in DayOfWeek.values:
            ClubSchedule.objects.update_or_create(
                day_of_week=day,
                defaults={
                    "open_time": open_time,
                    "close_time": close_time,
                    "active": True,
                },
            )

    def _seed_prices(self):
        today = timezone.localdate()
        self._upsert_price(
            game_mode=GameMode.SINGLES,
            player_type=PlayerType.MEMBER,
            price=Decimal("8000.00"),
            today=today,
        )
        self._upsert_price(
            game_mode=GameMode.SINGLES,
            player_type=PlayerType.NON_MEMBER,
            price=Decimal("13000.00"),
            today=today,
        )
        self._upsert_price(
            game_mode=GameMode.DOUBLES,
            player_type=PlayerType.MEMBER,
            price=Decimal("4000.00"),
            today=today,
        )
        self._upsert_price(
            game_mode=GameMode.DOUBLES,
            player_type=PlayerType.NON_MEMBER,
            price=Decimal("6500.00"),
            today=today,
        )

    def _upsert_price(self, game_mode: str, player_type: str, price: Decimal, today):
        PriceRule.objects.filter(
            game_mode=game_mode,
            player_type=player_type,
            active=True,
        ).exclude(valid_from=today).update(active=False, valid_to=today)
        PriceRule.objects.update_or_create(
            game_mode=game_mode,
            player_type=player_type,
            valid_from=today,
            defaults={
                "price": price,
                "active": True,
                "valid_to": None,
            },
        )
