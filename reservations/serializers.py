from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import (
    BlockedSlot,
    CancellationRequest,
    CancellationRequestStatus,
    ClubSchedule,
    Court,
    PriceRule,
    RecurringReservationRule,
    Reservation,
    ReservationPlayer,
    ReservationStatus,
    ReservationType,
    SpecialSchedule,
    GameMode,
)
from .services import (
    CLASS_RESERVATION_MINUTES,
    create_cancellation_request,
    create_reservation,
    get_schedule_for_date,
    resolve_cancellation_request,
)


class CourtSerializer(serializers.ModelSerializer):
    class Meta:
        model = Court
        fields = ("id", "name", "active", "created_at", "updated_at")


class ClubScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClubSchedule
        fields = ("id", "day_of_week", "open_time", "close_time", "active")


class SpecialScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = SpecialSchedule
        fields = ("id", "date", "open_time", "close_time", "closed", "reason")


class PriceRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = PriceRule
        fields = (
            "id",
            "game_mode",
            "player_type",
            "price",
            "active",
            "valid_from",
            "valid_to",
            "created_at",
            "updated_at",
        )


class ReservationPlayerSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReservationPlayer
        fields = ("first_name", "last_name", "is_member", "price_applied")


class ReservationPlayerInputSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    is_member = serializers.BooleanField()


class ReservationSerializer(serializers.ModelSerializer):
    players = ReservationPlayerSerializer(many=True, read_only=True)
    court_name = serializers.CharField(source="court.name", read_only=True)

    class Meta:
        model = Reservation
        fields = (
            "id",
            "court",
            "court_name",
            "reservation_type",
            "game_mode",
            "title",
            "contact_name",
            "contact_phone",
            "start_datetime",
            "end_datetime",
            "status",
            "total_price",
            "notes",
            "players",
            "recurring_rule",
            "created_at",
            "updated_at",
        )


class ReservationCreateSerializer(serializers.Serializer):
    court = serializers.IntegerField()
    date = serializers.DateField()
    start_time = serializers.TimeField()
    game_mode = serializers.ChoiceField(choices=GameMode.choices)
    contact_name = serializers.CharField(max_length=150)
    contact_phone = serializers.CharField(max_length=50)
    players = ReservationPlayerInputSerializer(many=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    def create(self, validated_data):
        request = self.context.get("request")
        created_by = request.user if request else None
        return create_reservation(data=validated_data, created_by=created_by)

    def to_representation(self, instance):
        return ReservationSerializer(instance).data


class CancelReservationSerializer(serializers.Serializer):
    cancellation_reason = serializers.CharField(required=False, allow_blank=True)


class CancellationRequestCreateSerializer(serializers.Serializer):
    requester_name = serializers.CharField(max_length=150)
    requester_phone = serializers.CharField(max_length=50)
    reason = serializers.CharField()

    def create(self, validated_data):
        reservation: Reservation = self.context["reservation"]
        return create_cancellation_request(reservation=reservation, data=validated_data)


class CancellationRequestSerializer(serializers.ModelSerializer):
    reservation_id = serializers.IntegerField(source="reservation.id", read_only=True)
    reservation_status = serializers.CharField(source="reservation.status", read_only=True)
    resolved_by_username = serializers.CharField(source="resolved_by.username", read_only=True)

    class Meta:
        model = CancellationRequest
        fields = (
            "id",
            "reservation_id",
            "reservation_status",
            "requester_name",
            "requester_phone",
            "reason",
            "status",
            "created_at",
            "updated_at",
            "resolved_at",
            "resolved_by",
            "resolved_by_username",
        )
        read_only_fields = (
            "id",
            "reservation_id",
            "reservation_status",
            "created_at",
            "updated_at",
            "resolved_at",
            "resolved_by",
            "resolved_by_username",
        )


class CancellationRequestResolveSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=(
            CancellationRequestStatus.APPROVED,
            CancellationRequestStatus.REJECTED,
        )
    )
    cancellation_reason = serializers.CharField(required=False, allow_blank=True)

    def update(self, instance, validated_data):
        request = self.context["request"]
        return resolve_cancellation_request(
            cancellation_request=instance,
            resolution_status=validated_data["status"],
            resolved_by=request.user,
            cancellation_reason=validated_data.get("cancellation_reason", ""),
        )

    def create(self, validated_data):
        raise NotImplementedError("Use update() for resolving cancellation requests.")


