from django import forms
from django.contrib import admin
from django.utils import timezone

from .models import (
    BlockedSlot,
    CancellationRequest,
    ClubSchedule,
    Court,
    DayOfWeek,
    NotificationDevice,
    NotificationLog,
    PaymentTransaction,
    PriceRule,
    RecurringReservationRule,
    Reservation,
    ReservationPlayer,
    SpecialSchedule,
)
from .services import deactivate_recurring_rule


class RecurringReservationRuleAdminForm(forms.ModelForm):
    days_of_week = forms.MultipleChoiceField(
        choices=DayOfWeek.choices,
        required=True,
        widget=admin.widgets.FilteredSelectMultiple("Days of week", is_stacked=False),
        help_text="Selecciona uno o mas dias. No hace falta escribir JSON.",
    )

    class Meta:
        model = RecurringReservationRule
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and isinstance(self.instance.days_of_week, list):
            self.initial["days_of_week"] = self.instance.days_of_week

    def clean_days_of_week(self):
        value = self.cleaned_data.get("days_of_week") or []
        if not value:
            raise forms.ValidationError("Debes seleccionar al menos un dia.")
        return value


@admin.register(RecurringReservationRule)
class RecurringReservationRuleAdmin(admin.ModelAdmin):
    form = RecurringReservationRuleAdminForm
    list_display = ("id", "title", "court", "start_time", "computed_end_time", "start_date", "end_date", "active")
    list_filter = ("active", "court")
    search_fields = ("title", "court__name")
    actions = ("deactivate_and_cancel_future_classes",)
    readonly_fields = ("created_by", "created_at", "updated_at")

    @admin.display(description="End time")
    def computed_end_time(self, obj):
        return obj.computed_end_time

    @admin.action(description="Deactivate rules and cancel future generated classes")
    def deactivate_and_cancel_future_classes(self, request, queryset):
        cancelled_total = 0
        for rule in queryset:
            _, cancelled_count = deactivate_recurring_rule(
                recurring_rule=rule,
                deactivated_by=request.user,
                cancellation_reason="Desactivada desde Django admin.",
            )
            cancelled_total += cancelled_count
        self.message_user(
            request,
            f"Rules deactivated: {queryset.count()}. Future classes cancelled: {cancelled_total}.",
        )


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "court",
        "reservation_type",
        "status",
        "is_paid",
        "payment_status",
        "paid_amount",
        "paid_at",
        "paid_confirmed_by",
        "contact_name",
        "title",
        "start_datetime",
        "end_datetime",
    )
    list_filter = ("status", "payment_status", "reservation_type", "court", "is_paid")
    search_fields = ("contact_name", "title", "contact_phone", "court__name")
    ordering = ("-start_datetime",)
    readonly_fields = ("created_at", "updated_at", "cancelled_at", "paid_at")


admin.site.register(Court)


@admin.register(ClubSchedule)
class ClubScheduleAdmin(admin.ModelAdmin):
    list_display = ("day_of_week", "open_time", "close_time", "active")
    list_filter = ("active",)
    ordering = ("day_of_week",)


@admin.register(SpecialSchedule)
class SpecialScheduleAdmin(admin.ModelAdmin):
    list_display = ("date", "closed", "open_time", "close_time", "reason")
    list_filter = ("closed",)
    search_fields = ("reason",)
    ordering = ("date",)


@admin.register(PriceRule)
class PriceRuleAdmin(admin.ModelAdmin):
    list_display = ("id", "game_mode", "player_type", "price", "active", "valid_from", "valid_to")
    list_filter = ("game_mode", "player_type", "active")
    search_fields = ("game_mode", "player_type")
    ordering = ("-valid_from", "game_mode", "player_type")
    actions = ("duplicate_prices_for_today",)

    @admin.action(description="Duplicate selected prices as active from today")
    def duplicate_prices_for_today(self, request, queryset):
        today = timezone.localdate()
        created_count = 0
        for rule in queryset:
            if not type(rule).objects.filter(
                game_mode=rule.game_mode,
                player_type=rule.player_type,
                valid_from=today,
            ).exists():
                type(rule).objects.create(
                    game_mode=rule.game_mode,
                    player_type=rule.player_type,
                    price=rule.price,
                    active=True,
                    valid_from=today,
                    valid_to=None,
                )
                created_count += 1
        self.message_user(request, f"New price rows created for today: {created_count}.")


admin.site.register(ReservationPlayer)
admin.site.register(BlockedSlot)
admin.site.register(CancellationRequest)


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ("id", "reservation", "channel", "destination", "status", "created_at")
    list_filter = ("channel", "status")
    search_fields = ("destination", "reservation__contact_name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(NotificationDevice)
class NotificationDeviceAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "platform", "provider", "enabled", "last_seen", "updated_at")
    list_filter = ("platform", "provider", "enabled")
    search_fields = ("user__username", "token", "device_id")
    readonly_fields = ("created_at", "updated_at", "last_seen")


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "reservation",
        "player",
        "provider",
        "payment_type",
        "status",
        "base_amount",
        "mp_amount",
        "amount_received",
        "payment_id",
        "external_reference",
        "paid_at",
    )
    list_filter = ("provider", "payment_type", "status")
    search_fields = ("external_reference", "payment_id", "preference_id", "reservation__contact_name")
    readonly_fields = ("created_at", "updated_at", "paid_at")
