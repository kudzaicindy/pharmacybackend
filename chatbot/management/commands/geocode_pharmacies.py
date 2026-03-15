"""
Management command to geocode pharmacies that don't have coordinates.

Usage:
    python manage.py geocode_pharmacies
    python manage.py geocode_pharmacies --dry-run  # Preview what would be updated
"""

from django.core.management.base import BaseCommand
from chatbot.models import Pharmacy
from chatbot.services import LocationService


class Command(BaseCommand):
    help = 'Geocode pharmacies that are missing latitude/longitude coordinates'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview what would be updated without making changes',
        )
        parser.add_argument(
            '--pharmacy-id',
            type=str,
            help='Geocode a specific pharmacy by ID',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        pharmacy_id = options.get('pharmacy_id')

        # Get pharmacies without coordinates
        if pharmacy_id:
            pharmacies = Pharmacy.objects.filter(pharmacy_id=pharmacy_id)
            if not pharmacies.exists():
                self.stdout.write(self.style.ERROR(f'Pharmacy with ID "{pharmacy_id}" not found'))
                return
        else:
            pharmacies = Pharmacy.objects.filter(
                latitude__isnull=True
            ) | Pharmacy.objects.filter(
                longitude__isnull=True
            )
        
        pharmacies = pharmacies.filter(is_active=True)  # Only active pharmacies
        
        total = pharmacies.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS('All pharmacies already have coordinates!'))
            return

        self.stdout.write(f'Found {total} pharmacy(ies) without coordinates')

        if dry_run:
            self.stdout.write(self.style.WARNING('\n=== DRY RUN MODE - No changes will be made ===\n'))

        geocoded_count = 0
        failed_count = 0

        for pharmacy in pharmacies:
            if not pharmacy.address:
                self.stdout.write(
                    self.style.WARNING(f'Skipping {pharmacy.pharmacy_id} - no address provided')
                )
                continue

            self.stdout.write(f'Geocoding: {pharmacy.name} ({pharmacy.pharmacy_id})')
            self.stdout.write(f'  Address: {pharmacy.address}')

            lat, lon = LocationService.geocode_address(pharmacy.address)
            if not lat or not lon:
                # Fallback: try "Suburb, Harare, Zimbabwe" for known Harare suburbs
                fallbacks = []
                addr_lower = pharmacy.address.lower()
                if 'glenview' in addr_lower or 'glen view' in addr_lower:
                    fallbacks.append('Glen View, Harare, Zimbabwe')
                if 'belgravia' in addr_lower:
                    fallbacks.append('Belgravia, Harare, Zimbabwe')
                if 'avondale' in addr_lower:
                    fallbacks.append('Avondale, Harare, Zimbabwe')
                if 'borrowdale' in addr_lower:
                    fallbacks.append('Borrowdale, Harare, Zimbabwe')
                if 'hatfield' in addr_lower:
                    fallbacks.append('Hatfield, Harare, Zimbabwe')
                if 'mbare' in addr_lower:
                    fallbacks.append('Mbare, Harare, Zimbabwe')
                if 'highfield' in addr_lower or 'high field' in addr_lower:
                    fallbacks.append('Highfield, Harare, Zimbabwe')
                for fb in fallbacks:
                    lat, lon = LocationService.geocode_address(fb)
                    if lat and lon:
                        self.stdout.write(f'  (used fallback: {fb})')
                        break
            if dry_run:
                if lat and lon:
                    self.stdout.write(
                        self.style.SUCCESS(f'  Would set coordinates: {lat}, {lon}')
                    )
                    geocoded_count += 1
                else:
                    self.stdout.write(
                        self.style.ERROR(f'  Could not geocode address')
                    )
                    failed_count += 1
            else:
                if lat and lon:
                    pharmacy.latitude = lat
                    pharmacy.longitude = lon
                    pharmacy.save(update_fields=['latitude', 'longitude'])
                    self.stdout.write(
                        self.style.SUCCESS(f'  [OK] Set coordinates: {lat}, {lon}')
                    )
                    geocoded_count += 1
                else:
                    self.stdout.write(
                        self.style.ERROR(f'  [ERROR] Could not geocode address')
                    )
                    failed_count += 1

        # Summary
        self.stdout.write('\n' + '='*50)
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN SUMMARY:'))
        else:
            self.stdout.write(self.style.SUCCESS('SUMMARY:'))
        self.stdout.write(f'  Total pharmacies processed: {total}')
        self.stdout.write(f'  Successfully geocoded: {geocoded_count}')
        self.stdout.write(f'  Failed: {failed_count}')
        
        if not dry_run and geocoded_count > 0:
            self.stdout.write(
                self.style.SUCCESS(f'\n[SUCCESS] Updated {geocoded_count} pharmacy(ies) with coordinates!')
            )
