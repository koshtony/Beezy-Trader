"""Who may trigger jobs, and request throttles for the public API."""

from django.conf import settings
from django.utils.crypto import constant_time_compare
from ninja.security import APIKeyHeader, django_auth_is_staff
from ninja.throttling import AnonRateThrottle, AuthRateThrottle
from ninja.utils import check_csrf

LOOPBACK = {"127.0.0.1", "::1"}


class ApiKey(APIKeyHeader):
    """`X-API-Key: <BEEZY_API_KEY>` — for cron jobs or scripts on other machines."""

    param_name = "X-API-Key"

    def authenticate(self, request, key):
        expected = settings.BEEZY.get("API_KEY")
        if expected and key and constant_time_compare(key, expected):
            return "api-key"
        return None


def local_dev(request):
    """In DEBUG only: the browser on this machine may trigger jobs (CSRF still enforced)."""
    if settings.DEBUG and request.META.get("REMOTE_ADDR") in LOOPBACK and check_csrf(request) is None:
        return "local"
    return None


# Job triggers: API key, a logged-in staff session (CSRF-checked), or local development.
JOB_AUTH = [ApiKey(), django_auth_is_staff, local_dev]

# Applied to the whole API; the expensive calculator gets a tighter limit of its own.
API_THROTTLE = [AnonRateThrottle("240/m"), AuthRateThrottle("1000/m")]
CALC_THROTTLE = [AnonRateThrottle("30/m"), AuthRateThrottle("120/m")]


def can_trigger_jobs(request) -> bool:
    """Whether to show the refresh buttons (the endpoints re-check)."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and user.is_staff:
        return True
    return settings.DEBUG and request.META.get("REMOTE_ADDR") in LOOPBACK


def safe_url(url: str) -> str:
    """Only http(s) links from third-party feeds are rendered (no javascript: or data: URLs)."""
    url = (url or "").strip()
    return url if url.lower().startswith(("https://", "http://")) else ""
