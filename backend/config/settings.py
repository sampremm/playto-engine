import os
from pathlib import Path
from decouple import config
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# ─── Core ────────────────────────────────────────────────────────────────────
SECRET_KEY = config('SECRET_KEY')   # No default — must be set in environment

DEBUG = config('DEBUG', default=False, cast=bool)

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1').split(',')

# ─── Security Headers (production hardening) ─────────────────────────────────
SECURE_BROWSER_XSS_FILTER       = True
SECURE_CONTENT_TYPE_NOSNIFF     = True
X_FRAME_OPTIONS                 = 'DENY'
REFERRER_POLICY                 = 'strict-origin-when-cross-origin'

# Enable these when behind HTTPS (uncomment for production with TLS termination)
# SECURE_SSL_REDIRECT           = True
# SECURE_HSTS_SECONDS           = 31536000
# SECURE_HSTS_INCLUDE_SUBDOMAINS= True
# SECURE_HSTS_PRELOAD           = True
# SESSION_COOKIE_SECURE         = True
# CSRF_COOKIE_SECURE            = True

# ─── Apps ─────────────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third-party
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    # Local
    'merchants',
    'payouts',
    'webhooks',
]

# ─── Middleware ────────────────────────────────────────────────────────────────
MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',          # Must be first
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

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

WSGI_APPLICATION = 'config.wsgi.application'

# ─── Database Sharding ────────────────────────────────────────────────────────
DATABASE_ROUTERS = ['config.routers.ShardRouter']

DATABASES = {
    'default': {
        **dj_database_url.parse(config('DATABASE_URL')),
        'TEST': {'NAME': 'test_payto_default'},
    },
    'shard_0': {
        **dj_database_url.parse(config('SHARD_0_URL')),
        'TEST': {'NAME': 'test_payto_shard_0'},
    },
    'shard_1': {
        **dj_database_url.parse(config('SHARD_1_URL')),
        'TEST': {'NAME': 'test_payto_shard_1'},
    },
    # Dedicated PostgreSQL database for idempotency keys.
    # In tests, IDEMPOTENCY_DB_ALIAS='default' routes all writes to test_payto_default.
    'idempotency_db': {
        **dj_database_url.parse(config('IDEMPOTENCY_DB_URL')),
        'TEST': {'NAME': 'test_payto_idempotency'},
    },
}

# ─── Redis ────────────────────────────────────────────────────────────────────
# db=0 → Celery broker (task queue)
# db=1 → Idempotency L1 cache (fast path — PostgreSQL is the durable L2 store)
REDIS_URL             = config('REDIS_URL')              # No default — must be set
IDEMPOTENCY_REDIS_URL = config('IDEMPOTENCY_REDIS_URL')  # No default — must be set
IDEMPOTENCY_KEY_TTL   = 60 * 60 * 24                    # 24 hours (matches PG expiry)

CELERY_BROKER_URL     = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER= 'json'

# ─── JWT ──────────────────────────────────────────────────────────────────────
from datetime import timedelta
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME' : timedelta(hours=1),   # Tightened from 24h → 1h
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'AUTH_HEADER_TYPES'     : ('Bearer',),
    'ROTATE_REFRESH_TOKENS' : True,
    'BLACKLIST_AFTER_ROTATION': True,
}

# ─── DRF — global defaults + throttling ───────────────────────────────────────
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    # Rate limiting: 300 authenticated/min, 20 anonymous/min
    # Dashboard makes ~5 requests per load; 300 handles multiple tabs fine.
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '20/min',
        'user': '300/min',
    },
}

# ─── CORS ─────────────────────────────────────────────────────────────────────
# No longer "allow all" — restricted to explicit frontend origins only
CORS_ALLOW_ALL_ORIGINS  = False
CORS_ALLOWED_ORIGINS    = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:5173,http://127.0.0.1:5173'
).split(',')
CORS_ALLOW_CREDENTIALS  = False   # No cookies over CORS

from corsheaders.defaults import default_headers
CORS_ALLOW_HEADERS = list(default_headers) + [
    'idempotency-key',
]

# ─── Auth ─────────────────────────────────────────────────────────────────────
AUTH_USER_MODEL = 'merchants.Merchant'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 10}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ─── Internationalisation ────────────────────────────────────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE     = 'UTC'
USE_I18N      = True
USE_TZ        = True

STATIC_URL          = 'static/'
DEFAULT_AUTO_FIELD  = 'django.db.models.BigAutoField'

# ─── Test overrides ───────────────────────────────────────────────────────────
import sys
if 'test' in sys.argv:
    CELERY_TASK_ALWAYS_EAGER = True
    # Allow insecure key in test environment only
    SECRET_KEY = 'test-only-insecure-key-that-is-long-enough-for-jwt'
    # Route idempotency keys to the default test DB (no separate idempotency_db in tests)
    IDEMPOTENCY_DB_ALIAS = 'default'
