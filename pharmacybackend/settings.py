"""
Django settings for pharmacybackend project.
"""

import os
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv, dotenv_values
import environ

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env from project root (so it works regardless of current working directory)
_env_file = BASE_DIR / '.env'
_env_example = BASE_DIR / '.env.example'
if _env_file.exists():
    load_dotenv(_env_file)
    # `.env` should win for Mongo switches: a shell may still have USE_SQL_BACKEND / empty MONGODB_URI from an old session.
    _parsed_env = dotenv_values(_env_file)
    for _mongo_key in ('MONGODB_URI', 'MONGODB_DB_NAME', 'DJANGO_USE_MONGODB', 'USE_SQL_BACKEND'):
        if _mongo_key not in _parsed_env:
            continue
        _mongo_val = _parsed_env[_mongo_key]
        if _mongo_val is None:
            continue
        _mongo_str = str(_mongo_val).strip()
        if _mongo_key == 'MONGODB_URI' and not _mongo_str:
            continue
        os.environ[_mongo_key] = _mongo_str
elif _env_example.exists():
    load_dotenv(_env_example)
    if os.getenv('DEBUG', '').lower() == 'true':
        print("[INFO] Loaded .env.example (no .env found). Copy .env.example to .env and add your API keys.")
else:
    load_dotenv()  # fallback: current directory

_YES = frozenset({'1', 'true', 'yes', 'on'})
_NO = frozenset({'0', 'false', 'no', 'off'})


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == '':
        return default
    s = str(raw).strip().lower()
    if s in _YES:
        return True
    if s in _NO:
        return False
    return default


# -----------------------------------------------------------------------------
# Canonical datastore: MongoDB (django-mongodb-backend). ``default`` carries sessions,
# contrib (mongo shim apps), chatbot models, etc.
# Set USE_SQL_BACKEND=true only when you deliberately want SQLite/Postgres-only
# (e.g. a fork CI job or quick script without Atlas).
# -----------------------------------------------------------------------------
USE_SQL_BACKEND = _env_bool('USE_SQL_BACKEND', default=False)

MONGODB_URI = os.getenv('MONGODB_URI', '').strip()
# Optional SQLite file to read from when importing legacy data into MongoDB
LEGACY_SQLITE_PATH = os.getenv('LEGACY_SQLITE_PATH', '').strip()

explicit_mongo = os.getenv('DJANGO_USE_MONGODB', '').strip().lower()

if USE_SQL_BACKEND:
    DJANGO_USE_MONGODB = False
elif explicit_mongo in _NO:
    DJANGO_USE_MONGODB = False
elif explicit_mongo in _YES or bool(MONGODB_URI):
    DJANGO_USE_MONGODB = True
else:
    # Default for this deployment: Mongo; connection string required below.
    DJANGO_USE_MONGODB = True

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/4.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-change-this-in-production')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.getenv('DEBUG', 'True') == 'True'

ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

# Cross-origin POSTs with session cookies (e.g. admin login from Vite) require trusted Origin.
# Must include scheme (https://...) per Django 4+.
CSRF_TRUSTED_ORIGINS = [
    'http://localhost:3000',
    'http://localhost:5173',
    'http://127.0.0.1:3000',
    'http://127.0.0.1:5173',
]
_csrf_extra = os.getenv('CSRF_TRUSTED_ORIGINS', '').strip()
if _csrf_extra:
    for _origin in _csrf_extra.split(','):
        _origin = _origin.strip()
        if _origin and _origin not in CSRF_TRUSTED_ORIGINS:
            CSRF_TRUSTED_ORIGINS.append(_origin)
_cors_one = os.getenv('CORS_ORIGIN', '').strip()
if _cors_one and _cors_one not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(_cors_one)


# Application definition

_APPS_REST = [
    'rest_framework',
    'corsheaders',
    'channels',
    'api',
    'chatbot',
]

if DJANGO_USE_MONGODB:
    if not MONGODB_URI:
        raise ImproperlyConfigured(
            'MongoDB is required: set MONGODB_URI for the default database. '
            'For SQLite/Postgres-only dev, set USE_SQL_BACKEND=true.'
        )
    INSTALLED_APPS = [
        'pharmacybackend.mongo_contrib_apps.MongoAdminConfig',
        'pharmacybackend.mongo_contrib_apps.MongoAuthConfig',
        'pharmacybackend.mongo_contrib_apps.MongoContentTypesConfig',
        'django.contrib.sessions',
        'django.contrib.messages',
        'django.contrib.staticfiles',
        'django_mongodb_backend',
        *_APPS_REST,
    ]
