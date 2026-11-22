#!/bin/sh
# Container start: guarded seed install + backup before migrations + migrate, then serve.
# The scheduler starts inside gunicorn (BEEZY_SCHEDULER=1).
set -eu
python manage.py prepare_data
exec gunicorn beezy.wsgi -c gunicorn.conf.py
