import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0007_cancel_future_orphan_recurring_classes"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationlog",
            name="notification_id",
            field=models.UUIDField(db_index=True, default=uuid.uuid4),
        ),
        migrations.AddField(
            model_name="notificationlog",
            name="is_history",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AlterField(
            model_name="notificationlog",
            name="status",
            field=models.CharField(
                choices=[
                    ("RECORDED", "Recorded"),
                    ("PENDING", "Pending"),
                    ("SENT", "Sent"),
                    ("FAILED", "Failed"),
                ],
                default="PENDING",
                max_length=8,
            ),
        ),
    ]
