from django.apps import AppConfig
import os
from dotenv import load_dotenv

load_dotenv()


class ChatbotConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'chatbot'
    
    def ready(self):
        """Initialize MongoDB connection when app is ready"""
        try:
            import mongoengine
            mongodb_uri = os.getenv('MONGODB_URI')
            if mongodb_uri:
                mongoengine.connect(
                    host=mongodb_uri,
                    alias='default'
                )
                print("[OK] MongoDB connected via mongoengine")
        except Exception as e:
            print(f"[WARNING] MongoDB connection warning: {e}")
            print("Note: Using Django ORM with SQLite. MongoDB features may be limited.")
