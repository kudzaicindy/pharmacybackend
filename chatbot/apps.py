from django.apps import AppConfig
import os
from django.conf import settings
from dotenv import load_dotenv

load_dotenv()


class ChatbotConfig(AppConfig):
    name = 'chatbot'

    def ready(self):
        """Optional mongoengine connection when Django is not using MongoDB as its default DB."""
        if settings.DATABASES['default'].get('ENGINE') == 'django_mongodb_backend':
            return
        try:
            import mongoengine
            mongodb_uri = os.getenv('MONGODB_URI')
            if mongodb_uri:
                mongoengine.connect(
                    host=mongodb_uri,
                    alias='default'
                )
                print('[OK] MongoDB connected via mongoengine')
        except Exception as e:
            print(f'[WARNING] MongoDB connection warning: {e}')
            print('Note: Using Django ORM with SQLite/Postgres. MongoDB features may be limited.')
