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
    NotificationDevice,
    NotificationProvider,
    PaymentTransaction,
    PaymentTransactionStatus,
    PaymentType,
    PriceRule,
    RecurringReservationRule,
    Reservation,
    ReservationPaymentStatus,
    ReservationPlayer,
    ReservationStatus,
    ReservationType,
    SpecialSchedule,
    GameMode,
)
from .services import (
    CLASS_RESERVATION_MINUTES,
    create_cancellation_request,
    create_reservation_payment_link,
    get_blocking_reservation_queryset,
    create_reservation,
    get_schedule_for_date,
    register_cash_payment,
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
        fields = ("id", "first_name", "last_name", "is_member", "price_applied")


class PaymentTransactionSerializer(serializers.ModelSerializer):
    player_name = serializers.SerializerMethodField()

    class Meta:
        model = PaymentTransaction
        fields = (
            "id",
            "player",
            "player_name",
            "provider",
            "payment_type",
            "preference_id",
            "payment_id",
            "external_reference",
            "status",
            "status_detail",
            "base_amount",
            "identification_decimal",
            "mp_amount",
            "amount_received",
            "payer_email",
            "payment_url",
            "expires_at",
            "paid_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_player_name(self, obj) -> str | None:
        if not obj.player:
            return None
        return f"{obj.player.first_name} {obj.player.last_name}"


class NotificationDeviceSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    provider = serializers.ChoiceField(choices=NotificationProvider.choices, required=False)

    class Meta:
        model = NotificationDevice
        fields = (
            "id",
            "user_id",
            "platform",
            "provider",
            "token",
            "device_id",
            "enabled",
            "last_seen",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "user_id", "enabled", "last_seen", "created_at", "updated_at")
        extra_kwargs = {"token": {"validators": []}}

    def validate_token(self, value):
        if not value.strip():
            raise serializers.ValidationError("token no puede estar vacio.")
        return value.strip()

    def create(self, validated_data):
        request = self.context["request"]
        now = timezone.now()
        device, _ = NotificationDevice.objects.update_or_create(
            token=validated_data["token"],
            defaults={
                "user": request.user,
                "platform": validated_data["platform"],
                "provider": validated_data.get("provider", NotificationProvider.FCM),
                "device_id": validated_data.get("device_id", ""),
                "enabled": True,
                "last_seen": now,
            },
        )
        return device


class NotificationDeviceUnregisterSerializer(serializers.Serializer):
    token = serializers.CharField(required=False, allow_blank=True)
    device_id = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        token = attrs.get("token", "").strip()
        device_id = attrs.get("device_id", "").strip()
        if not token and not device_id:
            raise serializers.ValidationError({"detail": "token o device_id es requerido."})
        attrs["token"] = token
        attrs["device_id"] = device_id
        return attrs


class ReservationPlayerInputSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    is_member = serializers.BooleanField()


class ReservationSerializer(serializers.ModelSerializer):
    players = ReservationPlayerSerializer(many=True, read_only=True)
    payment_transactions = PaymentTransactionSerializer(many=True, read_only=True)
    court_name = serializers.CharField(source="court.name", read_only=True)
    paid_confirmed_by_username = serializers.CharField(source="paid_confirmed_by.username", read_only=True)
    total_amount = serializers.DecimalField(max_digits=10, decimal_places=2, source="total_price", read_only=True)
    remaining_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

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
            "total_amount",
            "paid_amount",
            "remaining_amount",
            "payment_status",
            "payment_expires_at",
            "requires_admin_review",
            "mp_external_reference_base",
            "is_paid",
            "paid_at",
            "paid_confirmed_by",
            "paid_confirmed_by_username",
            "notes",
            "players",
            "payment_transactions",
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


class ReservationPaymentStatusSerializer(serializers.Serializer):
    is_paid = serializers.BooleanField()


class CashPaymentCreateSerializer(serializers.Serializer):
    confirmation_password = serializers.CharField(write_only=True, trim_whitespace=False)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    payment_type = serializers.ChoiceField(choices=PaymentType.choices, required=False)
    player_id = serializers.IntegerField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        payment_type = attrs.get("payment_type")
        if payment_type == PaymentType.PLAYER and not attrs.get("player_id"):
            raise serializers.ValidationError({"player_id": "player_id es requerido para pagos por jugador."})
        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        reservation: Reservation = self.context["reservation"]
        return register_cash_payment(
            reservation=reservation,
            confirmation_password=validated_data["confirmation_password"],
            confirmed_by=request.user if request else None,
            amount=validated_data.get("amount"),
            payment_type=validated_data.get("payment_type"),
            player_id=validated_data.get("player_id"),
            notes=validated_data.get("notes", ""),
        )


class ReservationPaymentLinkCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    payment_type = serializers.ChoiceField(choices=PaymentType.choices)
    player_id = serializers.IntegerField(required=False)

    def validate(self, attrs):
        payment_type = attrs["payment_type"]
        if payment_type == PaymentType.PLAYER and not attrs.get("player_id"):
            raise serializers.ValidationError({"player_id": "player_id es requerido para pagos por jugador."})
        return attrs

    def create(self, validated_data):
        reservation: Reservation = self.context["reservation"]
        return create_reservation_payment_link(
            reservation=reservation,
            amount=validated_data["amount"],
            payment_type=validated_data["payment_type"],
            player_id=validated_data.get("player_id"),
        )


class ReservationPaymentLinkResponseSerializer(serializers.Serializer):
    reservation_id = serializers.IntegerField()
    payment_transaction_id = serializers.IntegerField()
    payment_url = serializers.URLField(allow_blank=True)
    preference_id = serializers.CharField(allow_blank=True)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    mp_amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    identification_decimal = serializers.DecimalField(max_digits=4, decimal_places=2)
    reservation_total_amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    reservation_paid_amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    reservation_remaining_amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    expires_at = serializers.DateTimeField(allow_null=True)


class ReservationPaymentStatusDetailSerializer(serializers.ModelSerializer):
    players = ReservationPlayerSerializer(many=True, read_only=True)
    payment_transactions = PaymentTransactionSerializer(many=True, read_only=True)
    court_name = serializers.CharField(source="court.name", read_only=True)
    total_amount = serializers.DecimalField(max_digits=10, decimal_places=2, source="total_price", read_only=True)
    remaining_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = Reservation
        fields = (
            "id",
            "court",
            "court_name",
            "contact_name",
            "start_datetime",
            "end_datetime",
            "status",
            "total_amount",
            "paid_amount",
            "remaining_amount",
            "payment_status",
            "payment_expires_at",
            "requires_admin_review",
            "is_paid",
            "paid_at",
            "players",
            "payment_transactions",
        )


class PlayerReservationPaymentSearchSerializer(serializers.Serializer):
    q = serializers.CharField(min_length=3, max_length=100)


class MercadoPagoReportQuerySerializer(serializers.Serializer):
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    status = serializers.ChoiceField(
        choices=(("approved", "Approved"), ("all", "All")),
        required=False,
        default=PaymentTransactionStatus.APPROVED,
    )

    def validate(self, attrs):
        if attrs["end_date"] < attrs["start_date"]:
            raise serializers.ValidationError({"end_date": "end_date must be greater than or equal to start_date."})
        return attrs


class ReservationPaymentSearchResultSerializer(serializers.ModelSerializer):
    players = ReservationPlayerSerializer(many=True, read_only=True)
    matching_players = serializers.SerializerMethodField()
    payment_transactions = PaymentTransactionSerializer(many=True, read_only=True)
    court_name = serializers.CharField(source="court.name", read_only=True)
    total_amount = serializers.DecimalField(max_digits=10, decimal_places=2, source="total_price", read_only=True)
    remaining_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = Reservation
        fields = (
            "id",
            "court",
            "court_name",
            "contact_name",
            "start_datetime",
            "end_datetime",
            "status",
            "total_amount",
            "paid_amount",
            "remaining_amount",
            "payment_status",
            "payment_expires_at",
            "requires_admin_review",
            "players",
            "matching_players",
            "payment_transactions",
        )

    def get_matching_players(self, obj):
        tokens = self.context.get("search_tokens", [])
        matching_players = []
        for player in obj.players.all():
            first_name = player.first_name.lower()
            last_name = player.last_name.lower()
            if all(token in first_name or token in last_name for token in tokens):
                matching_players.append(player)
        return ReservationPlayerSerializer(matching_players, many=True).data


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
        if court and get_blocking_reservation_queryset().filter(
            court=court,
            start_datetime__lt=end_datetime,
            end_datetime__gt=start_datetime,
        ).exists():
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
