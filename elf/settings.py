"""Django settings for elf project."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name, default=False):
    return os.environ.get(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def _env_list(name):
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-m5-_1hqp!vpucf8lxdz$k8)y!_o5w-wcu^cx^55b+75rcn03_6",
)

# DEBUG defaults to True for `manage.py runserver` ergonomics. Compose / prod
# must set DJANGO_DEBUG=0 (the bundled docker-compose.yml already does).
DEBUG = _env_bool("DJANGO_DEBUG", default=True)

ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS")
if not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["*"] if DEBUG else ["localhost", "127.0.0.1"]

CSRF_TRUSTED_ORIGINS = _env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

BEHIND_TLS_PROXY = _env_bool("DJANGO_BEHIND_PROXY", default=False)
if BEHIND_TLS_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = BEHIND_TLS_PROXY
CSRF_COOKIE_SECURE = BEHIND_TLS_PROXY

SANTA_SYNC_TOKEN = os.environ.get("SANTA_SYNC_TOKEN", "")
SANTA_DEFAULT_BATCH_SIZE = int(os.environ.get("SANTA_DEFAULT_BATCH_SIZE", "50"))
SANTA_DEFAULT_CLIENT_MODE = os.environ.get("SANTA_DEFAULT_CLIENT_MODE", "MONITOR")
SANTA_FULL_SYNC_INTERVAL_SECONDS = int(os.environ.get("SANTA_FULL_SYNC_INTERVAL_SECONDS", "600"))
SANTA_ENABLE_BUNDLES = True
SANTA_ENABLE_TRANSITIVE = False

LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "app.apps.AppConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "elf.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
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

WSGI_APPLICATION = "elf.wsgi.application"


_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if _DATABASE_URL:
    import dj_database_url

    DATABASES = {
        "default": dj_database_url.parse(
            _DATABASE_URL, conn_max_age=600, conn_health_checks=True
        )
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
# Display timezone — storage stays UTC because USE_TZ=True. Override with DJANGO_TIME_ZONE
# if a different default is needed.
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "America/New_York")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Dev: serve static files straight from app dirs. Prod: hashed + compressed manifest
# built by `collectstatic` and served by whitenoise.
_staticfiles_backend = (
    "django.contrib.staticfiles.storage.StaticFilesStorage"
    if DEBUG
    else "whitenoise.storage.CompressedManifestStaticFilesStorage"
)
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": _staticfiles_backend},
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "santa.sync": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
