from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from .models import (
    BlockedSlot,
    CancellationRequest,
    CancellationRequestStatus,
    Court,
    DayOfWeek,
    GameMode,
    NotificationChannel,
    NotificationLog,
    NotificationStatus,
    PlayerType,
    PriceRule,
    RecurringReservationRule,
    Reservation,
    ReservationPlayer,
    ReservationStatus,
    ReservationType,
    SpecialSchedule,
    ClubSchedule,
)

NORMAL_RESERVATION_MINUTES = 90
CLASS_RESERVATION_MINUTES = 60

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
    reservation_qs = Reservation.objects.filter(court=court).exclude(status=ReservationStatus.CANCELLED)
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
    if start_datetime.date() == now.date() and start_datetime <= now:
        raise serializers.ValidationError(
            {"start_time": "Si la reserva es para hoy, no puede estar ya iniciada."}
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
        notes=data.get("notes", ""),
        created_by=created_by if getattr(created_by, "is_authenticated", False) else None,
    )
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

        reservations = Reservation.objects.filter(
            court=court,
            start_datetime__lt=closing_datetime,
            end_datetime__gt=opening_datetime,
        ).exclude(status=ReservationStatus.CANCELLED).values(
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
    reservation.cancelled_at = timezone.now()
    reservation.cancelled_by = cancelled_by
    reservation.cancellation_reason = cancellation_reason
    reservation.save(
        update_fields=("status", "cancelled_at", "cancelled_by", "cancellation_reason", "updated_at")
    )
    return reservation


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
        if not rule.court.active:
            continue
        generation_start = max(today, rule.start_date)
        generation_end = min(rule.end_date, limit_date) if rule.end_date else limit_date
        if generation_start > generation_end:
            continue

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
