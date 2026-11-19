"""Django settings for Beezy Trader — a Nairobi Securities Exchange trading assistant."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

from django.core.exceptions import ImproperlyConfigured

DEV_SECRET = "dev-insecure-beezy-trader-key-change-me-in-production"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", DEV_SECRET)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
if not DEBUG and SECRET_KEY == DEV_SECRET:
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY when DJANGO_DEBUG=0.")
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "market",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "django.middleware.http.ConditionalGetMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if not DEBUG:  # in development runserver serves static files itself
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "beezy.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "beezy.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("BEEZY_DB_PATH", BASE_DIR / "db.sqlite3"),
        "OPTIONS": {
            # WAL lets the web server read while a scrape/analysis job writes.
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            "transaction_mode": "IMMEDIATE",
            "timeout": 30,
        },
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "beezy",
        "TIMEOUT": 300,
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}
WHITENOISE_MAX_AGE = 0 if DEBUG else 60 * 60 * 24 * 30

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "loggers": {"market": {"handlers": ["console"], "level": "INFO"}},
}

# --- Production hardening (active when DJANGO_DEBUG=0) ----------------------------------
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SSL_REDIRECT", "1") == "1"
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
    CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]
SESSION_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
# Behind a reverse proxy set this to the number of proxies so rate limits see the real client IP.
NINJA_NUM_PROXIES = int(os.environ.get("BEEZY_NUM_PROXIES", "0"))

# --- Beezy Trader data-source settings -------------------------------------------------
BEEZY = {
    # afx.kwayisi.org publishes "Crawl-delay: 60" in robots.txt — we honour it.
    "AFX_BASE": "https://afx.kwayisi.org",
    "AFX_CRAWL_DELAY": int(os.environ.get("BEEZY_AFX_DELAY", "60")),
    "STOCKANALYSIS_BASE": "https://stockanalysis.com",
    "STOCKANALYSIS_DELAY": float(os.environ.get("BEEZY_SA_DELAY", "2.5")),
    "NEWS_DELAY": float(os.environ.get("BEEZY_NEWS_DELAY", "1.5")),
    "NEWS_LOOKBACK_DAYS": 60,
    "USER_AGENT": "BeezyTrader/1.0 (personal NSE research tool; low-frequency)",
    "HTTP_TIMEOUT": 30,
    # Liquidity floor for recommendations: average daily turnover (KES) over 30 sessions.
    "MIN_AVG_TURNOVER": 500_000,
    # Lets cron/scripts call POST /api/jobs/refresh with an "X-API-Key" header. Empty = disabled.
    "API_KEY": os.environ.get("BEEZY_API_KEY", ""),
    # Interactive docs at /api/docs are on in DEBUG; set BEEZY_API_DOCS=1 to keep them in production.
    "API_DOCS": os.environ.get("BEEZY_API_DOCS", "0") == "1",
}
