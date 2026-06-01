# Prescription image + OCR snapshot for pharmacist verification

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chatbot", "0022_pharmacist_profile_pharmacy_portal_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="medicinerequest",
            name="prescription_image",
            field=models.FileField(
                blank=True,
                help_text="Original prescription image for pharmacist verification (patient upload).",
                null=True,
                upload_to="prescriptions/%Y/%m/",
            ),
        ),
        migrations.AddField(
            model_name="medicinerequest",
            name="prescription_review_snapshot",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Structured OCR snapshot for pharmacists: items, dosages, confidence, notes.",
            ),
        ),
    ]
