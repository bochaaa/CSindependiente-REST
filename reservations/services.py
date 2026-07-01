from __future__ import annotations

from hmac import compare_digest
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import logging
import re
from uuid import uuid4

from django.db import OperationalError, transaction
from django.conf import settings
from django.db.models import F, Q, Sum
from django.utils import timezone
from rest_framework import serializers

from payments.services import mercadopago_service

from .models import (
    BlockedSlot,
    CancellationRequest,
    CancellationRequestStatus,
    Court,
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
    ClubSchedule,
)
from .push_notifications import (
    InvalidPushTokenError,
    PushNotificationsNotConfigured,
    send_firebase_push,
)

logger = logging.getLogger(__name__)

NORMAL_RESERVATION_MINUTES = 90
CLASS_RESERVATION_MINUTES = 60
SAME_DAY_RESERVATION_MIN_ADVANCE_HOURS = 3
RECURRING_GENERATION_RETRY_ATTEMPTS = 3
PAYMENT_REFERENCE_RE = re.compile(r"^TENIS-RESERVA-(?P<reservation_id>\d+)-")

DAY_OF_WEEK_BY_INDEX = {
    0: DayOfWeek.MONDAY,
    1: DayOfWeek.TUESDAY,
    2: DayOfWeek.WEDNESDAY,
    3: DayOfWeek.THURSDAY,
    4: DayOfWeek.FRIDAY,
    5: DayOfWeek.SATURDAY,
    6: DayOfWeek.SUNDAY,
}


@dataclass
class PlayerPricingResult:
    first_name: str
    last_name: str
    is_member: bool
    price_applied: Decimal


def combine_local_datetime(target_date: date, target_time: time) -> datetime:
    tz = timezone.get_current_timezone()
    naive_value = datetime.combine(target_date, target_time)
    return timezone.make_aware(naive_value, tz)


def get_schedule_for_date(target_date: date) -> tuple[time, time] | None:
    special_schedule = SpecialSchedule.objects.filter(date=target_date).first()
    if special_schedule:
        if special_schedule.closed:
            return None
        if special_schedule.open_time is None or special_schedule.close_time is None:
            return None
        return special_schedule.open_time, special_schedule.close_time

    day_of_week = DAY_OF_WEEK_BY_INDEX[target_date.weekday()]
    club_schedule = ClubSchedule.objects.filter(day_of_week=day_of_week, active=True).first()
    if not club_schedule:
        return None
    return club_schedule.open_time, club_schedule.close_time


def validate_players_for_game_mode(game_mode: str, players: list[dict]):
    expected_count = 2 if game_mode == GameMode.SINGLES else 4
    if game_mode not in (GameMode.SINGLES, GameMode.DOUBLES):
        raise serializers.ValidationError({"game_mode": "game_mode must be SINGLES or DOUBLES."})
    if len(players) != expected_count:
        raise serializers.ValidationError(
            {"players": f"{game_mode} requires exactly {expected_count} players."}
        )


def get_active_price_rule(game_mode: str, player_type: str, target_date: date) -> PriceRule:
    price_rule = (
        PriceRule.objects.filter(
            game_mode=game_mode,
            player_type=player_type,
            active=True,
            valid_from__lte=target_date,
        )
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=target_date))
        .order_by("-valid_from", "-id")
        .first()
    )
    if not price_rule:
        raise serializers.ValidationError(
            {"detail": f"No existe precio activo para {game_mode} + {player_type}"}
        )
    return price_rule


def get_payment_expiration_datetime() -> datetime | None:
    return None


def get_identification_decimal() -> Decimal:
    return Decimal(str(settings.MP_IDENTIFICATION_DECIMAL)).quantize(Decimal("0.01"))


def quantize_money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def get_blocking_reservation_queryset():
    return Reservation.objects.exclude(status=ReservationStatus.CANCELLED).exclude(
        payment_status__in=(
            ReservationPaymentStatus.EXPIRED,
            ReservationPaymentStatus.CANCELLED,
            ReservationPaymentStatus.REJECTED,
        )
    )


def build_reservation_created_push_payload(reservation: Reservation) -> dict:
    start_datetime = timezone.localtime(reservation.start_datetime)
    return {
        "title": "Nueva reserva",
        "body": f"{reservation.court.name} - {start_datetime.strftime('%H:%M')} hs",
        "data": {
            "type": "reservation_created",
            "reservation_id": str(reservation.id),
            "url": f"/admin/reservations?date={start_datetime.date().isoformat()}",
        },
    }


def queue_reservation_created_push_notifications(reservation: Reservation) -> int:
    payload = build_reservation_created_push_payload(reservation)
    devices = NotificationDevice.objects.filter(
        enabled=True,
        provider=NotificationProvider.FCM,
        user__is_active=True,
        user__is_staff=True,
    ).values_list("token", flat=True)
    logs = [
        NotificationLog(
            reservation=reservation,
            channel=NotificationChannel.PUSH,
            destination=token,
            status=NotificationStatus.PENDING,
            payload=payload,
        )
        for token in devices
    ]
    if not logs:
        return 0
    created_logs = NotificationLog.objects.bulk_create(logs)
    log_ids = [log.id for log in created_logs if log.id]
    if log_ids:
        transaction.on_commit(lambda: send_pending_push_notifications(log_ids=log_ids))
    return len(logs)


def send_push_notification_log(notification_log: NotificationLog) -> bool:
    if notification_log.channel != NotificationChannel.PUSH:
        return False
    if notification_log.status != NotificationStatus.PENDING:
        return False
    try:
        send_firebase_push(
            token=notification_log.destination,
            payload=notification_log.payload,
        )
    except PushNotificationsNotConfigured as exc:
        logger.warning("Push notifications are not configured: %s", exc)
        notification_log.status = NotificationStatus.FAILED
        notification_log.error_message = str(exc)
        notification_log.save(update_fields=("status", "error_message", "updated_at"))
        return False
    except InvalidPushTokenError as exc:
        now = timezone.now()
        NotificationDevice.objects.filter(token=notification_log.destination).update(
            enabled=False,
            updated_at=now,
        )
        notification_log.status = NotificationStatus.FAILED
        notification_log.error_message = str(exc)
        notification_log.save(update_fields=("status", "error_message", "updated_at"))
        return False
    except Exception as exc:
        notification_log.status = NotificationStatus.FAILED
        notification_log.error_message = str(exc)
        notification_log.save(update_fields=("status", "error_message", "updated_at"))
        return False

    notification_log.status = NotificationStatus.SENT
    notification_log.error_message = ""
    notification_log.save(update_fields=("status", "error_message", "updated_at"))
    return True


