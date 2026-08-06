from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0008_notification_history"),
    ]

    operations = [
        migrations.AlterField(
            model_name="paymenttransaction",
            name="provider",
            field=models.CharField(
                choices=[
                    ("mercadopago", "Mercado Pago"),
                    ("cash", "Cash"),
                    ("transfer", "Transferencia QR"),
                ],
                default="mercadopago",
                max_length=20,
            ),
        ),
    ]
