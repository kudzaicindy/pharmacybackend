from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chatbot', '0019_pharmacysettings_pharmacysettingshistory_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='patientprofile',
            name='mfa_totp_enabled',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='patientprofile',
            name='mfa_totp_secret',
            field=models.CharField(
                blank=True,
                help_text='Base32 authenticator secret; used when mfa_totp_enabled is true.',
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name='pharmacist',
            name='mfa_totp_enabled',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='pharmacist',
            name='mfa_totp_secret',
            field=models.CharField(
                blank=True,
                help_text='Base32 authenticator secret; used when mfa_totp_enabled is true.',
                max_length=64,
            ),
        ),
    ]