def send_pending_push_notifications(log_ids: list[int] | None = None, limit: int = 100) -> dict:
    if not settings.PUSH_NOTIFICATIONS_ENABLED:
        return {"sent": 0, "failed": 0, "skipped": 0}

    queryset = NotificationLog.objects.filter(
        channel=NotificationChannel.PUSH,
        status=NotificationStatus.PENDING,
    ).order_by("created_at")
    if log_ids is not None:
        queryset = queryset.filter(id__in=log_ids)

    sent = 0
    failed = 0
    skipped = 0
    for notification_log in queryset[:limit]:
        was_sent = send_push_notification_log(notification_log)
        notification_log.refresh_from_db(fields=("status",))
        if was_sent:
            sent += 1
        elif notification_log.status == NotificationStatus.FAILED:
            failed += 1
        else:
            skipped += 1
    return {"sent": sent, "failed": failed, "skipped": skipped}


def calculate_players_prices(
    game_mode: str, players: list[dict], target_date: date
) -> tuple[list[PlayerPricingResult], Decimal]:
    validate_players_for_game_mode(game_mode=game_mode, players=players)
    result: list[PlayerPricingResult] = []
    total_price = Decimal("0.00")
    for player in players:
        player_type = PlayerType.MEMBER if player["is_member"] else PlayerType.NON_MEMBER
        price_rule = get_active_price_rule(
            game_mode=game_mode,
            player_type=player_type,
            target_date=target_date,
        )
        result.append(
            PlayerPricingResult(
                first_name=player["first_name"],
                last_name=player["last_name"],
                is_member=player["is_member"],
                price_applied=price_rule.price,
            )
        )
        total_price += price_rule.price
    return result, total_price


def check_overlap(
    court: Court,
    start_datetime: datetime,
    end_datetime: datetime,
    exclude_reservation_id: int | None = None,
) -> bool:
    reservation_qs = get_blocking_reservation_queryset().filter(court=court)
    if exclude_reservation_id:
        reservation_qs = reservation_qs.exclude(id=exclude_reservation_id)
    reservation_overlap_exists = reservation_qs.filter(
        start_datetime__lt=end_datetime,
        end_datetime__gt=start_datetime,
    ).exists()
    if reservation_overlap_exists:
        return True
    return BlockedSlot.objects.filter(
        court=court,
        start_datetime__lt=end_datetime,
        end_datetime__gt=start_datetime,
    ).exists()


def validate_reservation_datetime(start_datetime: datetime, end_datetime: datetime):
    now = timezone.localtime()
    if start_datetime.date() < now.date():
        raise serializers.ValidationError({"date": "No se permite reservar en fecha pasada."})
    same_day_min_start_datetime = now + timedelta(hours=SAME_DAY_RESERVATION_MIN_ADVANCE_HOURS)
    if start_datetime.date() == now.date() and start_datetime < same_day_min_start_datetime:
        raise serializers.ValidationError(
            {
                "start_time": (
                    "Si la reserva es para hoy, debe hacerse con al menos "
                    f"{SAME_DAY_RESERVATION_MIN_ADVANCE_HOURS} horas de anticipacion."
                )
            }
        )
    schedule = get_schedule_for_date(start_datetime.date())
    if not schedule:
        raise serializers.ValidationError({"detail": "El club no abre en la fecha solicitada."})
    open_time, close_time = schedule
    open_datetime = combine_local_datetime(start_datetime.date(), open_time)
    close_datetime = combine_local_datetime(start_datetime.date(), close_time)
    if start_datetime < open_datetime or end_datetime > close_datetime:
        raise serializers.ValidationError({"detail": "La reserva esta fuera del horario del club."})


@transaction.atomic
def create_reservation(data: dict, created_by=None) -> Reservation:
    court = Court.objects.select_for_update().filter(id=data["court"], active=True).first()
    if not court:
        raise serializers.ValidationError({"court": "La cancha no existe o no esta activa."})

    start_datetime = combine_local_datetime(data["date"], data["start_time"])
    end_datetime = start_datetime + timedelta(minutes=NORMAL_RESERVATION_MINUTES)
    validate_reservation_datetime(start_datetime=start_datetime, end_datetime=end_datetime)

    if check_overlap(court=court, start_datetime=start_datetime, end_datetime=end_datetime):
        raise serializers.ValidationError({"detail": "La cancha no esta disponible en ese horario."})

    players_input = data["players"]
    priced_players, total_price = calculate_players_prices(
        game_mode=data["game_mode"],
        players=players_input,
        target_date=start_datetime.date(),
    )

    reservation = Reservation.objects.create(
        court=court,
        reservation_type=ReservationType.NORMAL,
        game_mode=data["game_mode"],
        contact_name=data["contact_name"],
        contact_phone=data["contact_phone"],
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        status=ReservationStatus.CONFIRMED,
        total_price=total_price,
        payment_status=ReservationPaymentStatus.PENDING_PAYMENT,
        payment_expires_at=get_payment_expiration_datetime(),
        notes=data.get("notes", ""),
        created_by=created_by if getattr(created_by, "is_authenticated", False) else None,
    )
    reservation.mp_external_reference_base = f"TENIS-RESERVA-{reservation.id}"
    reservation.save(update_fields=("mp_external_reference_base", "updated_at"))
    ReservationPlayer.objects.bulk_create(
        [
            ReservationPlayer(
                reservation=reservation,
                first_name=player.first_name,
                last_name=player.last_name,
                is_member=player.is_member,
                price_applied=player.price_applied,
            )
            for player in priced_players
        ]
    )

    # Placeholder logs for future async notifications. Reservation creation must not fail on notification issues.
    NotificationLog.objects.bulk_create(
        [
            NotificationLog(
                reservation=reservation,
                channel=NotificationChannel.WHATSAPP,
                destination="admin_whatsapp_pending",
                status=NotificationStatus.PENDING,
            ),
            NotificationLog(
                reservation=reservation,
                channel=NotificationChannel.EMAIL,
                destination="admin_email_pending",
                status=NotificationStatus.PENDING,
            ),
        ]
    )
    queue_reservation_created_push_notifications(reservation)
    return reservation