class RecurringReservationRuleSerializer(serializers.ModelSerializer):
    court_name = serializers.CharField(source="court.name", read_only=True)
    end_time = serializers.SerializerMethodField(read_only=True)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = RecurringReservationRule
        fields = (
            "id",
            "court",
            "court_name",
            "title",
            "days_of_week",
            "start_time",
            "end_time",
            "start_date",
            "end_date",
            "active",
            "notes",
            "created_at",
            "updated_at",
        )

    @extend_schema_field(serializers.TimeField())
    def get_end_time(self, obj) -> time:
        base_dt = datetime.combine(timezone.localdate(), obj.start_time)
        return (base_dt + timedelta(minutes=CLASS_RESERVATION_MINUTES)).time()

    def validate(self, attrs):
        target_court = attrs.get("court") or getattr(self.instance, "court", None)
        start_time = attrs.get("start_time") or getattr(self.instance, "start_time", None)
        start_date = attrs.get("start_date") or getattr(self.instance, "start_date", None)
        if target_court and not target_court.active:
            raise serializers.ValidationError({"court": "La cancha debe estar activa para reglas recurrentes."})
        if start_time and start_date:
            schedule = get_schedule_for_date(start_date)
            if not schedule:
                raise serializers.ValidationError({"detail": "No hay horario del club para start_date."})
            open_time, close_time = schedule
            end_time = (
                datetime.combine(timezone.localdate(), start_time)
                + timedelta(minutes=CLASS_RESERVATION_MINUTES)
            ).time()
            if start_time < open_time or end_time > close_time:
                raise serializers.ValidationError(
                    {"detail": "La clase recurrente esta fuera del horario del club."}
                )
        return attrs

    def create(self, validated_data):
        if validated_data.get("notes") is None:
            validated_data["notes"] = ""
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if "notes" in validated_data and validated_data["notes"] is None:
            validated_data["notes"] = ""
        return super().update(instance, validated_data)


class RecurringRuleDeactivateSerializer(serializers.Serializer):
    cancellation_reason = serializers.CharField(required=False, allow_blank=True)


class RecurringRuleDeactivateResponseSerializer(serializers.Serializer):
    rule = RecurringReservationRuleSerializer()
    cancelled_future_classes = serializers.IntegerField()


class BlockedSlotSerializer(serializers.ModelSerializer):
    court_name = serializers.CharField(source="court.name", read_only=True)

    class Meta:
        model = BlockedSlot
        fields = (
            "id",
            "court",
            "court_name",
            "start_datetime",
            "end_datetime",
            "block_type",
            "reason",
            "created_by",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("created_by",)

    def validate(self, attrs):
        court = attrs.get("court") or getattr(self.instance, "court", None)
        start_datetime = attrs.get("start_datetime") or getattr(self.instance, "start_datetime", None)
        end_datetime = attrs.get("end_datetime") or getattr(self.instance, "end_datetime", None)
        if start_datetime >= end_datetime:
            raise serializers.ValidationError({"end_datetime": "end_datetime must be later than start_datetime."})
        if court and Reservation.objects.filter(
            court=court,
            start_datetime__lt=end_datetime,
            end_datetime__gt=start_datetime,
        ).exclude(status=ReservationStatus.CANCELLED).exists():
            raise serializers.ValidationError({"detail": "No se puede bloquear: hay reservas activas en ese rango."})
        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        if request and getattr(request.user, "is_authenticated", False):
            validated_data["created_by"] = request.user
        return super().create(validated_data)


class AvailabilityRangeSerializer(serializers.Serializer):
    start_time = serializers.TimeField()
    end_time = serializers.TimeField()
    duration_minutes = serializers.IntegerField()
    can_book_90_min = serializers.BooleanField()
    can_start_until = serializers.TimeField(allow_null=True)


class UnavailableRangeSerializer(serializers.Serializer):
    start_time = serializers.TimeField()
    end_time = serializers.TimeField()
    reason = serializers.CharField()
    reservation_type = serializers.CharField(allow_null=True)
    reservation_contact_name = serializers.CharField(allow_null=True)
    class_title = serializers.CharField(allow_null=True)
    block_reason = serializers.CharField(allow_null=True)


class AvailabilityCourtSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    available_ranges = AvailabilityRangeSerializer(many=True)
    unavailable_ranges = UnavailableRangeSerializer(many=True)


class AvailabilityResponseSerializer(serializers.Serializer):
    date = serializers.DateField()
    reservation_duration_minutes = serializers.IntegerField()
    courts = AvailabilityCourtSerializer(many=True)


class GenerateRecurringReservationsResponseSerializer(serializers.Serializer):
    created = serializers.IntegerField()
    days_ahead = serializers.IntegerField()


class AuthUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = get_user_model()
        fields = (
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "is_active",
            "is_staff",
            "is_superuser",
        )
