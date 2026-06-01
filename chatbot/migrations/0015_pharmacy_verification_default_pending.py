from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chatbot', '0014_platform_admin_settings_and_safety_reviews'),
    ]

    operations = [
        migrations.AlterField(
            model_name='pharmacy',
            name='verification_status',
            field=models.CharField(
                choices=[
                    ('verified', 'Verified'),
                    ('pending_review', 'Pending review'),
                    ('suspended', 'Suspended'),
                ],
                default='pending_review',
                help_text='New pharmacies default to pending until an admin sets verified.',
                max_length=20,
            ),
        ),
    ]
