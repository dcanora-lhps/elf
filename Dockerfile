FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=elf.settings

RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 ca-certificates curl tini \
 && rm -rf /var/lib/apt/lists/*

RUN addgroup --system --gid 1000 elf \
 && adduser --system --uid 1000 --gid 1000 --home /app --shell /usr/sbin/nologin elf

WORKDIR /app

COPY requirements.txt /app/
RUN pip install -r requirements.txt

COPY --chown=elf:elf manage.py /app/
COPY --chown=elf:elf elf /app/elf
COPY --chown=elf:elf app /app/app
COPY --chown=elf:elf templates /app/templates
COPY --chown=elf:elf docker /app/docker

# Collect static into STATIC_ROOT at build time. A dummy SECRET_KEY is fine here;
# the real one comes from env at runtime.
RUN DJANGO_SECRET_KEY=build-time-dummy DJANGO_DEBUG=0 \
    python manage.py collectstatic --noinput

USER elf

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS -o /dev/null http://127.0.0.1:8000/admin/login/ || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["gunicorn", "elf.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--forwarded-allow-ips=*"]
