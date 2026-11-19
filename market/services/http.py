"""Polite HTTP fetching with per-host minimum intervals and retries."""

import logging
import threading
import time
from urllib.parse import urlparse

import requests
from django.conf import settings

log = logging.getLogger("market.http")

class PermanentHTTPError(Exception):
    """A 4xx response that retrying will not fix."""


_last_hit: dict[str, float] = {}
_lock = threading.Lock()
_session: requests.Session | None = None


def _cfg(key):
    return settings.BEEZY[key]


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": _cfg("USER_AGENT"),
                "Accept-Language": "en-KE,en;q=0.9",
            }
        )
        _session = s
    return _session


def host_delay(host: str) -> float:
    if "kwayisi" in host:
        return float(_cfg("AFX_CRAWL_DELAY"))
    if "stockanalysis" in host:
        return float(_cfg("STOCKANALYSIS_DELAY"))
    if "news.google" in host:
        return float(_cfg("NEWS_DELAY"))
    return 1.0


def _wait_turn(host: str):
    delay = host_delay(host)
    with _lock:
        last = _last_hit.get(host)
        now = time.monotonic()
        wait = 0.0 if last is None else max(0.0, last + delay - now)
        _last_hit[host] = now + wait
    if wait > 0:
        log.debug("waiting %.1fs before hitting %s", wait, host)
        time.sleep(wait)


def fetch(url: str, *, headers: dict | None = None, retries: int = 2, browser_ua: bool = False) -> str:
    """GET a URL as text, waiting for the host's crawl delay first."""
    host = urlparse(url).netloc
    hdrs = dict(headers or {})
    if browser_ua:
        # Some sites serve a stripped page to unknown agents; still identify ourselves.
        hdrs["User-Agent"] = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0 Safari/537.36 BeezyTrader/1.0"
        )
    last_exc = None
    for attempt in range(retries + 1):
        _wait_turn(host)
        try:
            r = session().get(url, headers=hdrs, timeout=_cfg("HTTP_TIMEOUT"))
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} for {url}")
            if r.status_code >= 400:  # client errors (404 etc.) are permanent: don't retry
                raise PermanentHTTPError(f"{r.status_code} for {url}")
            return r.text
        except PermanentHTTPError:
            raise
        except requests.RequestException as exc:  # pragma: no cover - network dependent
            last_exc = exc
            log.warning("fetch failed (%s/%s) %s: %s", attempt + 1, retries + 1, url, exc)
            time.sleep(2 * (attempt + 1))
    raise last_exc  # type: ignore[misc]
