"""Gunicorn settings for production (`gunicorn beezy.wsgi -c gunicorn.conf.py`).

One process with threads keeps memory low (~100-200 MB) and lets the scheduler, the job runner and
the page cache share a process. Raise WEB_CONCURRENCY for more CPU; the scheduler still runs once.
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
timeout = 60  # the slowest request (portfolio calculator) takes well under a second
graceful_timeout = 20
keepalive = 5
forwarded_allow_ips = "*"  # behind the platform's proxy, which sets X-Forwarded-*
accesslog = "-" if os.environ.get("GUNICORN_ACCESS_LOG") == "1" else None
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None


def post_worker_init(worker):
    """Start the 08:30/12:30/17:30 refresh scheduler in the worker when BEEZY_SCHEDULER=1."""
    from django.conf import settings

    if settings.BEEZY.get("SCHEDULER"):
        from market.services import scheduler

        scheduler.start_thread()
