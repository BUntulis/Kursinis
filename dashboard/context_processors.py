"""Template context processors for the dashboard shell (sidebar "Recent" list)."""
from __future__ import annotations

from django.http import HttpRequest
from django.urls import reverse

#: How many chats the sidebar "Recent" list shows.
_RECENT_LIMIT = 12
#: How many rows to scan before the (Python-side) prompt-writer filter — a small buffer so the cap is
#: still reached after dropping any docked prompt-writer chats.
_RECENT_SCAN = 40


def recent_chats(request: HttpRequest) -> dict:
    """Inject the sidebar's recent-chats list + move targets into every rendered template.

    Recent is chat history, so the list is only built for a signed-in visitor — anonymous pages get an
    empty list and the sidebar block disappears (``nav_active_loose`` is still computed: it only drives
    nav highlighting on a chat page, which anonymous visitors can open). The prompt-writer ``mode``
    filter is applied in Python (not an ORM JSON lookup) so we don't depend on the SQLite JSON1
    extension — mirroring ``dashboard.views`` elsewhere. The active chat is marked only on the
    project-chat page (its ``<pk>`` IS a chat id).
    """
    from .models import Chat, Project

    signed_in = bool(getattr(getattr(request, "user", None), "is_authenticated", False))

    try:
        rows = [] if not signed_in else list(
            Chat.objects.select_related("project").order_by("-updated_at")[:_RECENT_SCAN]
        )

        match = getattr(request, "resolver_match", None)
        active_id = None
        nav_active_loose = False
        if match is not None and match.url_name == "dashboard-project-chat":
            active_id = match.kwargs.get("pk")
            # The "Projects" sidebar item shouldn't light up on a standalone (loose) chat — a loose chat
            # isn't a project. Look up the open chat's project flag (cheap, one row).
            active = next((c for c in rows if c.id == active_id), None)
            if active is None:
                active = Chat.objects.select_related("project").filter(id=active_id).first()
            nav_active_loose = bool(active and active.project and active.project.is_loose)
    except Exception:
        # Never let the sidebar break a page render (e.g. during migrations / a broken DB).
        return {"recent_chats": [], "move_targets": [], "nav_active_loose": False}

    if not signed_in:
        return {"recent_chats": [], "move_targets": [], "nav_active_loose": nav_active_loose}

    recent = []
    for c in rows:
        if (c.settings or {}).get("mode") == "prompt_writer":
            continue
        loose = bool(c.project and c.project.is_loose)
        recent.append({
            "id": c.id,
            "title": c.title or "New chat",
            "url": reverse("dashboard-project-chat", args=[c.id]),
            "rename_url": reverse("dashboard-chat-rename", args=[c.id]),
            "delete_url": reverse("dashboard-chat-delete", args=[c.id]),
            "move_url": reverse("dashboard-chat-move", args=[c.id]),
            "is_loose": loose,
            "project_label": "" if loose else (c.project.name if c.project else ""),
            "active": active_id is not None and c.id == active_id,
        })
        if len(recent) >= _RECENT_LIMIT:
            break

    move_targets = list(
        Project.objects.filter(is_loose=False).order_by("name").values("id", "name")
    )
    return {"recent_chats": recent, "move_targets": move_targets, "nav_active_loose": nav_active_loose}
