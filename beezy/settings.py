"""Django settings for Beezy Markets (NSE stocks). Everything deploy-specific comes from environment variables."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


from django.core.exceptions import ImproperlyConfigured

DEV_SECRET = "dev-insecure-beezy-trader-key-change-me-in-production"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", DEV_SECRET)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
if not DEBUG and SECRET_KEY == DEV_SECRET:
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY when DJANGO_DEBUG=0.")
ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

# Railway: trust its generated domain and health checker without extra configuration.
ON_RAILWAY = bool(os.environ.get("RAILWAY_ENVIRONMENT"))
if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
    ALLOWED_HOSTS.append(os.environ["RAILWAY_PUBLIC_DOMAIN"])
    CSRF_TRUSTED_ORIGINS.append(f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}")
if ON_RAILWAY:
    ALLOWED_HOSTS.append("healthcheck.railway.app")

# The database and job locks live here; point it at a persistent volume in production.
DATA_DIR = Path(os.environ.get("BEEZY_DATA_DIR", BASE_DIR))
DB_PATH = Path(os.environ.get("BEEZY_DB_PATH", DATA_DIR / "db.sqlite3"))
try:  # SQLite creates the file but not its folder (e.g. /data in a pre-deploy step, before a volume is mounted)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

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
                "market.context_processors.asset_version",
            ],
        },
    },
]

WSGI_APPLICATION = "beezy.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DB_PATH,
        "OPTIONS": {
            # WAL lets the web server read while a scrape/analysis job writes.
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            "transaction_mode": "IMMEDIATE",
            "timeout": 30,
        },
        # Reuse connections between requests (skips reconnect + PRAGMAs); 0 in development.
        "CONN_MAX_AGE": 0 if DEBUG else 300,
        "CONN_HEALTH_CHECKS": True,
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
    "loggers": {
        "market": {"handlers": ["console"], "level": os.environ.get("BEEZY_LOG_LEVEL", "INFO"), "propagate": False},
        # 500s reach the platform logs in production too (4xx from bots would only be noise).
        "django": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "gunicorn.error": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# --- Production hardening (active when DJANGO_DEBUG=0) ----------------------------------
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SSL_REDIRECT", "1") == "1"
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_REDIRECT_EXEMPT = [r"^healthz$"]  # platform health checks call over plain HTTP
    # HSTS covers this host only: *.up.railway.app is shared, and preloading needs a domain you own.
    SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W021"]
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
# Behind a reverse proxy set this to the number of proxies so rate limits see the real client IP.
NINJA_NUM_PROXIES = int(os.environ.get("BEEZY_NUM_PROXIES", "1" if ON_RAILWAY else "0"))

# --- Beezy Markets data-source settings -------------------------------------------------
BEEZY = {
    # afx.kwayisi.org publishes "Crawl-delay: 60" in robots.txt — we honour it.
    "AFX_BASE": "https://afx.kwayisi.org",
    "AFX_CRAWL_DELAY": int(os.environ.get("BEEZY_AFX_DELAY", "60")),
    "STOCKANALYSIS_BASE": "https://stockanalysis.com",
    "STOCKANALYSIS_DELAY": float(os.environ.get("BEEZY_SA_DELAY", "2.5")),
    "NEWS_DELAY": float(os.environ.get("BEEZY_NEWS_DELAY", "1.5")),
    # Central Bank of Kenya and EIA pages (about nine requests a day).
    "MACRO_DELAY": float(os.environ.get("BEEZY_MACRO_DELAY", "3")),
    "NEWS_LOOKBACK_DAYS": 60,
    "USER_AGENT": "BeezyMarkets/1.0 (personal NSE research tool; low-frequency)",
    # Daily price list sources, tried in this order (see market/services/prices.py).
    "PRICE_SOURCES": os.environ.get("BEEZY_PRICE_SOURCES", "afx,mansa,africanfinancials,rokam"),
    "HTTP_TIMEOUT": 30,
    # Liquidity floor for recommendations: average daily turnover (KES) over 30 sessions.
    "MIN_AVG_TURNOVER": 500_000,
    # Lets cron/scripts call POST /api/jobs/refresh with an "X-API-Key" header. Empty = disabled.
    "API_KEY": os.environ.get("BEEZY_API_KEY", ""),
    # Interactive docs at /api/docs are on in DEBUG; set BEEZY_API_DOCS=1 to keep them in production.
    "API_DOCS": os.environ.get("BEEZY_API_DOCS", "0") == "1",
    # Run the 08:30 / 12:30 / 17:30 refresh inside the web process (for single-container hosts).
    "SCHEDULER": os.environ.get("BEEZY_SCHEDULER", "0") == "1",
    # Seed an empty database automatically (≈75 min, AFX crawl delay) when the scheduler starts.
    "AUTO_BOOTSTRAP": os.environ.get("BEEZY_AUTO_BOOTSTRAP", "1") == "1",
    # Old rows pruned once a day (price history is never pruned).
    "KEEP_DAYS": {"runs": 90, "jobs": 60, "projections": 400, "news": 730},
}
