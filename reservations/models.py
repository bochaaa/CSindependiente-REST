from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class DayOfWeek(models.TextChoices):
    MONDAY = "MONDAY", "Monday"
    TUESDAY = "TUESDAY", "Tuesday"
    WEDNESDAY = "WEDNESDAY", "Wednesday"
    THURSDAY = "THURSDAY", "Thursday"
    FRIDAY = "FRIDAY", "Friday"
    SATURDAY = "SATURDAY", "Saturday"
    SUNDAY = "SUNDAY", "Sunday"


class ReservationType(models.TextChoices):
    NORMAL = "NORMAL", "Normal"
    CLASS = "CLASS", "Class"


class ReservationStatus(models.TextChoices):
    CONFIRMED = "CONFIRMED", "Confirmed"
    CANCELLED = "CANCELLED", "Cancelled"
    CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED", "Cancellation requested"


class ReservationPaymentStatus(models.TextChoices):
    PENDING_PAYMENT = "pending_payment", "Pending payment"
    PARTIAL_PAYMENT = "partial_payment", "Partial payment"
    PAID = "paid", "Paid"
    EXPIRED = "expired", "Expired"
    CANCELLED = "cancelled", "Cancelled"
    REJECTED = "rejected", "Rejected"


class GameMode(models.TextChoices):
    SINGLES = "SINGLES", "Singles"
    DOUBLES = "DOUBLES", "Doubles"


class PlayerType(models.TextChoices):
    MEMBER = "MEMBER", "Member"
    NON_MEMBER = "NON_MEMBER", "Non member"


class BlockType(models.TextChoices):
    TOURNAMENT = "TOURNAMENT", "Tournament"
    MAINTENANCE = "MAINTENANCE", "Maintenance"
    OTHER = "OTHER", "Other"


class CancellationRequestStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class NotificationChannel(models.TextChoices):
    WHATSAPP = "WHATSAPP", "WhatsApp"
    EMAIL = "EMAIL", "Email"
    PUSH = "PUSH", "Push"


class NotificationStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"


class NotificationDevicePlatform(models.TextChoices):
    WEB = "web", "Web"
    ANDROID = "android", "Android"


class NotificationProvider(models.TextChoices):
    FCM = "fcm", "Firebase Cloud Messaging"


class PaymentProvider(models.TextChoices):
    MERCADOPAGO = "mercadopago", "Mercado Pago"
    CASH = "cash", "Cash"


class PaymentType(models.TextChoices):
    TOTAL = "total", "Total"
    PLAYER = "player", "Player"
    PARTIAL = "partial", "Partial"


class PaymentTransactionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    IN_PROCESS = "in_process", "In process"
    REJECTED = "rejected", "Rejected"
    CANCELLED = "cancelled", "Cancelled"
    REFUNDED = "refunded", "Refunded"
    AMOUNT_MISMATCH = "amount_mismatch", "Amount mismatch"


