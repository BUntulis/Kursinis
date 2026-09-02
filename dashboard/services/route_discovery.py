"""Discover the generated app's URL routes — the pages the Preview panel can open.

The Preview iframe used to always load ``/``; a generated project whose root URLconf maps
nothing there then shows Django's default "install worked" rocket page, which tells the user
nothing about what was actually built. This service asks the SANDBOX project itself (its own
venv + settings) which concrete routes exist, so:

  * the Preview panel can render a clickable pages bar and open a real page by default,
  * the finish card can tell the user which URLs their app answers on.

Results are cached in ``<workspace>/.kursinis_routes.json`` (a dot-file, so it never shows in
the Files tab). The cache is refreshed on every preview (re)start and at turn finish.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

#: Cache file written into the sandbox workspace.
ROUTES_CACHE = ".kursinis_routes.json"

#: Introspection script run WITH THE SANDBOX'S OWN python/settings (never the dashboard's):
#: walks the resolver tree and prints every leaf route as JSON.
_INTROSPECT = r"""
import json, os, re, sys
from pathlib import Path
manage = Path("manage.py").read_text(encoding="utf-8", errors="replace")
m = re.search(r'DJANGO_SETTINGS_MODULE"?\s*,\s*"([^"]+)"', manage)
os.environ["DJANGO_SETTINGS_MODULE"] = m.group(1) if m else "settings"
sys.path.insert(0, os.getcwd())
import django
django.setup()
from django.urls import get_resolver
out = []
def walk(patterns, prefix):
    for p in patterns:
        pat = str(getattr(p, "pattern", ""))
        if hasattr(p, "url_patterns"):
            try:
                walk(p.url_patterns, prefix + pat)
            except Exception:
                pass
        else:
            out.append({"path": "/" + prefix + pat, "name": getattr(p, "name", "") or ""})
walk(get_resolver().url_patterns, "")
print(json.dumps(out))
"""

_MAX_ROUTES = 12


def discover_routes(workspace, timeout: int = 25, static: bool | None = None) -> list[dict]:
    """The sandbox's concrete, openable app routes: ``[{"path": "/notes/", "name": "note_list"}]``.

    ``static=None`` (default) decides by manage.py presence: Django project → real URLconf
    introspection in the sandbox venv; otherwise → the prototype's HTML pages. Pass
    ``static=True`` when a static server serves the sandbox (the mixed prototype+scaffold state —
    manage.py exists but the prototype is what's on screen); ``static=False`` forces Django
    introspection ("does the app wire ANY page?"). Empty when nothing is reachable/runnable.
    """
    ws = Path(workspace)
    if static is True or (static is None and not (ws / "manage.py").exists()):
        return _static_pages(ws)
    if not (ws / "manage.py").exists():
        return []  # forced Django introspection without a project — nothing is wired

    from .project_venv import venv_python

    python_exec = venv_python(ws) or sys.executable
    env = dict(os.environ)
    for var in ("DJANGO_SETTINGS_MODULE", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(var, None)
    try:
        proc = subprocess.run(
            [python_exec, "-c", _INTROSPECT], cwd=str(ws), env=env,
            capture_output=True, text=True, timeout=timeout,
        )
        raw = json.loads(proc.stdout.strip() or "[]") if proc.returncode == 0 else []
    except Exception:
        raw = []

    routes, seen = [], set()
    for r in raw:
        path = re.sub(r"[\^\$]", "", str(r.get("path") or ""))
        if not path.startswith("/"):
            path = "/" + path
        # Only pages a browser can open directly: skip parameterized/regex routes and
        # framework endpoints (admin/static) — they are not "what the model built".
        if "<" in path or "(" in path or "?" in path:
            continue
        if path.startswith(("/admin", "/static", "/media", "/__")):
            continue
        if path in seen:
            continue
        seen.add(path)
        routes.append({"path": path, "name": str(r.get("name") or "")})
        if len(routes) >= _MAX_ROUTES:
            break
    return routes


#: Vendored/infra dirs that are never part of the user's prototype.
_STATIC_SKIP_DIRS = {".venv", "venv", "env", "node_modules", ".git", "__pycache__",
                     "site-packages", "staticfiles", ".idea", ".vscode"}


def _static_pages(ws: Path) -> list[dict]:
    """Openable pages of a static (pre-Django) prototype: every user-authored .html, RECURSIVE.

    The static preview serves the sandbox root; when the model puts its prototype in a subfolder
    (or names it something other than index.html), loading "/" shows http.server's bare directory
    listing instead of the design. Returning real file paths lets the Preview iframe (and its
    links bar) open the actual prototype page directly."""
    pages = []
    for dirpath, dirnames, filenames in os.walk(ws):
        dirnames[:] = [d for d in dirnames if d not in _STATIC_SKIP_DIRS and not d.startswith(".")]
        for fname in sorted(filenames):
            if fname.startswith(".") or not fname.lower().endswith((".html", ".htm")):
                continue
            rel = (Path(dirpath) / fname).relative_to(ws).as_posix()
            path = "/" if rel.lower() == "index.html" else "/" + rel
            pages.append({"path": path, "name": Path(fname).stem})

    def rank(p: dict):
        path = p["path"]
        is_root = path == "/"
        is_index = is_root or path.lower().endswith(("index.html", "index.htm"))
        return (not is_root, not is_index, path.count("/"), path)

    # Root index first, then any index page (shallowest first), then the rest — so the iframe's
    # default page is always the most "front door" file the prototype has.
    pages.sort(key=rank)
    return pages[:_MAX_ROUTES]


def refresh_routes(workspace, static: bool | None = None) -> list[dict]:
    """Recompute the routes and rewrite the workspace cache; returns the fresh list."""
    routes = discover_routes(workspace, static=static)
    try:
        (Path(workspace) / ROUTES_CACHE).write_text(
            json.dumps(routes, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass
    return routes


def refresh_routes_async(workspace, static: bool | None = None) -> None:
    """Fire-and-forget cache refresh — used right after a preview server spawns, so the
    start action stays snappy; the status polls pick the links up a moment later."""
    threading.Thread(target=refresh_routes, args=(str(workspace),), kwargs={"static": static}, daemon=True).start()


def cached_routes(workspace) -> list[dict]:
    """The last discovered routes (empty when never discovered / unreadable)."""
    try:
        data = json.loads((Path(workspace) / ROUTES_CACHE).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []
