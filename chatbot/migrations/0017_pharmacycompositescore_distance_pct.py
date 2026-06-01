# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chatbot', '0016_pharmacy_composite_score_snapshot'),
    ]

    operations = [
        migrations.AddField(
            model_name='pharmacycompositescoresnapshot',
            name='distance_pct',
            field=models.FloatField(
                blank=True,
                help_text='Proximity vs peers from median response distance_km (90d)',
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name='pharmacycompositescoresnapshot',
            name='formula',
            field=models.CharField(
                default='(0.25P+0.18R+0.12S+0.15T+0.15D)*100/85',
                help_text='Composite weights at time of snapshot',
                max_length=96,
            ),
        ),
    ]
