"""ASGI entrypoint Django web projektui."""
from __future__ import annotations

import os

from django.core.asgi import get_asgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "webapp.settings")

application = get_asgi_application()
