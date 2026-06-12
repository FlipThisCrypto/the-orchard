# SPDX-License-Identifier: Apache-2.0
"""Orchard View — Flask application factory.

Run with:
    python -m dashboard.app
or
    flask --app dashboard.app.main:create_app run --host 127.0.0.1 --port 5000
"""
from __future__ import annotations

from flask import Flask

from .config import settings
from .routes import api, dashboard, provision, tree


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(provision.bp)
    app.register_blueprint(tree.bp)
    app.register_blueprint(api.bp, url_prefix="/api")

    # Make `settings()` available to every Jinja template as ``settings``
    # — used by base.html to conditionally render the "Plant a Tree"
    # nav link based on public_mode.
    @app.context_processor
    def _inject_settings():
        return {"settings": settings()}

    @app.after_request
    def _security_headers(resp):
        # Clickjacking + sniffing + referrer hardening on every response.
        # frame-ancestors 'none' is the high-value, zero-breakage piece;
        # a strict script-src CSP is intentionally NOT set here because it
        # needs browser verification of the WalletConnect/esm.sh origins
        # (tracked as a follow-up, see docs/SECURITY_AUDIT_2026-06-09.md L1).
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        return resp

    return app


# Module-level instance so `flask --app dashboard.app.main` works.
app = create_app()


def main() -> None:
    s = settings()
    app.run(host=s.host, port=s.port, debug=False)


if __name__ == "__main__":
    main()
