from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .models import (
    BlockType,
    BlockedSlot,
    CancellationRequest,
    ClubSchedule,
    DayOfWeek,
    GameMode,
    PlayerType,
    PriceRule,
    RecurringReservationRule,
    Reservation,
    ReservationStatus,
    ReservationType,
    SpecialSchedule,
    Court,
)
from .services import generate_recurring_reservations


class ReservationBusinessRulesTests(APITestCase):
    def setUp(self):
        self.User = get_user_model()
        self.admin = self.User.objects.create_user(
            username="admin",
            password="admin123",
            is_staff=True,
        )
        self.court = self._create_court()
        self._create_default_schedules()
        self._create_price_rules()
        self.base_date = timezone.localdate() + timedelta(days=1)

    def _create_court(self):
        from .models import Court

        return Court.objects.create(name="Cancha 1", active=True)

    def _create_default_schedules(self):
        for day in DayOfWeek.values:
            ClubSchedule.objects.create(
                day_of_week=day,
                open_time=time(hour=8, minute=0),
                close_time=time(hour=23, minute=0),
                active=True,
            )

    def _create_price_rules(self):
        valid_from = timezone.localdate() - timedelta(days=30)
        PriceRule.objects.create(
            game_mode=GameMode.SINGLES,
            player_type=PlayerType.MEMBER,
            price=Decimal("4000.00"),
            active=True,
            valid_from=valid_from,
        )
        PriceRule.objects.create(
            game_mode=GameMode.SINGLES,
            player_type=PlayerType.NON_MEMBER,
            price=Decimal("6000.00"),
            active=True,
            valid_from=valid_from,
        )
        PriceRule.objects.create(
            game_mode=GameMode.DOUBLES,
            player_type=PlayerType.MEMBER,
            price=Decimal("3500.00"),
            active=True,
            valid_from=valid_from,
        )
        PriceRule.objects.create(
            game_mode=GameMode.DOUBLES,
            player_type=PlayerType.NON_MEMBER,
            price=Decimal("5000.00"),
            active=True,
            valid_from=valid_from,
        )

    def _reservation_payload(self, game_mode=GameMode.SINGLES, target_date=None, start_time="18:00", players=None):
        if target_date is None:
            target_date = self.base_date
        if players is None:
            players = [
                {"first_name": "Pedro", "last_name": "Rodriguez", "is_member": True},
                {"first_name": "Santi", "last_name": "Fernandez", "is_member": False},
            ]
        return {
            "court": self.court.id,
            "date": target_date.isoformat(),
            "start_time": start_time,
            "game_mode": game_mode,
            "contact_name": "Pedro Rodriguez",
            "contact_phone": "2302123456",
            "players": players,
            "notes": "Reserva de test",
        }

    def test_create_valid_singles_reservation(self):
        response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], ReservationStatus.CONFIRMED)
        self.assertEqual(response.data["reservation_type"], ReservationType.NORMAL)
        self.assertEqual(response.data["total_price"], "10000.00")
        self.assertEqual(len(response.data["players"]), 2)
        reservation = Reservation.objects.get(id=response.data["id"])
        self.assertEqual(reservation.players.count(), 2)
        self.assertEqual(reservation.players.first().price_applied, Decimal("4000.00"))

    def test_create_valid_doubles_reservation(self):
        players = [
            {"first_name": "Juan", "last_name": "Perez", "is_member": True},
            {"first_name": "Marcos", "last_name": "Gomez", "is_member": True},
            {"first_name": "Lucas", "last_name": "Diaz", "is_member": False},
            {"first_name": "Nico", "last_name": "Torres", "is_member": False},
        ]
        payload = self._reservation_payload(game_mode=GameMode.DOUBLES, players=players)
        response = self.client.post(reverse("reservation-list"), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["total_price"], "17000.00")
        self.assertEqual(len(response.data["players"]), 4)

    def test_reject_invalid_singles_player_count(self):
        players = [
            {"first_name": "A", "last_name": "Uno", "is_member": True},
            {"first_name": "B", "last_name": "Dos", "is_member": True},
            {"first_name": "C", "last_name": "Tres", "is_member": False},
        ]
        payload = self._reservation_payload(game_mode=GameMode.SINGLES, players=players)
        response = self.client.post(reverse("reservation-list"), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("players", response.data)

    def test_reject_past_date_reservation(self):
        payload = self._reservation_payload(target_date=timezone.localdate() - timedelta(days=1))
        response = self.client.post(reverse("reservation-list"), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("date", response.data)

    def test_reject_reservation_already_started_today(self):
        started_time = (timezone.localtime() - timedelta(minutes=10)).time().replace(microsecond=0)
        payload = self._reservation_payload(target_date=timezone.localdate(), start_time=started_time.strftime("%H:%M"))
        response = self.client.post(reverse("reservation-list"), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_overlap_with_confirmed_reservation(self):
        response_1 = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        self.assertEqual(response_1.status_code, status.HTTP_201_CREATED)
        response_2 = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        self.assertEqual(response_2.status_code, status.HTTP_400_BAD_REQUEST)

    def test_allow_slot_when_previous_reservation_is_cancelled(self):
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation = Reservation.objects.get(id=create_response.data["id"])
        reservation.status = ReservationStatus.CANCELLED
        reservation.save(update_fields=("status", "updated_at"))
        second_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        self.assertEqual(second_response.status_code, status.HTTP_201_CREATED)

    def test_reject_overlap_with_blocked_slot(self):
        start_datetime = timezone.make_aware(
            datetime.combine(self.base_date, time(hour=18, minute=0)),
            timezone.get_current_timezone(),
        )
        end_datetime = start_datetime + timedelta(minutes=90)
        BlockedSlot.objects.create(
            court=self.court,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            block_type=BlockType.TOURNAMENT,
            reason="Torneo interno",
            created_by=self.admin,
        )
        response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_reservation_outside_club_schedule(self):
        schedule = ClubSchedule.objects.get(day_of_week=DayOfWeek.values[self.base_date.weekday()])
        schedule.open_time = time(hour=8, minute=0)
        schedule.close_time = time(hour=17, minute=0)
        schedule.save()
        response = self.client.post(reverse("reservation-list"), self._reservation_payload(start_time="18:00"), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_special_schedule_overrides_weekly_schedule(self):
        SpecialSchedule.objects.create(
            date=self.base_date,
            closed=True,
            reason="Feriado",
        )
        response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_can_cancel_reservation(self):
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation_id = create_response.data["id"]
        self.client.force_authenticate(user=self.admin)
        cancel_response = self.client.patch(
            reverse("reservation-cancel", args=[reservation_id]),
            {"cancellation_reason": "Lluvia"},
            format="json",
        )
        self.assertEqual(cancel_response.status_code, status.HTTP_200_OK)
        self.assertEqual(cancel_response.data["status"], ReservationStatus.CANCELLED)

    def test_public_user_can_request_cancellation(self):
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation_id = create_response.data["id"]
        response = self.client.post(
            reverse("reservation-request-cancellation", args=[reservation_id]),
            {
                "requester_name": "Pedro Rodriguez",
                "requester_phone": "2302123456",
                "reason": "No podemos asistir",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(CancellationRequest.objects.count(), 1)
        reservation = Reservation.objects.get(id=reservation_id)
        self.assertEqual(reservation.status, ReservationStatus.CANCELLATION_REQUESTED)

    def test_generate_recurring_classes_60_minutes_and_no_duplicates(self):
        target_date = timezone.localdate() + timedelta(days=1)
        day = DayOfWeek.values[target_date.weekday()]
        rule = RecurringReservationRule.objects.create(
            court=self.court,
            title="Clases de Pedrito",
            days_of_week=[day],
            start_time=time(hour=16, minute=0),
            start_date=target_date,
            end_date=target_date,
            active=True,
            notes="Clase fija semanal",
            created_by=self.admin,
        )
        created_first = generate_recurring_reservations(days_ahead=7)
        created_second = generate_recurring_reservations(days_ahead=7)
        self.assertEqual(created_first, 1)
        self.assertEqual(created_second, 0)
        generated_reservation = Reservation.objects.get(recurring_rule=rule)
        self.assertEqual(generated_reservation.reservation_type, ReservationType.CLASS)
        self.assertEqual(
            generated_reservation.end_datetime,
            generated_reservation.start_datetime + timedelta(minutes=60),
        )

    def test_class_overlap_produces_correct_available_and_unavailable_ranges(self):
        target_date = timezone.localdate() + timedelta(days=1)
        day = DayOfWeek.values[target_date.weekday()]
        RecurringReservationRule.objects.create(
            court=self.court,
            title="Clases de Pedrito",
            days_of_week=[day],
            start_time=time(hour=16, minute=0),
            start_date=target_date,
            end_date=target_date,
            active=True,
            notes="Clase de 60 minutos",
            created_by=self.admin,
        )
        generate_recurring_reservations(days_ahead=7)
        response = self.client.get(f"{reverse('availability')}?date={target_date.isoformat()}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        court_data = response.data["courts"][0]
        unavailable_16_17 = None
        for item in court_data["unavailable_ranges"]:
            if item["start_time"] == "16:00:00" and item["end_time"] == "17:00:00":
                unavailable_16_17 = item
                break
        self.assertIsNotNone(unavailable_16_17)
        self.assertEqual(unavailable_16_17["reason"], "RESERVATION")
        self.assertEqual(unavailable_16_17["reservation_type"], ReservationType.CLASS)
        self.assertEqual(unavailable_16_17["class_title"], "Clases de Pedrito")
        self.assertIsNone(unavailable_16_17["reservation_contact_name"])

        available_8_16 = None
        for item in court_data["available_ranges"]:
            if item["start_time"] == "08:00:00" and item["end_time"] == "16:00:00":
                available_8_16 = item
                break
        self.assertIsNotNone(available_8_16)
        self.assertTrue(available_8_16["can_book_90_min"])
        self.assertEqual(available_8_16["can_start_until"], "14:30:00")

    def test_recurring_rule_create_endpoint_generates_classes_automatically(self):
        target_date = timezone.localdate() + timedelta(days=1)
        day = DayOfWeek.values[target_date.weekday()]
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            reverse("recurring-rule-list"),
            {
                "court": self.court.id,
                "title": "Clase Auto",
                "days_of_week": [day],
                "start_time": "16:00",
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "active": True,
                "notes": "Generacion automatica",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created_rule_id = response.data["id"]
        generated = Reservation.objects.filter(
            reservation_type=ReservationType.CLASS,
            recurring_rule_id=created_rule_id,
            start_datetime__date=target_date,
        )
        self.assertEqual(generated.count(), 1)

    def test_admin_endpoint_requires_jwt_for_write(self):
        create_court_payload = {"name": "Cancha 2", "active": True}

        unauthorized_response = self.client.post(
            reverse("court-list"),
            create_court_payload,
            format="json",
        )
        self.assertIn(
            unauthorized_response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

        token_response = self.client.post(
            reverse("token_obtain_pair"),
            {"username": "admin", "password": "admin123"},
            format="json",
        )
        self.assertEqual(token_response.status_code, status.HTTP_200_OK)
        access_token = token_response.data["access"]

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
        authorized_response = self.client.post(
            reverse("court-list"),
            create_court_payload,
            format="json",
        )
        self.assertEqual(authorized_response.status_code, status.HTTP_201_CREATED)

    def test_admin_can_approve_cancellation_request_and_cancel_reservation(self):
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation_id = create_response.data["id"]
        request_response = self.client.post(
            reverse("reservation-request-cancellation", args=[reservation_id]),
            {
                "requester_name": "Pedro Rodriguez",
                "requester_phone": "2302123456",
                "reason": "No podemos asistir",
            },
            format="json",
        )
        self.assertEqual(request_response.status_code, status.HTTP_201_CREATED)
        cancellation_request = CancellationRequest.objects.get(reservation_id=reservation_id)

        token_response = self.client.post(
            reverse("token_obtain_pair"),
            {"username": "admin", "password": "admin123"},
            format="json",
        )
        access_token = token_response.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
        resolve_response = self.client.patch(
            reverse("cancellation-request-resolve", args=[cancellation_request.id]),
            {"status": "APPROVED", "cancellation_reason": "Aprobado por admin"},
            format="json",
        )
        self.assertEqual(resolve_response.status_code, status.HTTP_200_OK)
        cancellation_request.refresh_from_db()
        reservation = Reservation.objects.get(id=reservation_id)
        self.assertEqual(cancellation_request.status, "APPROVED")
        self.assertIsNotNone(cancellation_request.resolved_at)
        self.assertEqual(reservation.status, ReservationStatus.CANCELLED)
        self.assertEqual(reservation.cancellation_reason, "Aprobado por admin")

    def test_admin_can_reject_cancellation_request_without_cancelling_reservation(self):
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation_id = create_response.data["id"]
        self.client.post(
            reverse("reservation-request-cancellation", args=[reservation_id]),
            {
                "requester_name": "Pedro Rodriguez",
                "requester_phone": "2302123456",
                "reason": "No podemos asistir",
            },
            format="json",
        )
        cancellation_request = CancellationRequest.objects.get(reservation_id=reservation_id)
        self.client.force_authenticate(user=self.admin)
        resolve_response = self.client.patch(
            reverse("cancellation-request-resolve", args=[cancellation_request.id]),
            {"status": "REJECTED"},
            format="json",
        )
        self.assertEqual(resolve_response.status_code, status.HTTP_200_OK)
        cancellation_request.refresh_from_db()
        reservation = Reservation.objects.get(id=reservation_id)
        self.assertEqual(cancellation_request.status, "REJECTED")
        self.assertEqual(reservation.status, ReservationStatus.CONFIRMED)

    def test_cannot_resolve_same_cancellation_request_twice(self):
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation_id = create_response.data["id"]
        self.client.post(
            reverse("reservation-request-cancellation", args=[reservation_id]),
            {
                "requester_name": "Pedro Rodriguez",
                "requester_phone": "2302123456",
                "reason": "No podemos asistir",
            },
            format="json",
        )
        cancellation_request = CancellationRequest.objects.get(reservation_id=reservation_id)
        self.client.force_authenticate(user=self.admin)
        first_response = self.client.patch(
            reverse("cancellation-request-resolve", args=[cancellation_request.id]),
            {"status": "REJECTED"},
            format="json",
        )
        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        second_response = self.client.patch(
            reverse("cancellation-request-resolve", args=[cancellation_request.id]),
            {"status": "APPROVED"},
            format="json",
        )
        self.assertEqual(second_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_request_cancellation_with_less_than_three_hours(self):
        start_datetime = timezone.now() + timedelta(hours=2)
        reservation = Reservation.objects.create(
            court=self.court,
            reservation_type=ReservationType.NORMAL,
            game_mode=GameMode.SINGLES,
            contact_name="Pedro Rodriguez",
            contact_phone="2302123456",
            start_datetime=start_datetime,
            end_datetime=start_datetime + timedelta(minutes=90),
            status=ReservationStatus.CONFIRMED,
            total_price=Decimal("10000.00"),
            notes="",
        )
        response = self.client.post(
            reverse("reservation-request-cancellation", args=[reservation.id]),
            {
                "requester_name": "Pedro Rodriguez",
                "requester_phone": "2302123456",
                "reason": "No podemos asistir",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", response.data)

    def test_auth_me_returns_current_user(self):
        token_response = self.client.post(
            reverse("token_obtain_pair"),
            {"username": "admin", "password": "admin123"},
            format="json",
        )
        self.assertEqual(token_response.status_code, status.HTTP_200_OK)
        access_token = token_response.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

        me_response = self.client.get(reverse("auth_me"))
        self.assertEqual(me_response.status_code, status.HTTP_200_OK)
        self.assertEqual(me_response.data["username"], "admin")
        self.assertTrue(me_response.data["is_staff"])

    def test_auth_user_detail_returns_user_by_id_for_admin(self):
        token_response = self.client.post(
            reverse("token_obtain_pair"),
            {"username": "admin", "password": "admin123"},
            format="json",
        )
        self.assertEqual(token_response.status_code, status.HTTP_200_OK)
        access_token = token_response.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

        detail_response = self.client.get(reverse("auth_user_detail", args=[self.admin.id]))
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_response.data["id"], self.admin.id)
        self.assertEqual(detail_response.data["username"], "admin")


class SeedInitialDataCommandTests(TestCase):
    def test_seed_initial_data_creates_expected_base_records(self):
        call_command("seed_initial_data")

        self.assertEqual(Court.objects.filter(active=True).count(), 5)
        self.assertTrue(Court.objects.filter(name="Cancha 1", active=True).exists())
        self.assertEqual(ClubSchedule.objects.filter(active=True).count(), 7)
        self.assertTrue(
            ClubSchedule.objects.filter(
                day_of_week=DayOfWeek.MONDAY,
                open_time=time(hour=8, minute=0),
                close_time=time(hour=21, minute=0),
                active=True,
            ).exists()
        )

        today = timezone.localdate()
        self.assertTrue(
            PriceRule.objects.filter(
                game_mode=GameMode.SINGLES,
                player_type=PlayerType.MEMBER,
                price=Decimal("8000.00"),
                active=True,
                valid_from=today,
            ).exists()
        )
        self.assertTrue(
            PriceRule.objects.filter(
                game_mode=GameMode.SINGLES,
                player_type=PlayerType.NON_MEMBER,
                price=Decimal("13000.00"),
                active=True,
                valid_from=today,
            ).exists()
        )
        self.assertTrue(
            PriceRule.objects.filter(
                game_mode=GameMode.DOUBLES,
                player_type=PlayerType.MEMBER,
                price=Decimal("4000.00"),
                active=True,
                valid_from=today,
            ).exists()
        )
        self.assertTrue(
            PriceRule.objects.filter(
                game_mode=GameMode.DOUBLES,
                player_type=PlayerType.NON_MEMBER,
                price=Decimal("6500.00"),
                active=True,
                valid_from=today,
            ).exists()
        )