def generate_availability_for_date(target_date: date) -> dict:
    schedule = get_schedule_for_date(target_date=target_date)
    courts = Court.objects.filter(active=True).order_by("name")
    if not schedule:
        return {
            "date": target_date,
            "reservation_duration_minutes": NORMAL_RESERVATION_MINUTES,
            "courts": [
                {"id": court.id, "name": court.name, "available_ranges": [], "unavailable_ranges": []}
                for court in courts
            ],
        }

    open_time, close_time = schedule
    opening_datetime = combine_local_datetime(target_date, open_time)
    closing_datetime = combine_local_datetime(target_date, close_time)

    response_courts = []
    for court in courts:
        busy_intervals: list[dict] = []

        reservations = get_blocking_reservation_queryset().filter(
            court=court,
            start_datetime__lt=closing_datetime,
            end_datetime__gt=opening_datetime,
        ).values(
            "start_datetime",
            "end_datetime",
            "reservation_type",
            "contact_name",
            "title",
        )
        for reservation in reservations:
            start_datetime = reservation["start_datetime"]
            end_datetime = reservation["end_datetime"]
            clamped_start = max(start_datetime, opening_datetime)
            clamped_end = min(end_datetime, closing_datetime)
            if clamped_start < clamped_end:
                reservation_contacts = set()
                class_titles = set()
                reservation_types = set()
                if reservation["reservation_type"] == ReservationType.CLASS:
                    if reservation["title"]:
                        class_titles.add(reservation["title"])
                    reservation_types.add(ReservationType.CLASS)
                else:
                    if reservation["contact_name"]:
                        reservation_contacts.add(reservation["contact_name"])
                    reservation_types.add(ReservationType.NORMAL)
                busy_intervals.append(
                    {
                        "start": clamped_start,
                        "end": clamped_end,
                        "reasons": {"RESERVATION"},
                        "reservation_types": reservation_types,
                        "reservation_contact_names": reservation_contacts,
                        "class_titles": class_titles,
                        "block_reasons": set(),
                    }
                )

        blocked_slots = BlockedSlot.objects.filter(
            court=court,
            start_datetime__lt=closing_datetime,
            end_datetime__gt=opening_datetime,
        ).values("start_datetime", "end_datetime", "reason", "block_type")
        for blocked_slot in blocked_slots:
            start_datetime = blocked_slot["start_datetime"]
            end_datetime = blocked_slot["end_datetime"]
            clamped_start = max(start_datetime, opening_datetime)
            clamped_end = min(end_datetime, closing_datetime)
            if clamped_start < clamped_end:
                block_reasons = set()
                if blocked_slot["reason"]:
                    block_reasons.add(blocked_slot["reason"])
                else:
                    block_reasons.add(blocked_slot["block_type"])
                busy_intervals.append(
                    {
                        "start": clamped_start,
                        "end": clamped_end,
                        "reasons": {"BLOCKED"},
                        "reservation_types": set(),
                        "reservation_contact_names": set(),
                        "class_titles": set(),
                        "block_reasons": block_reasons,
                    }
                )

        busy_intervals.sort(key=lambda item: item["start"])
        merged_busy: list[dict] = []
        for interval in busy_intervals:
            if not merged_busy:
                merged_busy.append(interval)
                continue
            last = merged_busy[-1]
            if interval["start"] <= last["end"]:
                last["end"] = max(last["end"], interval["end"])
                last["reasons"].update(interval["reasons"])
                last["reservation_types"].update(interval["reservation_types"])
                last["reservation_contact_names"].update(interval["reservation_contact_names"])
                last["class_titles"].update(interval["class_titles"])
                last["block_reasons"].update(interval["block_reasons"])
            else:
                merged_busy.append(interval)

        unavailable_ranges = []
        for interval in merged_busy:
            reasons = interval["reasons"]
            reason_value = next(iter(reasons)) if len(reasons) == 1 else "MULTIPLE"
            reservation_contact_name = None
            if len(interval["reservation_contact_names"]) == 1:
                reservation_contact_name = next(iter(interval["reservation_contact_names"]))
            class_title = None
            if len(interval["class_titles"]) == 1:
                class_title = next(iter(interval["class_titles"]))
            block_reason = None
            if len(interval["block_reasons"]) == 1:
                block_reason = next(iter(interval["block_reasons"]))
            reservation_type = None
            if len(interval["reservation_types"]) == 1:
                reservation_type = next(iter(interval["reservation_types"]))
            unavailable_ranges.append(
                {
                    "start_time": timezone.localtime(interval["start"]).time(),
                    "end_time": timezone.localtime(interval["end"]).time(),
                    "reason": reason_value,
                    "reservation_type": reservation_type,
                    "reservation_contact_name": reservation_contact_name,
                    "class_title": class_title,
                    "block_reason": block_reason,
                }
            )

        available_ranges = []
        cursor = opening_datetime
        for interval in merged_busy:
            if cursor < interval["start"]:
                range_start = cursor
                range_end = interval["start"]
                duration_minutes = int((range_end - range_start).total_seconds() // 60)
                can_book_90_min = duration_minutes >= NORMAL_RESERVATION_MINUTES
                can_start_until = None
                if can_book_90_min:
                    can_start_until = timezone.localtime(
                        range_end - timedelta(minutes=NORMAL_RESERVATION_MINUTES)
                    ).time()
                available_ranges.append(
                    {
                        "start_time": timezone.localtime(range_start).time(),
                        "end_time": timezone.localtime(range_end).time(),
                        "duration_minutes": duration_minutes,
                        "can_book_90_min": can_book_90_min,
                        "can_start_until": can_start_until,
                    }
                )
            cursor = max(cursor, interval["end"])

        if cursor < closing_datetime:
            range_start = cursor
            range_end = closing_datetime
            duration_minutes = int((range_end - range_start).total_seconds() // 60)
            can_book_90_min = duration_minutes >= NORMAL_RESERVATION_MINUTES
            can_start_until = None
            if can_book_90_min:
                can_start_until = timezone.localtime(
                    range_end - timedelta(minutes=NORMAL_RESERVATION_MINUTES)
                ).time()
            available_ranges.append(
                {
                    "start_time": timezone.localtime(range_start).time(),
                    "end_time": timezone.localtime(range_end).time(),
                    "duration_minutes": duration_minutes,
                    "can_book_90_min": can_book_90_min,
                    "can_start_until": can_start_until,
                }
            )

        response_courts.append(
            {
                "id": court.id,
                "name": court.name,
                "available_ranges": available_ranges,
                "unavailable_ranges": unavailable_ranges,
            }
        )

    return {
        "date": target_date,
        "reservation_duration_minutes": NORMAL_RESERVATION_MINUTES,
        "courts": response_courts,
    }


def create_cancellation_request(reservation: Reservation, data: dict) -> CancellationRequest:
    if reservation.status == ReservationStatus.CANCELLED:
        raise serializers.ValidationError(
            {"detail": "No se puede solicitar cancelacion de una reserva cancelada."}
        )

    cutoff_datetime = reservation.start_datetime - timedelta(hours=3)
    if timezone.now() > cutoff_datetime:
        raise serializers.ValidationError(
            {
                "detail": (
                    "La solicitud de cancelacion solo se permite hasta 3 horas antes del turno. "
                    "Luego se procede con el cobro."
                )
            }
        )

    if CancellationRequest.objects.filter(
        reservation=reservation,
        status=CancellationRequestStatus.PENDING,
    ).exists():
        raise serializers.ValidationError(
            {"detail": "Ya existe una solicitud de cancelacion pendiente para esta reserva."}
        )
    cancellation_request = CancellationRequest.objects.create(
        reservation=reservation,
        requester_name=data["requester_name"],
        requester_phone=data["requester_phone"],
        reason=data["reason"],
    )
    if reservation.status == ReservationStatus.CONFIRMED:
        reservation.status = ReservationStatus.CANCELLATION_REQUESTED
        reservation.save(update_fields=("status", "updated_at"))
    return cancellation_request


def cancel_reservation_by_admin(
    reservation: Reservation, cancelled_by, cancellation_reason: str = ""
) -> Reservation:
    reservation.status = ReservationStatus.CANCELLED
    reservation.payment_status = ReservationPaymentStatus.CANCELLED
    reservation.cancelled_at = timezone.now()
    reservation.cancelled_by = cancelled_by
    reservation.cancellation_reason = cancellation_reason
    reservation.save(
        update_fields=(
            "status",
            "payment_status",
            "cancelled_at",
            "cancelled_by",
            "cancellation_reason",
            "updated_at",
        )
    )
    return reservation


@transaction.atomic
def set_reservation_payment_status(
    reservation: Reservation,
    is_paid: bool,
    confirmed_by,
) -> Reservation:
    locked_reservation = Reservation.objects.select_for_update().get(id=reservation.id)
    if is_paid and locked_reservation.status == ReservationStatus.CANCELLED:
        raise serializers.ValidationError(
            {"detail": "No se puede confirmar pago de una reserva cancelada."}
        )

    if locked_reservation.is_paid == is_paid:
        return locked_reservation

    if is_paid:
        cash_amount = locked_reservation.remaining_amount
        now = timezone.now()
        authenticated_user = _get_authenticated_user(confirmed_by)
        if cash_amount > 0:
            PaymentTransaction.objects.create(
                reservation=locked_reservation,
                provider=PaymentProvider.CASH,
                payment_type=PaymentType.TOTAL if cash_amount == locked_reservation.total_price else PaymentType.PARTIAL,
                external_reference=f"TENIS-RESERVA-{locked_reservation.id}-EFECTIVO-ADMIN-{uuid4().hex}",
                status=PaymentTransactionStatus.APPROVED,
                status_detail="admin_confirmed",
                base_amount=cash_amount,
                identification_decimal=Decimal("0.00"),
                mp_amount=cash_amount,
                amount_received=cash_amount,
                paid_at=now,
                raw_response={
                    "source": "admin_payment_mark",
                    "confirmed_by_id": authenticated_user.id if authenticated_user else None,
                    "confirmed_by_username": authenticated_user.username if authenticated_user else "",
                },
            )
        locked_reservation = recalculate_reservation_payment_state(locked_reservation, now=now)
        locked_reservation.requires_admin_review = False
        locked_reservation.paid_confirmed_by = authenticated_user
        locked_reservation.save(
            update_fields=("requires_admin_review", "paid_confirmed_by", "updated_at")
        )
        return locked_reservation
    else:
        now = timezone.now()
        PaymentTransaction.objects.filter(
            reservation=locked_reservation,
            provider=PaymentProvider.CASH,
            status=PaymentTransactionStatus.APPROVED,
        ).update(
            status=PaymentTransactionStatus.CANCELLED,
            status_detail="admin_reverted",
            paid_at=None,
            updated_at=now,
        )
        paid_amount = (
            PaymentTransaction.objects.filter(
                reservation=locked_reservation,
                status=PaymentTransactionStatus.APPROVED,
            ).aggregate(total=Sum("base_amount"))["total"]
            or Decimal("0.00")
        )
        locked_reservation.paid_amount = paid_amount
        locked_reservation.is_paid = False
        locked_reservation.payment_status = (
            ReservationPaymentStatus.PARTIAL_PAYMENT
            if paid_amount > 0
            else ReservationPaymentStatus.PENDING_PAYMENT
        )
        locked_reservation.paid_at = None
        locked_reservation.paid_confirmed_by = None

    locked_reservation.save(
        update_fields=(
            "is_paid",
            "payment_status",
            "paid_amount",
            "paid_at",
            "paid_confirmed_by",
            "updated_at",
        )
    )
    return locked_reservation


def validate_cash_payment_confirmation_password(confirmation_password: str):
    expected_password = getattr(settings, "CASH_PAYMENT_CONFIRMATION_PASSWORD", "")
    if not expected_password:
        raise serializers.ValidationError(
            {"confirmation_password": "No esta configurada la clave para confirmar pagos en efectivo."}
        )
    if not compare_digest(str(confirmation_password), str(expected_password)):
        raise serializers.ValidationError({"confirmation_password": "Clave de confirmacion incorrecta."})


def _get_authenticated_user(user):
    if getattr(user, "is_authenticated", False):
        return user
    return None


@transaction.atomic
def register_cash_payment(
    reservation: Reservation,
    confirmation_password: str,
    confirmed_by=None,
    amount=None,
    payment_type: str | None = None,
    player_id: int | None = None,
    notes: str = "",
) -> PaymentTransaction:
    validate_cash_payment_confirmation_password(confirmation_password)
    locked_reservation = Reservation.objects.select_for_update().get(id=reservation.id)
    if locked_reservation.status == ReservationStatus.CANCELLED:
        raise serializers.ValidationError({"detail": "No se puede registrar efectivo en una reserva cancelada."})
    if locked_reservation.payment_status == ReservationPaymentStatus.PAID:
        raise serializers.ValidationError({"detail": "La reserva ya esta pagada."})

    cash_amount = quantize_money(amount if amount is not None else locked_reservation.remaining_amount)
    if cash_amount <= 0:
        raise serializers.ValidationError({"amount": "El monto debe ser mayor a 0."})
    if cash_amount > locked_reservation.remaining_amount:
        raise serializers.ValidationError({"amount": "El monto no puede superar el saldo pendiente."})

    player = None
    if player_id is not None:
        player = ReservationPlayer.objects.filter(id=player_id, reservation=locked_reservation).first()
        if not player:
            raise serializers.ValidationError({"player_id": "El jugador no pertenece a esta reserva."})

    if payment_type == PaymentType.PLAYER and not player:
        raise serializers.ValidationError({"player_id": "player_id es requerido para pagos por jugador."})
    if player and payment_type is None:
        payment_type = PaymentType.PLAYER
    if payment_type is None:
        payment_type = (
            PaymentType.TOTAL
            if cash_amount == locked_reservation.remaining_amount
            else PaymentType.PARTIAL
        )

    now = timezone.now()
    authenticated_user = _get_authenticated_user(confirmed_by)
    payment_transaction = PaymentTransaction.objects.create(
        reservation=locked_reservation,
        player=player,
        provider=PaymentProvider.CASH,
        payment_type=payment_type,
        external_reference=f"TENIS-RESERVA-{locked_reservation.id}-EFECTIVO-{uuid4().hex}",
        status=PaymentTransactionStatus.APPROVED,
        status_detail="cash_confirmed",
        base_amount=cash_amount,
        identification_decimal=Decimal("0.00"),
        mp_amount=cash_amount,
        amount_received=cash_amount,
        paid_at=now,
        raw_response={
            "source": "cash_confirmation",
            "notes": notes,
            "confirmed_by_id": authenticated_user.id if authenticated_user else None,
            "confirmed_by_username": authenticated_user.username if authenticated_user else "",
        },
    )
    updated_reservation = recalculate_reservation_payment_state(locked_reservation, now=now)
    if updated_reservation.is_paid:
        updated_reservation.requires_admin_review = False
        if authenticated_user and not updated_reservation.paid_confirmed_by_id:
            updated_reservation.paid_confirmed_by = authenticated_user
        updated_reservation.save(
            update_fields=("requires_admin_review", "paid_confirmed_by", "updated_at")
        )
    logger.info(
        "Registered cash payment transaction=%s reservation=%s amount=%s",
        payment_transaction.id,
        locked_reservation.id,
        cash_amount,
    )
    return payment_transaction


def build_payment_external_reference(payment_transaction: PaymentTransaction) -> str:
    reservation_id = payment_transaction.reservation_id
    if payment_transaction.payment_type == PaymentType.TOTAL:
        return f"TENIS-RESERVA-{reservation_id}-TOTAL-{payment_transaction.id}"
    if payment_transaction.payment_type == PaymentType.PLAYER and payment_transaction.player_id:
        return f"TENIS-RESERVA-{reservation_id}-JUGADOR-{payment_transaction.player_id}-{payment_transaction.id}"
    return f"TENIS-RESERVA-{reservation_id}-PARCIAL-{payment_transaction.id}"


def _validate_payment_link_request(
    reservation: Reservation,
    base_amount: Decimal,
    payment_type: str,
    player: ReservationPlayer | None,
):
    if reservation.status == ReservationStatus.CANCELLED:
        raise serializers.ValidationError({"detail": "No se puede pagar una reserva cancelada."})
    if reservation.payment_status in (
        ReservationPaymentStatus.PAID,
        ReservationPaymentStatus.EXPIRED,
        ReservationPaymentStatus.CANCELLED,
        ReservationPaymentStatus.REJECTED,
    ):
        raise serializers.ValidationError(
            {"detail": f"No se puede crear un pago para una reserva {reservation.payment_status}."}
        )
    if base_amount <= 0:
        raise serializers.ValidationError({"amount": "El monto debe ser mayor a 0."})
    if base_amount > reservation.remaining_amount:
        raise serializers.ValidationError({"amount": "El monto no puede superar el saldo pendiente."})
    if payment_type == PaymentType.PLAYER:
        if not player:
            raise serializers.ValidationError({"player_id": "player_id es requerido para pagos por jugador."})
        if player.reservation_id != reservation.id:
            raise serializers.ValidationError({"player_id": "El jugador no pertenece a esta reserva."})


@transaction.atomic
def create_reservation_payment_link(
    reservation: Reservation,
    amount,
    payment_type: str,
    player_id: int | None = None,
) -> PaymentTransaction:
    locked_reservation = (
        Reservation.objects.select_for_update()
        .select_related("court")
        .get(id=reservation.id)
    )
    base_amount = quantize_money(amount)
    identification_decimal = get_identification_decimal()
    if identification_decimal != Decimal("0.19"):
        raise serializers.ValidationError(
            {"identification_decimal": "Para Tenis el decimal identificador debe ser 0.19."}
        )

    player = None
    if player_id is not None:
        player = ReservationPlayer.objects.filter(id=player_id, reservation=locked_reservation).first()

    _validate_payment_link_request(
        reservation=locked_reservation,
        base_amount=base_amount,
        payment_type=payment_type,
        player=player,
    )

    expires_at = get_payment_expiration_datetime()
    payment_transaction = PaymentTransaction.objects.create(
        reservation=locked_reservation,
        player=player,
        payment_type=payment_type,
        external_reference=f"TENIS-RESERVA-{locked_reservation.id}-PENDING-{uuid4().hex}",
        status=PaymentTransactionStatus.PENDING,
        base_amount=base_amount,
        identification_decimal=identification_decimal,
        mp_amount=base_amount + identification_decimal,
        expires_at=expires_at,
    )
    payment_transaction.external_reference = build_payment_external_reference(payment_transaction)
    payment_transaction.save(update_fields=("external_reference", "updated_at"))

    preference = mercadopago_service.create_checkout_preference_for_reservation_payment(
        payment_transaction
    )
    payment_transaction.preference_id = str(preference.get("id", ""))
    payment_transaction.payment_url = (
        preference.get("init_point") or preference.get("sandbox_init_point") or ""
    )
    payment_transaction.raw_response = preference
    payment_transaction.save(
        update_fields=("preference_id", "payment_url", "raw_response", "updated_at")
    )

    if expires_at and (
        not locked_reservation.payment_expires_at
        or locked_reservation.payment_expires_at < expires_at
    ):
        locked_reservation.payment_expires_at = expires_at
        locked_reservation.save(update_fields=("payment_expires_at", "updated_at"))

    logger.info(
        "Created Mercado Pago link for reservation=%s transaction=%s external_reference=%s",
        locked_reservation.id,
        payment_transaction.id,
        payment_transaction.external_reference,
    )
    return payment_transaction


def search_payable_reservations_by_participant_or_contact_name(query: str, limit: int = 10):
    tokens = [token.strip() for token in query.split() if token.strip()]
    if not tokens:
        return Reservation.objects.none()

    base_reservations = Reservation.objects.filter(
        start_datetime__gte=timezone.now(),
        status__in=(
            ReservationStatus.CONFIRMED,
            ReservationStatus.CANCELLATION_REQUESTED,
        ),
        payment_status__in=(
            ReservationPaymentStatus.PENDING_PAYMENT,
            ReservationPaymentStatus.PARTIAL_PAYMENT,
        ),
        paid_amount__lt=F("total_price"),
    )

    players = ReservationPlayer.objects.filter(reservation__in=base_reservations)
    contact_reservations = base_reservations
    for token in tokens:
        players = players.filter(Q(first_name__icontains=token) | Q(last_name__icontains=token))
        contact_reservations = contact_reservations.filter(contact_name__icontains=token)

    reservation_ids = set(players.values_list("reservation_id", flat=True).distinct()[:limit])
    reservation_ids.update(contact_reservations.values_list("id", flat=True).distinct()[:limit])
    return (
        Reservation.objects.select_related("court")
        .prefetch_related(
            "payment_transactions",
            "players",
        )
        .filter(id__in=reservation_ids)
        .order_by("start_datetime")
    )


def get_today_pending_payment_reservations(target_date: date | None = None, limit: int = 50):
    target_date = target_date or timezone.localdate()
    return (
        Reservation.objects.select_related("court")
        .prefetch_related(
            "payment_transactions",
            "players",
        )
        .filter(
            start_datetime__date=target_date,
            status__in=(
                ReservationStatus.CONFIRMED,
                ReservationStatus.CANCELLATION_REQUESTED,
            ),
            payment_status__in=(
                ReservationPaymentStatus.PENDING_PAYMENT,
                ReservationPaymentStatus.PARTIAL_PAYMENT,
            ),
            paid_amount__lt=F("total_price"),
        )
        .order_by("start_datetime", "court_id", "id")[:limit]
    )


def get_payment_report_transactions(start_date: date, end_date: date, status_filter: str = "approved"):
    start_datetime = combine_local_datetime(start_date, time.min)
    end_datetime = combine_local_datetime(end_date + timedelta(days=1), time.min)
    queryset = PaymentTransaction.objects.select_related(
        "reservation",
        "reservation__court",
        "player",
    ).filter(provider__in=(PaymentProvider.MERCADOPAGO, PaymentProvider.CASH))

    if status_filter == "all":
        queryset = queryset.filter(created_at__gte=start_datetime, created_at__lt=end_datetime)
    else:
        queryset = queryset.filter(
            status=PaymentTransactionStatus.APPROVED,
            paid_at__gte=start_datetime,
            paid_at__lt=end_datetime,
        )
    return queryset.order_by("paid_at", "created_at", "id")


def build_payment_report_rows(start_date: date, end_date: date, status_filter: str = "approved") -> list[dict]:
    transactions = get_payment_report_transactions(
        start_date=start_date,
        end_date=end_date,
        status_filter=status_filter,
    )
    rows = []
    for transaction in transactions:
        reservation = transaction.reservation
        player_name = ""
        if transaction.player:
            player_name = f"{transaction.player.first_name} {transaction.player.last_name}"
        paid_at = timezone.localtime(transaction.paid_at) if transaction.paid_at else None
        reservation_start = timezone.localtime(reservation.start_datetime)
        reservation_end = timezone.localtime(reservation.end_datetime)
        rows.append(
            {
                "fecha_pago": paid_at.date().isoformat() if paid_at else "",
                "hora_pago": paid_at.time().strftime("%H:%M:%S") if paid_at else "",
                "reserva_id": reservation.id,
                "cancha": reservation.court.name,
                "turno_fecha": reservation_start.date().isoformat(),
                "turno_inicio": reservation_start.time().strftime("%H:%M:%S"),
                "turno_fin": reservation_end.time().strftime("%H:%M:%S"),
                "jugador": player_name,
                "metodo_pago": transaction.provider,
                "tipo_pago": transaction.payment_type,
                "estado": transaction.status,
                "detalle_estado": transaction.status_detail,
                "monto_reserva": transaction.base_amount,
                "decimal_identificador": transaction.identification_decimal,
                "monto_cobrado_mp": transaction.mp_amount,
                "monto_informado_mp": transaction.amount_received or "",
                "nro_operacion_mp": transaction.payment_id or "",
                "preference_id": transaction.preference_id,
                "external_reference": transaction.external_reference,
                "payer_email": transaction.payer_email,
            }
        )
    return rows


def get_mercadopago_report_transactions(start_date: date, end_date: date, status_filter: str = "approved"):
    return get_payment_report_transactions(start_date, end_date, status_filter)


def build_mercadopago_report_rows(start_date: date, end_date: date, status_filter: str = "approved") -> list[dict]:
    return build_payment_report_rows(start_date, end_date, status_filter)


def extract_mercadopago_payment_id(request) -> str | None:
    for key in ("id", "data.id", "payment_id"):
        value = request.query_params.get(key)
        if value:
            return str(value)
    if request.query_params.get("topic") == "payment" and request.query_params.get("id"):
        return str(request.query_params["id"])

    data = request.data if isinstance(request.data, dict) else {}
    nested_data = data.get("data") if isinstance(data.get("data"), dict) else {}
    for value in (
        nested_data.get("id"),
        data.get("id"),
        data.get("payment_id"),
    ):
        if value:
            return str(value)
    return None


def parse_reservation_id_from_external_reference(external_reference: str) -> int | None:
    match = PAYMENT_REFERENCE_RE.match(external_reference or "")
    if not match:
        return None
    return int(match.group("reservation_id"))


def recalculate_reservation_payment_state(reservation: Reservation, now=None) -> Reservation:
    now = now or timezone.now()
    paid_amount = (
        PaymentTransaction.objects.filter(
            reservation=reservation,
            status=PaymentTransactionStatus.APPROVED,
        ).aggregate(total=Sum("base_amount"))["total"]
        or Decimal("0.00")
    )
    reservation.paid_amount = paid_amount
    if paid_amount >= reservation.total_price:
        reservation.payment_status = ReservationPaymentStatus.PAID
        reservation.is_paid = True
        reservation.paid_at = reservation.paid_at or now
    elif paid_amount > 0:
        reservation.payment_status = ReservationPaymentStatus.PARTIAL_PAYMENT
        reservation.is_paid = False
        reservation.paid_at = None
    else:
        has_pending = reservation.payment_transactions.filter(
            status__in=(PaymentTransactionStatus.PENDING, PaymentTransactionStatus.IN_PROCESS)
        ).exists()
        reservation.payment_status = (
            ReservationPaymentStatus.PENDING_PAYMENT
            if has_pending
            else ReservationPaymentStatus.REJECTED
        )
        reservation.is_paid = False
        reservation.paid_at = None
    reservation.save(
        update_fields=("paid_amount", "payment_status", "is_paid", "paid_at", "updated_at")
    )
    return reservation


@transaction.atomic
def apply_mercadopago_payment(payment_data: dict) -> PaymentTransaction | None:
    external_reference = payment_data.get("external_reference") or ""
    payment_transaction = (
        PaymentTransaction.objects.select_for_update()
        .select_related("reservation")
        .filter(external_reference=external_reference)
        .first()
    )
    if not payment_transaction:
        reservation_id = parse_reservation_id_from_external_reference(external_reference)
        logger.warning(
            "Mercado Pago webhook without matching transaction. external_reference=%s reservation_id=%s",
            external_reference,
            reservation_id,
        )
        return None

    status_value = payment_data.get("status") or PaymentTransactionStatus.PENDING
    payment_id = payment_data.get("id")
    amount_received = quantize_money(payment_data.get("transaction_amount") or "0")
    payer = payment_data.get("payer") if isinstance(payment_data.get("payer"), dict) else {}
    payer_email = payer.get("email", "")
    now = timezone.now()

    payment_transaction.payment_id = str(payment_id) if payment_id else payment_transaction.payment_id
    payment_transaction.status_detail = payment_data.get("status_detail") or ""
    payment_transaction.amount_received = amount_received
    payment_transaction.payer_email = payer_email or ""
    payment_transaction.raw_response = payment_data

    if status_value == PaymentTransactionStatus.APPROVED:
        if amount_received != payment_transaction.mp_amount:
            payment_transaction.status = PaymentTransactionStatus.AMOUNT_MISMATCH
            payment_transaction.save(
                update_fields=(
                    "payment_id",
                    "status",
                    "status_detail",
                    "amount_received",
                    "payer_email",
                    "raw_response",
                    "updated_at",
                )
            )
            logger.warning(
                "Mercado Pago amount mismatch transaction=%s received=%s expected=%s",
                payment_transaction.id,
                amount_received,
                payment_transaction.mp_amount,
            )
            return payment_transaction

        payment_transaction.status = PaymentTransactionStatus.APPROVED
        payment_transaction.paid_at = payment_transaction.paid_at or now
        payment_transaction.save(
            update_fields=(
                "payment_id",
                "status",
                "status_detail",
                "amount_received",
                "payer_email",
                "raw_response",
                "paid_at",
                "updated_at",
            )
        )
        recalculate_reservation_payment_state(payment_transaction.reservation, now=now)
        logger.info(
            "Approved Mercado Pago payment transaction=%s reservation=%s",
            payment_transaction.id,
            payment_transaction.reservation_id,
        )
        return payment_transaction

    if status_value in (
        PaymentTransactionStatus.REJECTED,
        PaymentTransactionStatus.CANCELLED,
        PaymentTransactionStatus.REFUNDED,
    ):
        payment_transaction.status = status_value
    elif status_value == PaymentTransactionStatus.IN_PROCESS:
        payment_transaction.status = PaymentTransactionStatus.IN_PROCESS
    else:
        payment_transaction.status = PaymentTransactionStatus.PENDING

    payment_transaction.save(
        update_fields=(
            "payment_id",
            "status",
            "status_detail",
            "amount_received",
            "payer_email",
            "raw_response",
            "updated_at",
        )
    )
    recalculate_reservation_payment_state(payment_transaction.reservation, now=now)
    logger.info(
        "Updated Mercado Pago payment transaction=%s status=%s",
        payment_transaction.id,
        payment_transaction.status,
    )
    return payment_transaction


def process_mercadopago_webhook_payment(payment_id: str) -> PaymentTransaction | None:
    payment_data = mercadopago_service.get_payment(payment_id)
    return apply_mercadopago_payment(payment_data)


@transaction.atomic
def expire_pending_reservations(now=None) -> dict:
    return {
        "expired_without_payment": 0,
        "marked_for_review": 0,
    }


@transaction.atomic
def resolve_cancellation_request(
    cancellation_request: CancellationRequest,
    resolution_status: str,
    resolved_by,
    cancellation_reason: str = "",
) -> CancellationRequest:
    locked_request = CancellationRequest.objects.select_for_update().select_related("reservation").get(
        id=cancellation_request.id
    )
    if locked_request.status != CancellationRequestStatus.PENDING:
        raise serializers.ValidationError(
            {"detail": "La solicitud ya fue resuelta previamente."}
        )
    if resolution_status not in (
        CancellationRequestStatus.APPROVED,
        CancellationRequestStatus.REJECTED,
    ):
        raise serializers.ValidationError(
            {"status": "status must be APPROVED or REJECTED."}
        )

    if resolution_status == CancellationRequestStatus.APPROVED:
        reason_to_apply = cancellation_reason or locked_request.reason
        if locked_request.reservation.status != ReservationStatus.CANCELLED:
            cancel_reservation_by_admin(
                reservation=locked_request.reservation,
                cancelled_by=resolved_by,
                cancellation_reason=reason_to_apply,
            )
    elif resolution_status == CancellationRequestStatus.REJECTED:
        reservation = locked_request.reservation
        if reservation.status == ReservationStatus.CANCELLATION_REQUESTED:
            reservation.status = ReservationStatus.CONFIRMED
            reservation.save(update_fields=("status", "updated_at"))

    locked_request.status = resolution_status
    locked_request.resolved_at = timezone.now()
    locked_request.resolved_by = resolved_by
    locked_request.save(update_fields=("status", "resolved_at", "resolved_by", "updated_at"))
    return locked_request


@transaction.atomic
def deactivate_recurring_rule(
    recurring_rule: RecurringReservationRule,
    deactivated_by=None,
    cancellation_reason: str = "Regla recurrente desactivada por admin.",
) -> tuple[RecurringReservationRule, int]:
    locked_rule = RecurringReservationRule.objects.select_for_update().get(id=recurring_rule.id)
    if locked_rule.active:
        locked_rule.active = False
        locked_rule.save(update_fields=("active", "updated_at"))

    now = timezone.now()
    cancelled_count = Reservation.objects.filter(
        recurring_rule=locked_rule,
        reservation_type=ReservationType.CLASS,
        start_datetime__gte=now,
    ).exclude(status=ReservationStatus.CANCELLED).update(
        status=ReservationStatus.CANCELLED,
        cancelled_at=now,
        cancelled_by=deactivated_by if getattr(deactivated_by, "is_authenticated", False) else None,
        cancellation_reason=cancellation_reason,
        updated_at=now,
    )
    return locked_rule, cancelled_count


def _generate_reservations_for_rule(
    rule: RecurringReservationRule,
    today: date,
    limit_date: date,
) -> int:
    generated_count = 0
    if not rule.court.active:
        return 0

    generation_start = max(today, rule.start_date)
    generation_end = min(rule.end_date, limit_date) if rule.end_date else limit_date
    if generation_start > generation_end:
        return 0

    current_date = generation_start
    while current_date <= generation_end:
        weekday = DAY_OF_WEEK_BY_INDEX[current_date.weekday()]
        if weekday not in rule.days_of_week:
            current_date += timedelta(days=1)
            continue

        start_datetime = combine_local_datetime(current_date, rule.start_time)
        end_datetime = start_datetime + timedelta(minutes=CLASS_RESERVATION_MINUTES)
        schedule = get_schedule_for_date(current_date)
        if schedule:
            open_time, close_time = schedule
            open_datetime = combine_local_datetime(current_date, open_time)
            close_datetime = combine_local_datetime(current_date, close_time)
            if start_datetime >= open_datetime and end_datetime <= close_datetime:
                already_exists = Reservation.objects.filter(
                    reservation_type=ReservationType.CLASS,
                    recurring_rule=rule,
                    court=rule.court,
                    start_datetime=start_datetime,
                ).exists()
                if not already_exists and not check_overlap(
                    court=rule.court, start_datetime=start_datetime, end_datetime=end_datetime
                ):
                    Reservation.objects.create(
                        court=rule.court,
                        reservation_type=ReservationType.CLASS,
                        game_mode=None,
                        title=rule.title,
                        contact_name="Admin",
                        contact_phone="N/A",
                        start_datetime=start_datetime,
                        end_datetime=end_datetime,
                        status=ReservationStatus.CONFIRMED,
                        total_price=Decimal("0.00"),
                        notes=rule.notes,
                        created_by=rule.created_by,
                        recurring_rule=rule,
                    )
                    generated_count += 1
        current_date += timedelta(days=1)
    return generated_count


@transaction.atomic
def _sync_single_recurring_rule_generation(
    recurring_rule_id: int,
    days_ahead: int,
    regenerate_future_classes: bool,
) -> int:
    locked_rule = (
        RecurringReservationRule.objects.select_for_update()
        .select_related("court")
        .get(id=recurring_rule_id)
    )

    if regenerate_future_classes:
        now = timezone.now()
        Reservation.objects.filter(
            recurring_rule=locked_rule,
            reservation_type=ReservationType.CLASS,
            start_datetime__gte=now,
        ).exclude(status=ReservationStatus.CANCELLED).update(
            status=ReservationStatus.CANCELLED,
            cancelled_at=now,
            cancellation_reason="Regla recurrente actualizada; clases futuras regeneradas.",
            updated_at=now,
        )

    if not locked_rule.active or not locked_rule.court.active:
        return 0

    today = timezone.localdate()
    limit_date = today + timedelta(days=days_ahead)
    return _generate_reservations_for_rule(rule=locked_rule, today=today, limit_date=limit_date)


def ensure_recurring_rule_generation(
    recurring_rule_id: int,
    days_ahead: int = 90,
    regenerate_future_classes: bool = False,
) -> int:
    attempts = max(1, RECURRING_GENERATION_RETRY_ATTEMPTS)
    last_error = None
    for _ in range(attempts):
        try:
            return _sync_single_recurring_rule_generation(
                recurring_rule_id=recurring_rule_id,
                days_ahead=days_ahead,
                regenerate_future_classes=regenerate_future_classes,
            )
        except OperationalError as exc:
            last_error = exc
    if last_error:
        raise last_error
    return 0


@transaction.atomic
def generate_recurring_reservations(days_ahead: int = 90) -> int:
    today = timezone.localdate()
    limit_date = today + timedelta(days=days_ahead)
    generated_count = 0
    rules = (
        RecurringReservationRule.objects.select_for_update()
        .select_related("court")
        .filter(active=True)
    )
    for rule in rules:
        generated_count += _generate_reservations_for_rule(
            rule=rule,
            today=today,
            limit_date=limit_date,
        )
    return generated_count