class Court(TimestampedModel):
    name = models.CharField(max_length=100, unique=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name


class ClubSchedule(models.Model):
    day_of_week = models.CharField(
        max_length=16,
        choices=DayOfWeek.choices,
        unique=True,
    )
    open_time = models.TimeField()
    close_time = models.TimeField()
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ("day_of_week",)

    def clean(self):
        if self.open_time >= self.close_time:
            raise ValidationError({"close_time": "close_time must be later than open_time."})

    def __str__(self) -> str:
        return f"{self.day_of_week}: {self.open_time} - {self.close_time}"


class SpecialSchedule(models.Model):
    date = models.DateField(unique=True)
    open_time = models.TimeField(null=True, blank=True)
    close_time = models.TimeField(null=True, blank=True)
    closed = models.BooleanField(default=False)
    reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("date",)

    def clean(self):
        if self.closed:
            return
        if self.open_time is None or self.close_time is None:
            raise ValidationError("open_time and close_time are required when closed is false.")
        if self.open_time >= self.close_time:
            raise ValidationError({"close_time": "close_time must be later than open_time."})

    def __str__(self) -> str:
        if self.closed:
            return f"{self.date} (closed)"
        return f"{self.date}: {self.open_time} - {self.close_time}"


class PriceRule(TimestampedModel):
    game_mode = models.CharField(max_length=10, choices=GameMode.choices)
    player_type = models.CharField(max_length=12, choices=PlayerType.choices)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    active = models.BooleanField(default=True)
    valid_from = models.DateField(default=timezone.localdate)
    valid_to = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ("-valid_from", "game_mode", "player_type")
        indexes = [
            models.Index(fields=("game_mode", "player_type", "active")),
            models.Index(fields=("valid_from", "valid_to")),
        ]

    def clean(self):
        if self.valid_to and self.valid_to < self.valid_from:
            raise ValidationError({"valid_to": "valid_to must be >= valid_from."})

    def __str__(self) -> str:
        return f"{self.game_mode} - {self.player_type}: {self.price}"


class RecurringReservationRule(TimestampedModel):
    court = models.ForeignKey(Court, on_delete=models.PROTECT, related_name="recurring_rules")
    title = models.CharField(max_length=150)
    days_of_week = models.JSONField(default=list)
    start_time = models.TimeField()
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_recurring_rules",
    )

    class Meta:
        ordering = ("court_id", "start_time", "title")

    def clean(self):
        valid_days = {choice for choice, _ in DayOfWeek.choices}
        if not isinstance(self.days_of_week, list) or not self.days_of_week:
            raise ValidationError({"days_of_week": "days_of_week must be a non-empty list."})
        invalid_values = [day for day in self.days_of_week if day not in valid_days]
        if invalid_values:
            raise ValidationError({"days_of_week": f"Invalid day values: {invalid_values}"})
        if self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "end_date must be >= start_date."})

    @property
    def computed_end_time(self):
        datetime_value = datetime.combine(timezone.localdate(), self.start_time)
        return (datetime_value + timedelta(minutes=60)).time()

    def __str__(self) -> str:
        return f"{self.title} ({self.court.name})"


class Reservation(TimestampedModel):
    court = models.ForeignKey(Court, on_delete=models.PROTECT, related_name="reservations")
    reservation_type = models.CharField(
        max_length=10,
        choices=ReservationType.choices,
        default=ReservationType.NORMAL,
    )
    game_mode = models.CharField(
        max_length=10,
        choices=GameMode.choices,
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=150, blank=True)
    contact_name = models.CharField(max_length=150)
    contact_phone = models.CharField(max_length=50)
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    status = models.CharField(
        max_length=30,
        choices=ReservationStatus.choices,
        default=ReservationStatus.CONFIRMED,
    )
    total_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    paid_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    payment_status = models.CharField(
        max_length=24,
        choices=ReservationPaymentStatus.choices,
        default=ReservationPaymentStatus.PENDING_PAYMENT,
    )
    payment_expires_at = models.DateTimeField(null=True, blank=True)
    requires_admin_review = models.BooleanField(default=False)
    mp_external_reference_base = models.CharField(max_length=80, blank=True)
    is_paid = models.BooleanField(default=False)
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="paid_reservations",
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_reservations",
    )
    recurring_rule = models.ForeignKey(
        "RecurringReservationRule",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generated_reservations",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cancelled_reservations",
    )
    cancellation_reason = models.TextField(blank=True)

    class Meta:
        ordering = ("start_datetime", "court_id")
        indexes = [
            models.Index(fields=("court", "start_datetime")),
            models.Index(fields=("court", "end_datetime")),
            models.Index(fields=("status",)),
            models.Index(fields=("is_paid",)),
            models.Index(fields=("payment_status",)),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("recurring_rule", "court", "start_datetime"),
                condition=Q(reservation_type=ReservationType.CLASS),
                name="uniq_class_per_rule_court_start",
            ),
        ]

    def clean(self):
        if self.start_datetime >= self.end_datetime:
            raise ValidationError({"end_datetime": "end_datetime must be later than start_datetime."})
        if self.reservation_type == ReservationType.NORMAL and not self.game_mode:
            raise ValidationError({"game_mode": "game_mode is required for normal reservations."})
        if self.reservation_type == ReservationType.CLASS:
            if self.game_mode:
                raise ValidationError({"game_mode": "game_mode must be null for class reservations."})
            if not self.title:
                raise ValidationError({"title": "title is required for class reservations."})

    def __str__(self) -> str:
        return f"Reservation #{self.id} - {self.court.name}"

    @property
    def total_amount(self):
        return self.total_price

    @property
    def remaining_amount(self):
        remaining = self.total_price - self.paid_amount
        return max(remaining, 0)


