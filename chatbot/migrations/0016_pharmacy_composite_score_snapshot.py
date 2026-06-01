# Generated manually for pharmacy composite score history

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chatbot', '0015_pharmacy_verification_default_pending'),
    ]

    operations = [
        migrations.CreateModel(
            name='PharmacyCompositeScoreSnapshot',
            fields=[
                ('snapshot_id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('ranking_score_0_100', models.PositiveSmallIntegerField()),
                ('price_competitiveness_pct', models.FloatField()),
                ('response_rate_pct', models.FloatField()),
                ('stock_reliability_pct', models.FloatField()),
                ('patient_rating_pct', models.FloatField()),
                ('leaderboard_rank', models.PositiveIntegerField(blank=True, null=True)),
                ('leaderboard_total', models.PositiveIntegerField(blank=True, null=True)),
                ('formula', models.CharField(default='0.3P+0.2R+0.15S+0.2T', help_text='Composite weights at time of snapshot', max_length=64)),
                ('pharmacy', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='composite_score_snapshots', to='chatbot.pharmacy')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='pharmacycompositescoresnapshot',
            index=models.Index(fields=['pharmacy', 'created_at'], name='chatbot_pha_pharmacy_created_idx'),
        ),
    ]
