"""
Expire reservations past their 2-hour window and release reserved stock.

Run via cron (e.g. every 5–10 minutes):
    python manage.py expire_reservations
    python manage.py expire_reservations --dry-run
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from chatbot.models import Reservation, PharmacyInventory


class Command(BaseCommand):
    help = 'Mark expired reservations and release reserved_quantity on inventory'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview what would be updated without making changes',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        now = timezone.now()

        expired = Reservation.objects.filter(
            status__in=['pending', 'confirmed'],
            expires_at__lt=now,
        )
        count = expired.count()
        if count == 0:
            self.stdout.write(self.style.SUCCESS('No reservations to expire.'))
            return

        if dry_run:
            self.stdout.write(self.style.WARNING(f'[DRY RUN] Would expire {count} reservation(s):'))
            for r in expired[:10]:
                self.stdout.write(f'  {r.reservation_id} - {r.medicine_name} x{r.quantity} @ {r.pharmacy.name}')
            if count > 10:
                self.stdout.write(f'  ... and {count - 10} more')
            return

        updated = 0
        with transaction.atomic():
            for r in expired:
                inv = PharmacyInventory.objects.filter(
                    pharmacy=r.pharmacy,
                    medicine_name__iexact=r.medicine_name,
                ).first()
                if inv:
                    inv.reserved_quantity = max(0, inv.reserved_quantity - r.quantity)
                    inv.save(update_fields=['reserved_quantity'])
                r.status = 'expired'
                r.save(update_fields=['status'])
                updated += 1
        self.stdout.write(self.style.SUCCESS(f'Expired {updated} reservation(s) and released stock.'))
