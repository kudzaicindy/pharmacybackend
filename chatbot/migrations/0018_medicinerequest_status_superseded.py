# Generated manually for new lifecycle status

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chatbot', '0017_pharmacycompositescore_distance_pct'),
    ]

    operations = [
        migrations.AlterField(
            model_name='medicinerequest',
            name='status',
            field=models.CharField(
                choices=[
                    ('created', 'Created'),
                    ('validated', 'Validated'),
                    ('broadcasting', 'Broadcasting'),
                    ('awaiting_responses', 'Awaiting Responses'),
                    ('partial', 'Partial'),
                    ('timeout', 'Timeout'),
                    ('responses_received', 'Responses Received'),
                    ('ranking', 'Ranking'),
                    ('completed', 'Completed'),
                    ('expired', 'Expired'),
                    ('superseded', 'Superseded by newer request'),
                ],
                default='created',
                max_length=20,
            ),
        ),
    ]
