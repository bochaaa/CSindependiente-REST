from django import forms
from django.contrib import admin

from .models import (
    BlockedSlot,
    CancellationRequest,
    ClubSchedule,
    Court,
    DayOfWeek,
    NotificationLog,
    PriceRule,
    RecurringReservationRule,
    Reservation,
    ReservationPlayer,
    SpecialSchedule,
)


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
    list_display = ("title", "court", "start_time", "start_date", "end_date", "active")
    list_filter = ("active", "court")
    search_fields = ("title", "court__name")


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "court",
        "reservation_type",
        "status",
        "contact_name",
        "title",
        "start_datetime",
        "end_datetime",
    )
    list_filter = ("status", "reservation_type", "court")
    search_fields = ("contact_name", "title", "contact_phone", "court__name")
    ordering = ("-start_datetime",)


admin.site.register(Court)
admin.site.register(ClubSchedule)
admin.site.register(SpecialSchedule)
admin.site.register(PriceRule)
admin.site.register(ReservationPlayer)
admin.site.register(BlockedSlot)
admin.site.register(CancellationRequest)
admin.site.register(NotificationLog)
