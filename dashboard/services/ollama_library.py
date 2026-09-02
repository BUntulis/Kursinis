"""Browse the public Ollama model library (ollama.com) — list, detail, tags.

There is no official JSON API for the library, so this scrapes the HTML. The pages carry
stable ``x-test-*`` marker attributes (used by Ollama's own test-suite), which we prefer
over styling classes; every selector still degrades gracefully to an empty result rather
than raising, and results are cached on disk (TTL) so the Models page stays fast and keeps
working offline after the first successful fetch.

Scraped, untrusted README HTML is sanitised through a strict tag/attribute whitelist before
it is ever handed to the frontend (which inserts it with innerHTML).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from django.conf import settings as dj_settings

BASE_URL = "https://ollama.com"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Kursinis dashboard; model browser)"}
_TIMEOUT = 20
_CACHE_TTL_SEC = 24 * 3600

#: Library model names are single path segments like ``llama3.1`` / ``qwen2.5-coder``. Must START
#: with an alphanumeric so pure-punctuation segments (``.``/``..``) can't be treated as a model and
#: fed to the scraper URL (every real Ollama model name begins with a letter or digit).
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")
#: A full pullable tag: ``name`` or ``name:variant`` (same leading-alphanumeric rule).
MODEL_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(:[A-Za-z0-9._\-]+)?$")

_SIZE_RE = re.compile(r"\b(\d+(?:\.\d+)?\s?[GMK]B)\b")
_CONTEXT_RE = re.compile(r"\b(\d+(?:\.\d+)?[KM])\s+context", re.IGNORECASE)


def _cache_dir() -> Path:
    d = Path(dj_settings.BASE_DIR) / ".ollama_library_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_read(key: str, max_age: float = _CACHE_TTL_SEC):
    f = _cache_dir() / f"{key}.json"
    try:
        if f.exists() and (time.time() - f.stat().st_mtime) < max_age:
            return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return None


def _cache_write(key: str, data) -> None:
    try:
        (_cache_dir() / f"{key}.json").write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _get(path: str) -> str:
    resp = requests.get(urljoin(BASE_URL, path), headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------- HTML sanitiser
_ALLOWED_TAGS = {
    "p", "div", "span", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "pre", "code", "strong", "b", "em", "i", "a", "img",
    "table", "thead", "tbody", "tr", "th", "td", "blockquote",
}
_ALLOWED_ATTRS = {"a": {"href"}, "img": {"src", "alt"}}


def sanitize_html(html: str) -> str:
    """Reduce untrusted HTML to a strict whitelist (tags + attributes, http(s) URLs only).

    The output is rendered with innerHTML on the Models page, so anything not explicitly
    allowed is removed: scripts/styles/iframes are dropped with their content, unknown tags
    are unwrapped (text kept), URLs must be http(s) (relative ones resolve against ollama.com).
    """
    soup = BeautifulSoup(html or "", "lxml")
    for el in soup(["script", "style", "iframe", "object", "embed", "noscript", "form", "input", "button"]):
        el.decompose()
    for el in list(soup.find_all(True)):
        if el.name not in _ALLOWED_TAGS:
            el.unwrap()
            continue
        allowed = _ALLOWED_ATTRS.get(el.name, set())
        for attr in list(el.attrs):
            if attr not in allowed:
                del el.attrs[attr]
        if el.name == "a" and el.get("href"):
            href = urljoin(BASE_URL, el["href"].strip())
            if not href.startswith(("http://", "https://")):
                del el.attrs["href"]
            else:
                el.attrs["href"] = href
                el.attrs["target"] = "_blank"
                el.attrs["rel"] = "noopener noreferrer"
        if el.name == "img":
            src = urljoin(BASE_URL, (el.get("src") or "").strip())
            if not src.startswith(("http://", "https://")):
                el.decompose()
                continue
            el.attrs["src"] = src
            el.attrs["loading"] = "lazy"
    body = soup.body or soup
    return "".join(str(c) for c in body.children).strip()


# ---------------------------------------------------------------- parsers (pure: html -> data)
def parse_library(html: str) -> list[dict]:
    """The /library index: one entry per model card."""
    soup = BeautifulSoup(html, "lxml")
    models = []
    for li in soup.select("li[x-test-model]"):
        link = li.select_one("a[href^='/library/']")
        if not link:
            continue
        name = link["href"].rsplit("/", 1)[-1].strip()
        if not MODEL_NAME_RE.match(name):
            continue
        desc_el = li.select_one("p")
        pulls_el = li.select_one("[x-test-pull-count]")
        updated_el = li.select_one("[x-test-updated]")
        tags_el = li.select_one("[x-test-tag-count]")
        models.append({
            "name": name,
            "description": desc_el.get_text(strip=True) if desc_el else "",
            "capabilities": [c.get_text(strip=True) for c in li.select("[x-test-capability]")],
            "sizes": [s.get_text(strip=True) for s in li.select("[x-test-size]")],
            "pulls": pulls_el.get_text(strip=True) if pulls_el else "",
            "updated": updated_el.get_text(strip=True) if updated_el else "",
            "tag_count": tags_el.get_text(strip=True) if tags_el else "",
        })
    return models


def parse_detail(html: str, name: str) -> dict:
    """A /library/<name> page: sanitised README (with images) + summary meta."""
    soup = BeautifulSoup(html, "lxml")
    readme = soup.select_one("#readme") or soup.select_one("#display")
    readme_html = sanitize_html(str(readme)) if readme else ""
    images = []
    if readme:
        for img in readme.select("img"):
            src = urljoin(BASE_URL, (img.get("src") or "").strip())
            if src.startswith(("http://", "https://")) and src not in images:
                images.append(src)
    # the short blurb lives in the meta description (the visible summary markup shifts more often)
    summary = soup.select_one("#summary") or soup
    meta = soup.select_one('meta[name="description"]') or soup.select_one('meta[property="og:description"]')
    description = (meta.get("content") or "").strip() if meta else ""
    if not description:
        desc_el = summary.select_one("h2 ~ p") or summary.select_one("p")
        description = desc_el.get_text(strip=True) if desc_el else ""
    return {
        "name": name,
        "description": description,
        "capabilities": [c.get_text(strip=True) for c in summary.select("[x-test-capability]")][:6],
        "readme_html": readme_html,
        "images": images,
    }


def parse_tags(html: str, name: str) -> list[dict]:
    """A /library/<name>/tags page: every pullable variant with its size/context."""
    soup = BeautifulSoup(html, "lxml")
    seen: dict[str, dict] = {}
    for a in soup.select(f"a[href*='/library/{name}:']"):
        tag = a["href"].rsplit("/", 1)[-1].strip()
        if not MODEL_TAG_RE.match(tag):
            continue
        # each tag appears in desktop + mobile markup; keep the row with the richest text
        row = a.find_parent("div")
        text = " ".join((row or a).get_text(" ", strip=True).split())
        size_m = _SIZE_RE.search(text)
        ctx_m = _CONTEXT_RE.search(text)
        entry = {
            "tag": tag,
            "size": size_m.group(1) if size_m else "",
            "context": (ctx_m.group(1) + " context") if ctx_m else "",
        }
        cur = seen.get(tag)
        if not cur or (entry["size"] and not cur["size"]):
            seen[tag] = entry
    # ``latest`` first, then by tag name for a stable picker
    return sorted(seen.values(), key=lambda e: (e["tag"] != "latest" and not e["tag"].endswith(":latest"), e["tag"]))


# ---------------------------------------------------------------- public API (network + cache)
def list_library(refresh: bool = False) -> dict:
    """All library models. Returns ``{"models": [...], "error": str|None, "cached_at": ts|None}``."""
    if not refresh:
        cached = _cache_read("library")
        if cached:
            return {"models": cached, "error": None}
    try:
        models = parse_library(_get("/library"))
    except Exception as exc:
        stale = _cache_read("library", max_age=365 * 24 * 3600)  # any age beats nothing
        return {"models": stale or [], "error": f"Could not reach ollama.com: {exc}"}
    if models:
        _cache_write("library", models)
        return {"models": models, "error": None}
    stale = _cache_read("library", max_age=365 * 24 * 3600)
    return {"models": stale or [], "error": "ollama.com returned an unexpected page (markup changed?)"}


def model_detail(name: str, refresh: bool = False) -> dict:
    """Full detail for one model: README + images + pullable tags. ``{"detail":…, "error":…}``."""
    if not MODEL_NAME_RE.match(name or ""):
        return {"detail": None, "error": "Invalid model name."}
    key = f"detail_{name}"
    if not refresh:
        cached = _cache_read(key)
        if cached:
            return {"detail": cached, "error": None}
    try:
        detail = parse_detail(_get(f"/library/{quote(name)}"), name)
        detail["tags"] = parse_tags(_get(f"/library/{quote(name)}/tags"), name)
    except Exception as exc:
        stale = _cache_read(key, max_age=365 * 24 * 3600)
        return {"detail": stale, "error": f"Could not reach ollama.com: {exc}"}
    _cache_write(key, detail)
    return {"detail": detail, "error": None}
