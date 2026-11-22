# Beezy Markets — single container: web server + in-process refresh scheduler.
# Mount a persistent volume at /data (SQLite database, job locks).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Hashed, pre-compressed static files (WhiteNoise). The key is only used for this build step.
RUN DJANGO_DEBUG=0 DJANGO_SECRET_KEY=collectstatic-only python manage.py collectstatic --noinput -v 0 \
    && chmod +x docker/start.sh

ENV DJANGO_DEBUG=0 \
    BEEZY_DATA_DIR=/data \
    BEEZY_SCHEDULER=1 \
    PORT=8000

EXPOSE 8000
CMD ["docker/start.sh"]
