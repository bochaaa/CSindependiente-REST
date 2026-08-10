from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("reservations", "0009_paymentprovider_transfer"),
    ]

    operations = [
        migrations.AddField(
            model_name="reservationplayer",
            name="is_payment_exempt",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="reservationplayer",
            name="payment_exemption_reason",
            field=models.CharField(
                blank=True,
                choices=[("employee", "Empleado"), ("club_player", "Jugador del club")],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="reservationplayer",
            name="payment_exempted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="reservationplayer",
            name="payment_exempted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="payment_exempted_reservation_players",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
