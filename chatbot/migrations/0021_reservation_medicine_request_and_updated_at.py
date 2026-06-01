# Generated manually for patient pickup linkage

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chatbot", "0020_patient_pharmacist_totp_mfa"),
    ]

    operations = [
        migrations.AddField(
            model_name="reservation",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, help_text="Bumped on confirm/complete/expiry flows for cache freshness"),
        ),
        migrations.AddField(
            model_name="reservation",
            name="medicine_request",
            field=models.ForeignKey(
                blank=True,
                help_text="Broadcast this reservation was created from (patient pickup / dedupe)",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="reservations",
                to="chatbot.medicinerequest",
            ),
        ),
    ]
