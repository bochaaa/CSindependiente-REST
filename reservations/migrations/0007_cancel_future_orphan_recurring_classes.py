from django.db import migrations
from django.utils import timezone


def cancel_future_orphan_recurring_classes(apps, schema_editor):
    Reservation = apps.get_model("reservations", "Reservation")
    now = timezone.now()
    Reservation.objects.filter(
        reservation_type="CLASS",
        recurring_rule__isnull=True,
        contact_name="Admin",
        contact_phone="N/A",
        start_datetime__gte=now,
    ).exclude(status="CANCELLED").update(
        status="CANCELLED",
        cancelled_at=now,
        cancellation_reason="Regla recurrente eliminada; clase futura liberada.",
        updated_at=now,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0006_alter_notificationlog_destination"),
    ]

    operations = [
        migrations.RunPython(
            cancel_future_orphan_recurring_classes,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