else:
    INSTALLED_APPS = [
        'django.contrib.admin',
        'django.contrib.auth',
        'django.contrib.contenttypes',
        'django.contrib.sessions',
        'django.contrib.messages',
        'django.contrib.staticfiles',
        *_APPS_REST,
    ]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'pharmacybackend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'pharmacybackend.wsgi.application'
ASGI_APPLICATION = 'pharmacybackend.asgi.application'

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}


# Database — Mongo canonical default (`default` alias). SQL branches only apply when Mongo is off.

env = environ.Env()

if DJANGO_USE_MONGODB:
    from pymongo.uri_parser import parse_uri

    def _mongo_db_name(uri: str) -> str:
        name = os.getenv('MONGODB_DB_NAME', '').strip()
        if name:
            return name
        try:
            parsed = parse_uri(uri)
            if parsed.get('database'):
                return parsed['database']
        except Exception:
            pass
        return 'pharmacybackend'

    DATABASES = {
        'default': {
            'ENGINE': 'django_mongodb_backend',
            'HOST': MONGODB_URI,
            'NAME': _mongo_db_name(MONGODB_URI),
        }
    }
    DATABASE_ROUTERS = ['django_mongodb_backend.routers.MongoRouter']
    MIGRATION_MODULES = {
        'admin': 'mongo_migrations.admin',
        'auth': 'mongo_migrations.auth',
        'contenttypes': 'mongo_migrations.contenttypes',
    }
    if LEGACY_SQLITE_PATH:
        _legacy_sqlite = Path(LEGACY_SQLITE_PATH)
        if not _legacy_sqlite.is_absolute():
            _legacy_sqlite = BASE_DIR / _legacy_sqlite
        if _legacy_sqlite.is_file():
            DATABASES['legacy'] = {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': _legacy_sqlite,
            }
            DATABASE_ROUTERS = [
                'django_mongodb_backend.routers.MongoRouter',
                'pharmacybackend.db_routers.LegacySqliteRouter',
            ]
elif os.getenv('DATABASE_URL'):
    DATABASES = {'default': env.db_url_config(os.environ['DATABASE_URL'])}
    DATABASE_ROUTERS = []
    MIGRATION_MODULES = {}
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
    DATABASE_ROUTERS = []
    MIGRATION_MODULES = {}


# Password validation
# https://docs.djangoproject.com/en/4.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/4.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.2/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# Media files
MEDIA_URL = 'media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = (
    'django_mongodb_backend.fields.ObjectIdAutoField'
    if DJANGO_USE_MONGODB
    else 'django.db.models.BigAutoField'
)

# REST Framework settings
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
}

# Pharmacist (/ patient later) Bearer tokens issued after login (`access` ~1h, `refresh` configurable).
JWT_ACCESS_MINUTES = int(os.getenv('JWT_ACCESS_MINUTES', '60') or '60')
JWT_REFRESH_DAYS = int(os.getenv('JWT_REFRESH_DAYS', '7') or '7')

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=JWT_ACCESS_MINUTES),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=JWT_REFRESH_DAYS),
    'ROTATE_REFRESH_TOKENS': False,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

# CORS settings
# Allow multiple frontend dev servers
CORS_ALLOWED_ORIGINS = [
    'http://localhost:3000',   # React dev server (Create React App)
    'http://localhost:5173',   # Vite dev server
    'http://127.0.0.1:3000',
    'http://127.0.0.1:5173',
]

# Also check environment variable for additional origins
cors_origin_env = os.getenv('CORS_ORIGIN', '')
if cors_origin_env and cors_origin_env not in CORS_ALLOWED_ORIGINS:
    CORS_ALLOWED_ORIGINS.append(cors_origin_env)

# For development only - allow all origins (remove in production!)
if DEBUG:
    CORS_ALLOW_ALL_ORIGINS = True
else:
    CORS_ALLOW_ALL_ORIGINS = False

CORS_ALLOW_CREDENTIALS = True

# Allow common headers
CORS_ALLOW_HEADERS = [
    'accept',
    'accept-encoding',
    'authorization',
    'content-type',
    'dnt',
    'origin',
    'user-agent',
    'x-csrftoken',
    'x-requested-with',
]

# Outbound email (SMTP). Leave EMAIL_HOST empty to skip sends (helpers log a warning).
EMAIL_BACKEND = os.getenv('EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = os.getenv('EMAIL_HOST', '').strip()
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587') or '587')
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'true').lower() in ('1', 'true', 'yes')
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '').strip()
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '').strip()
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', '').strip() or 'noreply@localhost'
APP_PUBLIC_URL = os.getenv('APP_PUBLIC_URL', '').strip()
# When true, staff/patient/pharmacist logins email a 6-digit code; client must POST verify with otp_challenge + code.
LOGIN_EMAIL_2FA = os.getenv('LOGIN_EMAIL_2FA', '').lower() in ('1', 'true', 'yes')
