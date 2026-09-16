"""
Sukod — Django settings.

Database: MySQL is the production target (see CLAUDE.md). To keep the project
runnable on a laptop with no MySQL server, we read the engine from environment
variables and fall back to SQLite. The ORM code is identical either way.

Set SUKOD_DB=mysql plus the SUKOD_DB_* variables to switch.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Dev default. Override in production with the SUKOD_SECRET_KEY env var.
SECRET_KEY = os.environ.get("SUKOD_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = os.environ.get("SUKOD_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]

# --- Public tunnel (ngrok) ------------------------------------------------
# A phone cannot install a progressive web app (PWA) from the laptop's
# 127.0.0.1, and a service worker only runs in a "secure context" - HTTPS, or
# localhost. ngrok gives us a real HTTPS address that forwards to this server,
# which satisfies both at once.
#
# Two settings are required for that forwarding to behave:
#
# 1. CSRF_TRUSTED_ORIGINS
#    Cross-Site Request Forgery (CSRF) protection rejects any POST whose
#    Origin header does not match the host Django believes it is serving.
#    Through a tunnel the browser sends Origin: https://xxxx.ngrok-free.app,
#    which Django has never heard of, so every form POST - device pairing,
#    purity check, calibration - would fail with "CSRF verification failed".
#    Listing the ngrok domains as trusted is what lets those forms submit.
#    The leading "*." wildcard is supported by Django 4.0 and later, which
#    matters because the free ngrok subdomain changes on every restart.
#
# 2. SECURE_PROXY_SSL_HEADER
#    ngrok forwards plain HTTP to us, so request.is_secure() would be False
#    and request.build_absolute_uri() - used by the report renderer - would
#    emit http:// links that a browser on an https:// page blocks as mixed
#    content. ngrok sets X-Forwarded-Proto: https on every forwarded request;
#    this setting tells Django to trust that header.
#    Safe while only the tunnel can reach this process. Do NOT keep this on a
#    server exposed directly to the internet, where a client could forge it.
SUKOD_TUNNEL_ORIGIN = os.environ.get("SUKOD_TUNNEL_ORIGIN")

CSRF_TRUSTED_ORIGINS = [
    "https://*.ngrok-free.app",   # free tier, random subdomain
    "https://*.ngrok-free.dev",   # newer free tier domain
    "https://*.ngrok.app",        # paid or reserved domains
    "https://*.ngrok.io",         # legacy, still handed out to old accounts
]
if SUKOD_TUNNEL_ORIGIN:
    # Escape hatch for any other tunnel or a real deployment, e.g.
    # SUKOD_TUNNEL_ORIGIN=https://sukod.example.ph
    CSRF_TRUSTED_ORIGINS.append(SUKOD_TUNNEL_ORIGIN)

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "sukod.urls"

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

WSGI_APPLICATION = "sukod.wsgi.application"

# --- Database -------------------------------------------------------------
if os.environ.get("SUKOD_DB") == "mysql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.environ.get("SUKOD_DB_NAME", "sukod"),
            "USER": os.environ.get("SUKOD_DB_USER", "root"),
            "PASSWORD": os.environ.get("SUKOD_DB_PASSWORD", ""),
            "HOST": os.environ.get("SUKOD_DB_HOST", "127.0.0.1"),
            "PORT": os.environ.get("SUKOD_DB_PORT", "3306"),
            "OPTIONS": {"charset": "utf8mb4"},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "sukod.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-ph"
TIME_ZONE = "Asia/Manila"   # all "today" boundaries are Philippine local time
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- Sign-in routing ------------------------------------------------------
# Django's defaults point at /accounts/login/ and /accounts/profile/, neither
# of which exists here. LOGIN_URL is where an unauthenticated visitor to a
# guarded screen gets sent; LOGIN_REDIRECT_URL is where they land afterwards
# if they were not heading anywhere in particular.
#
# /home/ is not a screen. It is a one-line view that looks at the role and
# forwards: a supervisor to the dashboard, an admin to the back room. Doing it
# in a view rather than a fixed setting is what lets the two roles have
# genuinely different starting points.
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/home/"
LOGOUT_REDIRECT_URL = "/login/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The logger posts entries from a paired device, not a logged-in user, so the
# sync endpoint is CSRF-exempt and authenticated by device token instead.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
}
