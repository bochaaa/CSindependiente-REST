from datetime import datetime, time, timedelta
from decimal import Decimal
import csv
import os
from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
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
    NotificationChannel,
    NotificationDevice,
    NotificationLog,
    NotificationProvider,
    NotificationStatus,
    PaymentProvider,
    PaymentTransaction,
    PaymentTransactionStatus,
    PaymentType,
    PlayerType,
    PriceRule,
    RecurringReservationRule,
    Reservation,
    ReservationPaymentStatus,
    ReservationPlayer,
    ReservationStatus,
    ReservationType,
    SpecialSchedule,
    Court,
)
from .push_notifications import InvalidPushTokenError
from .services import generate_recurring_reservations, send_pending_push_notifications


class ReservationBusinessRulesTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.User = get_user_model()
        self.admin = self.User.objects.create_user(
            username="admin",
            password="admin123",
            is_staff=True,
        )
        self.normal_user = self.User.objects.create_user(
            username="user",
            password="user123",
            is_staff=False,
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
        self.assertIsNone(reservation.payment_expires_at)

    def test_reservation_creation_queues_push_notifications_for_admin_devices(self):
        NotificationDevice.objects.create(
            user=self.admin,
            platform="web",
            provider=NotificationProvider.FCM,
            token="admin-web-token",
            enabled=True,
        )
        NotificationDevice.objects.create(
            user=self.admin,
            platform="android",
            provider=NotificationProvider.FCM,
            token="disabled-admin-token",
            enabled=False,
        )
        NotificationDevice.objects.create(
            user=self.normal_user,
            platform="web",
            provider=NotificationProvider.FCM,
            token="non-admin-token",
            enabled=True,
        )

        response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        reservation = Reservation.objects.get(id=response.data["id"])
        push_logs = NotificationLog.objects.filter(
            reservation=reservation,
            channel=NotificationChannel.PUSH,
        )
        self.assertEqual(push_logs.count(), 1)
        push_log = push_logs.get()
        self.assertEqual(push_log.destination, "admin-web-token")
        self.assertEqual(push_log.payload["title"], "Nueva reserva")
        self.assertEqual(push_log.payload["data"]["type"], "reservation_created")
        self.assertEqual(push_log.payload["data"]["reservation_id"], str(reservation.id))

    def test_admin_can_register_and_unregister_notification_device(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.post(
            reverse("notification-device-list"),
            {
                "platform": "web",
                "provider": NotificationProvider.FCM,
                "token": "fcm-token-1",
                "device_id": "browser-1",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        device = NotificationDevice.objects.get(token="fcm-token-1")
        self.assertEqual(device.user, self.admin)
        self.assertTrue(device.enabled)
        self.assertIsNotNone(device.last_seen)

        refresh_response = self.client.post(
            reverse("notification-device-list"),
            {
                "platform": "android",
                "provider": NotificationProvider.FCM,
                "token": "fcm-token-1",
                "device_id": "phone-1",
            },
            format="json",
        )
        device.refresh_from_db()
        self.assertEqual(refresh_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(NotificationDevice.objects.count(), 1)
        self.assertEqual(device.platform, "android")
        self.assertEqual(device.device_id, "phone-1")

        unregister_response = self.client.post(
            reverse("notification-device-unregister"),
            {"token": "fcm-token-1"},
            format="json",
        )

        self.assertEqual(unregister_response.status_code, status.HTTP_200_OK)
        self.assertEqual(unregister_response.data["disabled"], 1)
        device.refresh_from_db()
        self.assertFalse(device.enabled)

    def test_non_admin_cannot_register_notification_device(self):
        self.client.force_authenticate(user=self.normal_user)

        response = self.client.post(
            reverse("notification-device-list"),
            {
                "platform": "web",
                "provider": NotificationProvider.FCM,
                "token": "fcm-token-1",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pending_push_notifications_are_sent_when_firebase_is_enabled(self):
        NotificationDevice.objects.create(
            user=self.admin,
            platform="web",
            provider=NotificationProvider.FCM,
            token="admin-web-token",
            enabled=True,
        )
        response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        push_log = NotificationLog.objects.get(channel=NotificationChannel.PUSH)
        self.assertEqual(push_log.status, NotificationStatus.PENDING)

        with (
            override_settings(PUSH_NOTIFICATIONS_ENABLED=True),
            patch("reservations.services.send_firebase_push", return_value="fcm-message-id") as send_mock,
        ):
            result = send_pending_push_notifications(log_ids=[push_log.id])

        self.assertEqual(result, {"sent": 1, "failed": 0, "skipped": 0})
        push_log.refresh_from_db()
        self.assertEqual(push_log.status, NotificationStatus.SENT)
        send_mock.assert_called_once_with(
            token="admin-web-token",
            payload=push_log.payload,
        )

    def test_invalid_push_token_marks_log_failed_and_disables_device(self):
        device = NotificationDevice.objects.create(
            user=self.admin,
            platform="web",
            provider=NotificationProvider.FCM,
            token="invalid-admin-token",
            enabled=True,
        )
        response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        push_log = NotificationLog.objects.get(channel=NotificationChannel.PUSH)

        with (
            override_settings(PUSH_NOTIFICATIONS_ENABLED=True),
            patch(
                "reservations.services.send_firebase_push",
                side_effect=InvalidPushTokenError("Token no registrado."),
            ),
        ):
            result = send_pending_push_notifications(log_ids=[push_log.id])

        self.assertEqual(result, {"sent": 0, "failed": 1, "skipped": 0})
        push_log.refresh_from_db()
        device.refresh_from_db()
        self.assertEqual(push_log.status, NotificationStatus.FAILED)
        self.assertFalse(device.enabled)

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

    def test_reject_same_day_reservation_with_less_than_three_hours_notice(self):
        fixed_now = timezone.make_aware(
            datetime.combine(timezone.localdate(), time(hour=10, minute=0)),
            timezone.get_current_timezone(),
        )
        payload = self._reservation_payload(target_date=timezone.localdate(), start_time="12:00")

        with patch("reservations.services.timezone.now", return_value=fixed_now):
            response = self.client.post(reverse("reservation-list"), payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("start_time", response.data)

    def test_allow_same_day_reservation_with_three_hours_notice(self):
        fixed_now = timezone.make_aware(
            datetime.combine(timezone.localdate(), time(hour=10, minute=0)),
            timezone.get_current_timezone(),
        )
        payload = self._reservation_payload(target_date=timezone.localdate(), start_time="13:00")

        with patch("reservations.services.timezone.now", return_value=fixed_now):
            response = self.client.post(reverse("reservation-list"), payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

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

    def test_admin_can_confirm_reservation_payment(self):
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation_id = create_response.data["id"]

        self.client.force_authenticate(user=self.admin)
        payment_response = self.client.patch(
            reverse("reservation-mark-payment", args=[reservation_id]),
            {"is_paid": True},
            format="json",
        )
        self.assertEqual(payment_response.status_code, status.HTTP_200_OK)
        self.assertTrue(payment_response.data["is_paid"])
        self.assertIsNotNone(payment_response.data["paid_at"])
        self.assertEqual(payment_response.data["paid_confirmed_by"], self.admin.id)

        reservation = Reservation.objects.get(id=reservation_id)
        self.assertTrue(reservation.is_paid)
        self.assertIsNotNone(reservation.paid_at)
        self.assertEqual(reservation.paid_confirmed_by_id, self.admin.id)
        cash_transaction = PaymentTransaction.objects.get(reservation=reservation)
        self.assertEqual(cash_transaction.provider, PaymentProvider.CASH)
        self.assertEqual(cash_transaction.status, PaymentTransactionStatus.APPROVED)
        self.assertEqual(cash_transaction.base_amount, reservation.total_price)

    def test_non_admin_cannot_confirm_reservation_payment(self):
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation_id = create_response.data["id"]

        self.client.force_authenticate(user=self.normal_user)
        payment_response = self.client.patch(
            reverse("reservation-mark-payment", args=[reservation_id]),
            {"is_paid": True},
            format="json",
        )
        self.assertEqual(payment_response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(CASH_PAYMENT_CONFIRMATION_PASSWORD="cash-secret")
    def test_cash_payment_password_confirms_reservation_and_creates_transaction(self):
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation_id = create_response.data["id"]

        response = self.client.post(
            reverse("reservation-confirm-cash-payment", args=[reservation_id]),
            {"confirmation_password": "cash-secret", "notes": "Pago en caja"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["is_paid"])
        self.assertEqual(response.data["payment_status"], ReservationPaymentStatus.PAID)
        self.assertEqual(response.data["paid_amount"], "10000.00")
        self.assertEqual(len(response.data["payment_transactions"]), 1)
        self.assertEqual(response.data["payment_transactions"][0]["provider"], PaymentProvider.CASH)

        reservation = Reservation.objects.get(id=reservation_id)
        cash_transaction = PaymentTransaction.objects.get(reservation=reservation)
        self.assertEqual(cash_transaction.provider, PaymentProvider.CASH)
        self.assertEqual(cash_transaction.status, PaymentTransactionStatus.APPROVED)
        self.assertEqual(cash_transaction.base_amount, reservation.total_price)
        self.assertEqual(cash_transaction.identification_decimal, Decimal("0.00"))
        self.assertEqual(cash_transaction.amount_received, reservation.total_price)

    @override_settings(CASH_PAYMENT_CONFIRMATION_PASSWORD="cash-secret")
    def test_cash_payment_requires_valid_password(self):
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation_id = create_response.data["id"]

        response = self.client.post(
            reverse("reservation-confirm-cash-payment", args=[reservation_id]),
            {"confirmation_password": "wrong"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(PaymentTransaction.objects.count(), 0)

    @patch("reservations.services.mercadopago_service.create_checkout_preference_for_reservation_payment")
    def test_create_total_payment_link_uses_checkout_pro_decimal_identifier(self, mocked_create_preference):
        mocked_create_preference.return_value = {
            "id": "pref_total_1",
            "init_point": "https://mercadopago.example/checkout/total",
        }
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation_id = create_response.data["id"]

        response = self.client.post(
            reverse("reservation-create-payment-link", args=[reservation_id]),
            {"amount": "10000.00", "payment_type": PaymentType.TOTAL},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["amount"], "10000.00")
        self.assertEqual(response.data["mp_amount"], "10000.19")
        payment_transaction = PaymentTransaction.objects.get(id=response.data["payment_transaction_id"])
        self.assertEqual(payment_transaction.status, PaymentTransactionStatus.PENDING)
        self.assertEqual(payment_transaction.base_amount, Decimal("10000.00"))
        self.assertEqual(payment_transaction.identification_decimal, Decimal("0.19"))
        self.assertEqual(payment_transaction.mp_amount, Decimal("10000.19"))
        self.assertTrue(payment_transaction.external_reference.startswith(f"TENIS-RESERVA-{reservation_id}-TOTAL"))

        preference_transaction = mocked_create_preference.call_args.args[0]
        self.assertEqual(preference_transaction.id, payment_transaction.id)

    @patch("reservations.services.mercadopago_service.create_checkout_preference_for_reservation_payment")
    def test_create_player_payment_link_tracks_player(self, mocked_create_preference):
        mocked_create_preference.return_value = {
            "id": "pref_player_1",
            "init_point": "https://mercadopago.example/checkout/player",
        }
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation = Reservation.objects.get(id=create_response.data["id"])
        player = reservation.players.order_by("id").first()

        response = self.client.post(
            reverse("reservation-create-payment-link", args=[reservation.id]),
            {"amount": str(player.price_applied), "payment_type": PaymentType.PLAYER, "player_id": player.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        payment_transaction = PaymentTransaction.objects.get(id=response.data["payment_transaction_id"])
        self.assertEqual(payment_transaction.player_id, player.id)
        self.assertTrue(
            payment_transaction.external_reference.startswith(
                f"TENIS-RESERVA-{reservation.id}-JUGADOR-{player.id}"
            )
        )

    @patch("reservations.services.mercadopago_service.get_payment")
    @patch("reservations.services.mercadopago_service.create_checkout_preference_for_reservation_payment")
    def test_webhook_approved_payment_updates_partial_and_paid_idempotently(
        self,
        mocked_create_preference,
        mocked_get_payment,
    ):
        mocked_create_preference.side_effect = [
            {"id": "pref_player_1", "init_point": "https://mercadopago.example/checkout/player-1"},
            {"id": "pref_player_2", "init_point": "https://mercadopago.example/checkout/player-2"},
        ]
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation = Reservation.objects.get(id=create_response.data["id"])
        first_player, second_player = list(reservation.players.order_by("id"))

        first_link = self.client.post(
            reverse("reservation-create-payment-link", args=[reservation.id]),
            {
                "amount": str(first_player.price_applied),
                "payment_type": PaymentType.PLAYER,
                "player_id": first_player.id,
            },
            format="json",
        )
        second_link = self.client.post(
            reverse("reservation-create-payment-link", args=[reservation.id]),
            {
                "amount": str(second_player.price_applied),
                "payment_type": PaymentType.PLAYER,
                "player_id": second_player.id,
            },
            format="json",
        )
        first_transaction = PaymentTransaction.objects.get(id=first_link.data["payment_transaction_id"])
        second_transaction = PaymentTransaction.objects.get(id=second_link.data["payment_transaction_id"])

        mocked_get_payment.return_value = {
            "id": "mp_payment_1",
            "status": "approved",
            "status_detail": "accredited",
            "external_reference": first_transaction.external_reference,
            "transaction_amount": str(first_transaction.mp_amount),
            "payer": {"email": "player1@example.com"},
        }
        webhook_response = self.client.post(reverse("payment-webhook"), {"data": {"id": "mp_payment_1"}}, format="json")
        self.assertEqual(webhook_response.status_code, status.HTTP_200_OK)
        reservation.refresh_from_db()
        self.assertEqual(reservation.paid_amount, first_player.price_applied)
        self.assertEqual(reservation.payment_status, ReservationPaymentStatus.PARTIAL_PAYMENT)
        self.assertFalse(reservation.is_paid)

        duplicate_response = self.client.post(reverse("payment-webhook"), {"data": {"id": "mp_payment_1"}}, format="json")
        self.assertEqual(duplicate_response.status_code, status.HTTP_200_OK)
        reservation.refresh_from_db()
        self.assertEqual(reservation.paid_amount, first_player.price_applied)

        mocked_get_payment.return_value = {
            "id": "mp_payment_2",
            "status": "approved",
            "status_detail": "accredited",
            "external_reference": second_transaction.external_reference,
            "transaction_amount": str(second_transaction.mp_amount),
            "payer": {"email": "player2@example.com"},
        }
        self.client.post(reverse("payment-webhook"), {"data": {"id": "mp_payment_2"}}, format="json")
        reservation.refresh_from_db()
        self.assertEqual(reservation.paid_amount, reservation.total_price)
        self.assertEqual(reservation.payment_status, ReservationPaymentStatus.PAID)
        self.assertTrue(reservation.is_paid)
        self.assertIsNotNone(reservation.paid_at)

    def test_payment_status_endpoint_returns_players_and_transactions(self):
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation = Reservation.objects.get(id=create_response.data["id"])
        PaymentTransaction.objects.create(
            reservation=reservation,
            payment_type=PaymentType.TOTAL,
            external_reference=f"TENIS-RESERVA-{reservation.id}-TOTAL-test",
            base_amount=reservation.total_price,
            identification_decimal=Decimal("0.19"),
            mp_amount=reservation.total_price + Decimal("0.19"),
        )

        response = self.client.get(reverse("reservation-payment-status", args=[reservation.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], reservation.id)
        self.assertEqual(response.data["total_amount"], "10000.00")
        self.assertEqual(response.data["remaining_amount"], "10000.00")
        self.assertEqual(len(response.data["players"]), 2)
        self.assertEqual(len(response.data["payment_transactions"]), 1)

    def test_search_payable_reservations_by_player_full_name(self):
        payable_players = [
            {"first_name": "Jose", "last_name": "Hernandez", "is_member": True},
            {"first_name": "Santi", "last_name": "Fernandez", "is_member": False},
        ]
        payable_response = self.client.post(
            reverse("reservation-list"),
            self._reservation_payload(players=payable_players),
            format="json",
        )
        payable_reservation_id = payable_response.data["id"]

        paid_response = self.client.post(
            reverse("reservation-list"),
            self._reservation_payload(start_time="20:00", players=payable_players),
            format="json",
        )
        paid_reservation = Reservation.objects.get(id=paid_response.data["id"])
        paid_reservation.payment_status = ReservationPaymentStatus.PAID
        paid_reservation.is_paid = True
        paid_reservation.paid_amount = paid_reservation.total_price
        paid_reservation.save(update_fields=("payment_status", "is_paid", "paid_amount", "updated_at"))

        expired_response = self.client.post(
            reverse("reservation-list"),
            self._reservation_payload(start_time="21:30", players=payable_players),
            format="json",
        )
        expired_reservation = Reservation.objects.get(id=expired_response.data["id"])
        expired_reservation.payment_status = ReservationPaymentStatus.EXPIRED
        expired_reservation.save(update_fields=("payment_status", "updated_at"))

        response = self.client.get(
            reverse("reservation-search-payments-by-player"),
            {"q": "jose hernandez"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], payable_reservation_id)
        self.assertEqual(response.data[0]["remaining_amount"], "10000.00")
        self.assertEqual(len(response.data[0]["matching_players"]), 1)
        self.assertEqual(response.data[0]["matching_players"][0]["first_name"], "Jose")

    def test_search_payable_reservations_by_contact_name(self):
        payload = self._reservation_payload(
            players=[
                {"first_name": "Jose", "last_name": "Hernandez", "is_member": True},
                {"first_name": "Santi", "last_name": "Fernandez", "is_member": False},
            ]
        )
        payload["contact_name"] = "Maria Gomez"
        create_response = self.client.post(reverse("reservation-list"), payload, format="json")
        reservation_id = create_response.data["id"]

        response = self.client.get(
            reverse("reservation-search-payments-by-player"),
            {"q": "maria gomez"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], reservation_id)
        self.assertEqual(response.data[0]["contact_name"], "Maria Gomez")
        self.assertEqual(response.data[0]["matching_players"], [])

    def test_search_payable_reservations_includes_past_unpaid_reservation(self):
        start_datetime = timezone.now() - timedelta(days=1)
        reservation = Reservation.objects.create(
            court=self.court,
            reservation_type=ReservationType.NORMAL,
            game_mode=GameMode.SINGLES,
            contact_name="Lucre",
            contact_phone="2302691967",
            start_datetime=start_datetime,
            end_datetime=start_datetime + timedelta(minutes=90),
            status=ReservationStatus.CONFIRMED,
            total_price=Decimal("16000.00"),
            paid_amount=Decimal("0.00"),
            payment_status=ReservationPaymentStatus.PENDING_PAYMENT,
        )
        ReservationPlayer.objects.create(
            reservation=reservation,
            first_name="Lucre",
            last_name="Fraire",
            is_member=True,
            price_applied=Decimal("8000.00"),
        )

        response = self.client.get(
            reverse("reservation-search-payments-by-player"),
            {"q": "Lucre"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], reservation.id)
        self.assertEqual(response.data[0]["contact_name"], "Lucre")

    def test_search_payable_reservations_requires_minimum_query_length(self):
        response = self.client.get(
            reverse("reservation-search-payments-by-player"),
            {"q": "jo"},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("q", response.data)

    def test_public_can_list_today_pending_payment_reservations(self):
        today = timezone.localdate()
        fixed_now = timezone.make_aware(
            datetime.combine(today, time(hour=8, minute=0)),
            timezone.get_current_timezone(),
        )
        with patch("reservations.services.timezone.now", return_value=fixed_now):
            pending_response = self.client.post(
                reverse("reservation-list"),
                self._reservation_payload(target_date=today, start_time="11:00"),
                format="json",
            )
            paid_response = self.client.post(
                reverse("reservation-list"),
                self._reservation_payload(target_date=today, start_time="13:00"),
                format="json",
            )
        self.assertEqual(pending_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(paid_response.status_code, status.HTTP_201_CREATED)
        paid_reservation = Reservation.objects.get(id=paid_response.data["id"])
        paid_reservation.payment_status = ReservationPaymentStatus.PAID
        paid_reservation.is_paid = True
        paid_reservation.paid_amount = paid_reservation.total_price
        paid_reservation.save(update_fields=("payment_status", "is_paid", "paid_amount", "updated_at"))
        tomorrow_response = self.client.post(
            reverse("reservation-list"),
            self._reservation_payload(target_date=today + timedelta(days=1), start_time="11:00"),
            format="json",
        )
        self.assertEqual(tomorrow_response.status_code, status.HTTP_201_CREATED)

        response = self.client.get(reverse("reservation-pending-payments-today"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], pending_response.data["id"])
        self.assertEqual(response.data[0]["remaining_amount"], "10000.00")
        self.assertEqual(len(response.data[0]["players"]), 2)

    def test_admin_can_export_mercadopago_monthly_report_csv(self):
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation = Reservation.objects.get(id=create_response.data["id"])
        player = reservation.players.order_by("id").first()
        now = timezone.now()
        approved_transaction = PaymentTransaction.objects.create(
            reservation=reservation,
            player=player,
            payment_type=PaymentType.PLAYER,
            preference_id="pref_approved",
            payment_id="mp_approved_1",
            external_reference=f"TENIS-RESERVA-{reservation.id}-JUGADOR-{player.id}-report",
            status=PaymentTransactionStatus.APPROVED,
            status_detail="accredited",
            base_amount=player.price_applied,
            identification_decimal=Decimal("0.19"),
            mp_amount=player.price_applied + Decimal("0.19"),
            amount_received=player.price_applied + Decimal("0.19"),
            payer_email="jose@example.com",
            paid_at=now,
        )
        PaymentTransaction.objects.create(
            reservation=reservation,
            payment_type=PaymentType.TOTAL,
            preference_id="pref_rejected",
            payment_id="mp_rejected_1",
            external_reference=f"TENIS-RESERVA-{reservation.id}-TOTAL-report-rejected",
            status=PaymentTransactionStatus.REJECTED,
            status_detail="cc_rejected_other_reason",
            base_amount=reservation.total_price,
            identification_decimal=Decimal("0.19"),
            mp_amount=reservation.total_price + Decimal("0.19"),
            amount_received=reservation.total_price + Decimal("0.19"),
        )

        self.client.force_authenticate(user=self.admin)
        response = self.client.get(
            reverse("mercadopago-report-csv"),
            {
                "start_date": timezone.localdate().isoformat(),
                "end_date": timezone.localdate().isoformat(),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        rows = list(csv.DictReader(StringIO(response.content.decode("utf-8"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["nro_operacion_mp"], approved_transaction.payment_id)
        self.assertEqual(rows[0]["metodo_pago"], PaymentProvider.MERCADOPAGO)
        self.assertEqual(rows[0]["estado"], PaymentTransactionStatus.APPROVED)
        self.assertEqual(rows[0]["monto_reserva"], str(player.price_applied))
        self.assertEqual(rows[0]["monto_cobrado_mp"], str(player.price_applied + Decimal("0.19")))
        self.assertEqual(rows[0]["external_reference"], approved_transaction.external_reference)

    @override_settings(CASH_PAYMENT_CONFIRMATION_PASSWORD="cash-secret")
    def test_admin_export_monthly_report_csv_includes_cash_payments(self):
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation_id = create_response.data["id"]
        cash_response = self.client.post(
            reverse("reservation-confirm-cash-payment", args=[reservation_id]),
            {"confirmation_password": "cash-secret"},
            format="json",
        )
        self.assertEqual(cash_response.status_code, status.HTTP_201_CREATED)

        self.client.force_authenticate(user=self.admin)
        response = self.client.get(
            reverse("mercadopago-report-csv"),
            {
                "start_date": timezone.localdate().isoformat(),
                "end_date": timezone.localdate().isoformat(),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = list(csv.DictReader(StringIO(response.content.decode("utf-8"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["metodo_pago"], PaymentProvider.CASH)
        self.assertEqual(rows[0]["estado"], PaymentTransactionStatus.APPROVED)
        self.assertEqual(rows[0]["monto_reserva"], "10000.00")
        self.assertEqual(rows[0]["decimal_identificador"], "0.00")
        self.assertEqual(rows[0]["nro_operacion_mp"], "")

    def test_non_admin_cannot_export_mercadopago_report_csv(self):
        response = self.client.get(
            reverse("mercadopago-report-csv"),
            {
                "start_date": timezone.localdate().isoformat(),
                "end_date": timezone.localdate().isoformat(),
            },
        )

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_expire_pending_reservations_keeps_unpaid_slot_blocked(self):
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation = Reservation.objects.get(id=create_response.data["id"])
        reservation.payment_expires_at = timezone.now() - timedelta(minutes=1)
        reservation.save(update_fields=("payment_expires_at", "updated_at"))

        call_command("expire_pending_reservations")

        reservation.refresh_from_db()
        self.assertEqual(reservation.payment_status, ReservationPaymentStatus.PENDING_PAYMENT)
        second_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        self.assertEqual(second_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_expire_pending_reservations_keeps_partial_payment_without_admin_review(self):
        create_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        reservation = Reservation.objects.get(id=create_response.data["id"])
        first_player = reservation.players.order_by("id").first()
        reservation.paid_amount = first_player.price_applied
        reservation.payment_status = ReservationPaymentStatus.PARTIAL_PAYMENT
        reservation.payment_expires_at = timezone.now() - timedelta(minutes=1)
        reservation.save(
            update_fields=("paid_amount", "payment_status", "payment_expires_at", "updated_at")
        )

        call_command("expire_pending_reservations")

        reservation.refresh_from_db()
        self.assertEqual(reservation.payment_status, ReservationPaymentStatus.PARTIAL_PAYMENT)
        self.assertFalse(reservation.requires_admin_review)
        second_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        self.assertEqual(second_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_run_scheduled_tasks_runs_current_scheduler_tasks(self):
        output = StringIO()
        with patch(
            "reservations.management.commands.run_scheduled_tasks.generate_recurring_reservations",
            return_value=3,
        ) as recurring_mock:
            call_command("run_scheduled_tasks", "--days-ahead=14", stdout=output)

        recurring_mock.assert_called_once_with(days_ahead=14)
        self.assertIn("Expired without payment: 0", output.getvalue())
        self.assertIn("Marked for review: 0", output.getvalue())
        self.assertIn("Generated recurring reservations: 3", output.getvalue())

    def test_run_scheduled_tasks_skips_when_lock_exists(self):
        output = StringIO()
        with TemporaryDirectory() as lock_dir:
            os.mkdir(os.path.join(lock_dir, "csitenis_run_scheduled_tasks.lock"))
            with patch(
                "reservations.management.commands.run_scheduled_tasks.generate_recurring_reservations"
            ) as recurring_mock:
                call_command("run_scheduled_tasks", lock_dir=lock_dir, stdout=output)

        recurring_mock.assert_not_called()
        self.assertIn("already running", output.getvalue())

    def test_admin_can_filter_unpaid_reservations(self):
        first_response = self.client.post(reverse("reservation-list"), self._reservation_payload(), format="json")
        second_response = self.client.post(
            reverse("reservation-list"),
            self._reservation_payload(start_time="20:00"),
            format="json",
        )
        first_id = first_response.data["id"]
        second_id = second_response.data["id"]

        self.client.force_authenticate(user=self.admin)
        self.client.patch(
            reverse("reservation-mark-payment", args=[second_id]),
            {"is_paid": True},
            format="json",
        )
        unpaid_response = self.client.get(f"{reverse('reservation-list')}?is_paid=false")
        self.assertEqual(unpaid_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(unpaid_response.data), 1)
        self.assertEqual(unpaid_response.data[0]["id"], first_id)
        self.assertFalse(unpaid_response.data[0]["is_paid"])

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

    def test_recurring_rule_create_endpoint_generates_90_day_horizon(self):
        today = timezone.localdate()
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            reverse("recurring-rule-list"),
            {
                "court": self.court.id,
                "title": "Clase Diario",
                "days_of_week": list(DayOfWeek.values),
                "start_time": "10:00",
                "start_date": today.isoformat(),
                "end_date": None,
                "active": True,
                "notes": "Horizonte 90 dias",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created_rule_id = response.data["id"]
        generated = Reservation.objects.filter(
            reservation_type=ReservationType.CLASS,
            recurring_rule_id=created_rule_id,
        ).order_by("-start_datetime")
        self.assertEqual(generated.count(), 91)
        self.assertEqual(generated.first().start_datetime.date(), today + timedelta(days=90))

    def test_recurring_rule_update_regenerates_future_classes(self):
        initial_date = timezone.localdate() + timedelta(days=1)
        initial_day = DayOfWeek.values[initial_date.weekday()]
        self.client.force_authenticate(user=self.admin)

        create_response = self.client.post(
            reverse("recurring-rule-list"),
            {
                "court": self.court.id,
                "title": "Clase Reprogramable",
                "days_of_week": [initial_day],
                "start_time": "17:00",
                "start_date": initial_date.isoformat(),
                "end_date": initial_date.isoformat(),
                "active": True,
                "notes": "",
            },
            format="json",
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        rule_id = create_response.data["id"]
        old_class = Reservation.objects.get(
            reservation_type=ReservationType.CLASS,
            recurring_rule_id=rule_id,
            start_datetime__date=initial_date,
        )
        self.assertEqual(old_class.status, ReservationStatus.CONFIRMED)

        updated_date = timezone.localdate() + timedelta(days=2)
        updated_day = DayOfWeek.values[updated_date.weekday()]
        update_response = self.client.patch(
            reverse("recurring-rule-detail", args=[rule_id]),
            {
                "days_of_week": [updated_day],
                "start_time": "18:00",
                "start_date": updated_date.isoformat(),
                "end_date": updated_date.isoformat(),
            },
            format="json",
        )
        self.assertEqual(update_response.status_code, status.HTTP_200_OK)

        old_class.refresh_from_db()
        self.assertEqual(old_class.status, ReservationStatus.CANCELLED)
        new_classes = Reservation.objects.filter(
            reservation_type=ReservationType.CLASS,
            recurring_rule_id=rule_id,
            start_datetime__date=updated_date,
            status=ReservationStatus.CONFIRMED,
        )
        self.assertEqual(new_classes.count(), 1)

    def test_recurring_rule_allows_null_notes(self):
        target_date = timezone.localdate() + timedelta(days=1)
        day = DayOfWeek.values[target_date.weekday()]
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            reverse("recurring-rule-list"),
            {
                "court": self.court.id,
                "title": "Clase sin notas",
                "days_of_week": [day],
                "start_time": "16:00",
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "active": True,
                "notes": None,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["notes"], "")

    def test_deactivate_recurring_rule_cancels_future_classes_and_frees_slot(self):
        target_date = timezone.localdate() + timedelta(days=1)
        day = DayOfWeek.values[target_date.weekday()]
        self.client.force_authenticate(user=self.admin)

        first_rule_response = self.client.post(
            reverse("recurring-rule-list"),
            {
                "court": self.court.id,
                "title": "Clase Juan",
                "days_of_week": [day],
                "start_time": "14:00",
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "active": True,
                "notes": "",
            },
            format="json",
        )
        self.assertEqual(first_rule_response.status_code, status.HTTP_201_CREATED)
        first_rule_id = first_rule_response.data["id"]
        first_classes = Reservation.objects.filter(
            reservation_type=ReservationType.CLASS,
            recurring_rule_id=first_rule_id,
        )
        self.assertEqual(first_classes.count(), 1)
        self.assertEqual(first_classes.first().status, ReservationStatus.CONFIRMED)

        deactivate_response = self.client.patch(
            reverse("recurring-rule-deactivate", args=[first_rule_id]),
            {"cancellation_reason": "Profesor no disponible"},
            format="json",
        )
        self.assertEqual(deactivate_response.status_code, status.HTTP_200_OK)
        self.assertEqual(deactivate_response.data["cancelled_future_classes"], 1)

        first_class = first_classes.first()
        first_class.refresh_from_db()
        self.assertEqual(first_class.status, ReservationStatus.CANCELLED)

        second_rule_response = self.client.post(
            reverse("recurring-rule-list"),
            {
                "court": self.court.id,
                "title": "Clase Josefina",
                "days_of_week": [day],
                "start_time": "14:00",
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "active": True,
                "notes": "",
            },
            format="json",
        )
        self.assertEqual(second_rule_response.status_code, status.HTTP_201_CREATED)
        second_rule_id = second_rule_response.data["id"]
        second_classes = Reservation.objects.filter(
            reservation_type=ReservationType.CLASS,
            recurring_rule_id=second_rule_id,
            status=ReservationStatus.CONFIRMED,
        )
        self.assertEqual(second_classes.count(), 1)

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


class ResetLaunchDataCommandTests(TestCase):
    def test_reset_launch_data_dry_run_does_not_delete_transactional_data(self):
        court = Court.objects.create(name="Cancha test", active=True)
        reservation = Reservation.objects.create(
            court=court,
            reservation_type=ReservationType.NORMAL,
            game_mode=GameMode.SINGLES,
            contact_name="Cliente Test",
            contact_phone="2302000000",
            start_datetime=timezone.now() + timedelta(days=1),
            end_datetime=timezone.now() + timedelta(days=1, minutes=90),
            status=ReservationStatus.CONFIRMED,
            total_price=Decimal("10000.00"),
        )
        PaymentTransaction.objects.create(
            reservation=reservation,
            payment_type=PaymentType.TOTAL,
            external_reference="TENIS-RESERVA-TEST-DRY-RUN",
            base_amount=Decimal("10000.00"),
            identification_decimal=Decimal("0.19"),
            mp_amount=Decimal("10000.19"),
        )

        output = StringIO()
        call_command("reset_launch_data", stdout=output)

        self.assertIn("Dry run only", output.getvalue())
        self.assertEqual(Reservation.objects.count(), 1)
        self.assertEqual(PaymentTransaction.objects.count(), 1)

    def test_reset_launch_data_deletes_transactional_data_and_preserves_configuration(self):
        court = Court.objects.create(name="Cancha test", active=True)
        PriceRule.objects.create(
            game_mode=GameMode.SINGLES,
            player_type=PlayerType.MEMBER,
            price=Decimal("8000.00"),
            active=True,
        )
        recurring_rule = RecurringReservationRule.objects.create(
            court=court,
            title="Clase fija",
            days_of_week=[DayOfWeek.MONDAY],
            start_time=time(hour=10, minute=0),
            start_date=timezone.localdate(),
            active=True,
        )
        reservation = Reservation.objects.create(
            court=court,
            reservation_type=ReservationType.NORMAL,
            game_mode=GameMode.SINGLES,
            contact_name="Cliente Test",
            contact_phone="2302000000",
            start_datetime=timezone.now() + timedelta(days=1),
            end_datetime=timezone.now() + timedelta(days=1, minutes=90),
            status=ReservationStatus.CONFIRMED,
            total_price=Decimal("10000.00"),
        )
        player = ReservationPlayer.objects.create(
            reservation=reservation,
            first_name="Cliente",
            last_name="Test",
            is_member=True,
            price_applied=Decimal("8000.00"),
        )
        PaymentTransaction.objects.create(
            reservation=reservation,
            player=player,
            payment_type=PaymentType.PLAYER,
            external_reference="TENIS-RESERVA-TEST-RESET",
            base_amount=Decimal("8000.00"),
            identification_decimal=Decimal("0.19"),
            mp_amount=Decimal("8000.19"),
        )
        CancellationRequest.objects.create(
            reservation=reservation,
            requester_name="Cliente Test",
            requester_phone="2302000000",
            reason="Prueba",
        )
        NotificationLog.objects.create(
            reservation=reservation,
            channel=NotificationChannel.PUSH,
            destination="test-device",
        )
        BlockedSlot.objects.create(
            court=court,
            start_datetime=timezone.now() + timedelta(days=2),
            end_datetime=timezone.now() + timedelta(days=2, hours=1),
            block_type=BlockType.OTHER,
            reason="Bloqueo real",
        )

        output = StringIO()
        call_command("reset_launch_data", "--confirm", "RESET_LAUNCH_DATA", stdout=output)

        self.assertIn("Launch data reset completed", output.getvalue())
        self.assertEqual(Reservation.objects.count(), 0)
        self.assertEqual(ReservationPlayer.objects.count(), 0)
        self.assertEqual(PaymentTransaction.objects.count(), 0)
        self.assertEqual(CancellationRequest.objects.count(), 0)
        self.assertEqual(NotificationLog.objects.count(), 0)
        self.assertEqual(BlockedSlot.objects.count(), 1)
        self.assertEqual(Court.objects.filter(id=court.id).count(), 1)
        self.assertEqual(PriceRule.objects.count(), 1)
        self.assertEqual(RecurringReservationRule.objects.filter(id=recurring_rule.id).count(), 1)

    def test_reset_launch_data_can_delete_blocked_slots_when_requested(self):
        court = Court.objects.create(name="Cancha test", active=True)
        BlockedSlot.objects.create(
            court=court,
            start_datetime=timezone.now() + timedelta(days=2),
            end_datetime=timezone.now() + timedelta(days=2, hours=1),
            block_type=BlockType.OTHER,
            reason="Bloqueo de prueba",
        )

        call_command(
            "reset_launch_data",
            "--confirm",
            "RESET_LAUNCH_DATA",
            "--delete-blocked-slots",
            stdout=StringIO(),
        )

        self.assertEqual(BlockedSlot.objects.count(), 0)
        self.assertEqual(Court.objects.filter(id=court.id).count(), 1)