class ReservationPlayer(TimestampedModel):
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name="players",
    )
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    is_member = models.BooleanField(default=False)
    price_applied = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        ordering = ("id",)

    def __str__(self) -> str:
        return f"{self.first_name} {self.last_name}"


class PaymentTransaction(TimestampedModel):
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name="payment_transactions",
    )
    player = models.ForeignKey(
        ReservationPlayer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payment_transactions",
    )
    provider = models.CharField(
        max_length=20,
        choices=PaymentProvider.choices,
        default=PaymentProvider.MERCADOPAGO,
    )
    payment_type = models.CharField(max_length=12, choices=PaymentType.choices)
    preference_id = models.CharField(max_length=120, blank=True)
    payment_id = models.CharField(max_length=120, null=True, blank=True, unique=True)
    external_reference = models.CharField(max_length=120, unique=True)
    status = models.CharField(
        max_length=24,
        choices=PaymentTransactionStatus.choices,
        default=PaymentTransactionStatus.PENDING,
    )
    status_detail = models.CharField(max_length=255, blank=True)
    base_amount = models.DecimalField(max_digits=10, decimal_places=2)
    identification_decimal = models.DecimalField(max_digits=4, decimal_places=2, default="0.19")
    mp_amount = models.DecimalField(max_digits=10, decimal_places=2)
    amount_received = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    payer_email = models.EmailField(blank=True)
    payment_url = models.URLField(max_length=500, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("reservation", "status")),
            models.Index(fields=("external_reference",)),
            models.Index(fields=("payment_id",)),
        ]

    def __str__(self) -> str:
        return f"{self.provider} {self.external_reference} {self.status}"


class BlockedSlot(TimestampedModel):
    court = models.ForeignKey(Court, on_delete=models.PROTECT, related_name="blocked_slots")
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    block_type = models.CharField(max_length=20, choices=BlockType.choices)
    reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_blocked_slots",
    )

    class Meta:
        ordering = ("start_datetime", "court_id")
        indexes = [
            models.Index(fields=("court", "start_datetime")),
            models.Index(fields=("court", "end_datetime")),
        ]

    def clean(self):
        if self.start_datetime >= self.end_datetime:
            raise ValidationError({"end_datetime": "end_datetime must be later than start_datetime."})

    def __str__(self) -> str:
        return f"{self.court.name}: {self.start_datetime} - {self.end_datetime}"


class CancellationRequest(TimestampedModel):
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name="cancellation_requests",
    )
    requester_name = models.CharField(max_length=150)
    requester_phone = models.CharField(max_length=50)
    reason = models.TextField()
    status = models.CharField(
        max_length=12,
        choices=CancellationRequestStatus.choices,
        default=CancellationRequestStatus.PENDING,
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_cancellation_requests",
    )

    class Meta:
        ordering = ("-created_at",)


class NotificationLog(TimestampedModel):
    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name="notification_logs",
    )
    channel = models.CharField(max_length=12, choices=NotificationChannel.choices)
    destination = models.CharField(max_length=1024)
    status = models.CharField(
        max_length=8,
        choices=NotificationStatus.choices,
        default=NotificationStatus.PENDING,
    )
    error_message = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)


class NotificationDevice(TimestampedModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_devices",
    )
    platform = models.CharField(max_length=16, choices=NotificationDevicePlatform.choices)
    provider = models.CharField(
        max_length=16,
        choices=NotificationProvider.choices,
        default=NotificationProvider.FCM,
    )
    token = models.CharField(max_length=1024, unique=True)
    device_id = models.CharField(max_length=128, blank=True)
    enabled = models.BooleanField(default=True)
    last_seen = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-last_seen", "-updated_at")
        indexes = [
            models.Index(fields=("user", "enabled")),
            models.Index(fields=("provider", "platform")),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} {self.platform} {self.provider}"
