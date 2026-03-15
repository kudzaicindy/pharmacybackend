"""
UC-S07: Mark medicine requests as expired when no pharmacies respond by deadline.

Run via cron (e.g. every 5 minutes):
    python manage.py expire_requests
    python manage.py expire_requests --dry-run
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from chatbot.models import MedicineRequest


class Command(BaseCommand):
    help = 'Mark medicine requests as expired when expires_at passed with zero responses'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview what would be updated without making changes',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        now = timezone.now()

        # Find requests still awaiting responses past their expiry
        requests = MedicineRequest.objects.filter(
            status__in=['broadcasting', 'awaiting_responses'],
            expires_at__lt=now,
        )

        count = requests.count()
        if count == 0:
            self.stdout.write(self.style.SUCCESS('No requests to expire.'))
            return

        if dry_run:
            self.stdout.write(self.style.WARNING(f'[DRY RUN] Would expire {count} request(s):'))
            for req in requests[:10]:
                self.stdout.write(f'  {req.request_id} - {req.medicine_names} (expired {req.expires_at})')
            if count > 10:
                self.stdout.write(f'  ... and {count - 10} more')
            return

        updated = requests.update(status='expired')
        self.stdout.write(self.style.SUCCESS(f'Marked {updated} request(s) as expired.'))
