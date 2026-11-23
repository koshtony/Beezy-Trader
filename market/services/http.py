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


class RateLimited(requests.HTTPError):
    def __init__(self, msg: str, retry_after: float | None):
        super().__init__(msg)
        self.retry_after = retry_after


_last_hit: dict[str, float] = {}
# Extra seconds between requests after a host answers 429; eases off again on success.
_penalty: dict[str, float] = {}
MAX_PENALTY = 20.0
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
    if "centralbank.go.ke" in host or "eia.gov" in host:
        return float(_cfg("MACRO_DELAY"))
    return 1.0


def _wait_turn(host: str):
    delay = host_delay(host) + _penalty.get(host, 0.0)
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
            "Chrome/130.0 Safari/537.36 BeezyMarkets/1.0"
        )
    last_exc = None
    for attempt in range(retries + 1):
        _wait_turn(host)
        try:
            r = session().get(url, headers=hdrs, timeout=_cfg("HTTP_TIMEOUT"))
            if r.status_code == 429:
                raise RateLimited(f"429 for {url}", _retry_after(r.headers.get("Retry-After")))
            if r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} for {url}")
            if r.status_code >= 400:  # client errors (404 etc.) are permanent: don't retry
                raise PermanentHTTPError(f"{r.status_code} for {url}")
            _ease(host)
            return r.text
        except PermanentHTTPError:
            raise
        except RateLimited as exc:
            last_exc = exc
            pause = _slow_down(host, attempt, exc.retry_after)
            if attempt < retries:
                log.warning("rate limited by %s (%s/%s); retrying in %.0fs", host, attempt + 1, retries + 1, pause)
                time.sleep(pause)
            else:
                log.warning("rate limited by %s; giving up on %s", host, url)
        except requests.RequestException as exc:  # pragma: no cover - network dependent
            last_exc = exc
            log.warning("fetch failed (%s/%s) %s: %s", attempt + 1, retries + 1, url, exc)
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def _retry_after(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:  # an HTTP date; rare, use our own backoff
        return None


def _slow_down(host: str, attempt: int, retry_after: float | None) -> float:
    """After a 429: space out the rest of this run's requests and back off before retrying."""
    with _lock:
        _penalty[host] = min(MAX_PENALTY, max(_penalty.get(host, 0.0) * 2, host_delay(host), 1.0))
    return min(120.0, retry_after if retry_after is not None else 10.0 * (attempt + 1))


def _ease(host: str) -> None:
    if host in _penalty:
        with _lock:
            p = _penalty.get(host, 0.0) * 0.8
            if p < 0.25:
                _penalty.pop(host, None)
            else:
                _penalty[host] = p
