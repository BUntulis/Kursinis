"""Auto-imported (via PYTHONPATH) into every Kursinis PREVIEW server process.

The dashboard embeds the generated app in an <iframe>. Django's default
``XFrameOptionsMiddleware`` (which ``startproject`` installs) answers with
``X-Frame-Options: DENY``, so the browser refuses to render the page in the
Preview panel ("127.0.0.1 refused to connect"). This shim no-ops that
middleware for the sandboxed dev preview only — it never touches the user's
project code, and static (http.server) previews simply skip it.
"""
try:
    from django.middleware.clickjacking import XFrameOptionsMiddleware

    XFrameOptionsMiddleware.process_response = staticmethod(  # type: ignore[assignment]
        lambda request, response: response
    )
except Exception:  # Django absent (static prototype server) or API drift — never break startup
    pass
