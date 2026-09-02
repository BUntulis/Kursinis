"""Cache-busting static helper.

``{% static_v 'dashboard/app.js' %}`` renders the normal static URL with a ``?v=<mtime>`` suffix
derived from the file's last-modified time. The query changes whenever the file changes, so browsers
(and the dev server's conditional GET) are forced to fetch the new version instead of serving a stale
cached copy — which otherwise leaves a UI bug "fixed" in the code but still broken in the browser.
"""
from __future__ import annotations

import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def static_v(path: str) -> str:
    """Static URL for ``path`` plus a ``?v=<mtime>`` cache-buster (best-effort)."""
    url = static(path)
    try:
        abs_path = finders.find(path)
        if abs_path and os.path.exists(abs_path):
            version = int(os.path.getmtime(abs_path))
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}v={version}"
    except Exception:
        pass
    return url
