"""
UC-S09: Clean up expired anonymous session data for privacy compliance.

Removes conversations and related data older than configured timeout (default 24h).
Run via cron daily:
    python manage.py clean_expired_sessions
    python manage.py clean_expired_sessions --hours 48 --dry-run
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from chatbot.models import ChatConversation, ChatMessage, MedicineRequest


class Command(BaseCommand):
    help = 'Delete expired anonymous session data (conversations, messages)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--hours',
            type=int,
            default=24,
            help='Sessions older than this many hours are deleted (default: 24)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview what would be deleted without making changes',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        hours = options['hours']
        cutoff = timezone.now() - timedelta(hours=hours)

        # Only clean conversations with no linked user (anonymous)
        expired = ChatConversation.objects.filter(
            user__isnull=True,
            created_at__lt=cutoff,
        )

        count = expired.count()
        if count == 0:
            self.stdout.write(self.style.SUCCESS(f'No sessions older than {hours}h to clean.'))
            return

        if dry_run:
            self.stdout.write(self.style.WARNING(f'[DRY RUN] Would delete {count} conversation(s) (older than {hours}h)'))
            for conv in expired[:5]:
                msg_count = conv.messages.count()
                req_count = conv.medicine_requests.count()
                self.stdout.write(f'  {conv.conversation_id} - {msg_count} messages, {req_count} requests')
            if count > 5:
                self.stdout.write(f'  ... and {count - 5} more')
            return

        # Delete in order (messages and requests have FK to conversation)
        deleted_convs = 0
        deleted_msgs = 0
        for conv in expired:
            deleted_msgs += conv.messages.count()
            conv.delete()
            deleted_convs += 1

        self.stdout.write(self.style.SUCCESS(
            f'Deleted {deleted_convs} conversation(s) and {deleted_msgs} message(s).'
        ))
