# Generated migration for pharmacy portal settings

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chatbot", "0021_reservation_medicine_request_and_updated_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="pharmacy",
            name="tax_number",
            field=models.CharField(blank=True, help_text="Invoice / registry tax id (pharmacy portal)", max_length=120),
        ),
        migrations.AddField(
            model_name="pharmacy",
            name="whatsapp",
            field=models.CharField(blank=True, help_text="WhatsApp dial string for patients", max_length=40),
        ),
        migrations.AddField(
            model_name="pharmacy",
            name="website",
            field=models.URLField(blank=True, help_text="Pharmacy public website URL"),
        ),
        migrations.AddField(
            model_name="pharmacy",
            name="description",
            field=models.TextField(blank=True, help_text="Short public / directory description"),
        ),
        migrations.AddField(
            model_name="pharmacist",
            name="display_name",
            field=models.CharField(
                blank=True,
                help_text="Portal display name (defaults to full name when empty)",
                max_length=200,
            ),
        ),
        migrations.AddField(
            model_name="pharmacysettings",
            name="accepting_requests",
            field=models.BooleanField(
                default=True,
                help_text="When false: pharmacy hides from new broadcasts / live search and pauses pharmacist inbox.",
            ),
        ),
        migrations.AddField(
            model_name="pharmacysettings",
            name="pause_outside_opening_hours",
            field=models.BooleanField(
                default=False,
                help_text="UI hint — auto-pause outside opening_hours when supported by integrations.",
            ),
        ),
        migrations.AddField(
            model_name="pharmacysettings",
            name="notify_quiet_hours",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='e.g. {"start_local": "22:00", "end_local": "07:30"} — UI/scheduling hints',
            ),
        ),
        migrations.AddField(
            model_name="pharmacysettings",
            name="notifications_digest_frequency",
            field=models.CharField(
                choices=[
                    ("instant", "Instant"),
                    ("daily", "Daily digest"),
                    ("weekly", "Weekly digest"),
                    ("muted", "Muted"),
                ],
                default="instant",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="pharmacysettings",
            name="service_radius_km",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="Preferential service radius in km — informational / future ranking use",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="pharmacysettings",
            name="service_pickup_available",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="pharmacysettings",
            name="service_delivery_available",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="pharmacysettings",
            name="service_areas_covered",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="List of locality names or polygons as JSON primitives",
            ),
        ),
    ]
