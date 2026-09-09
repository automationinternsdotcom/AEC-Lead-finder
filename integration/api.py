"""FastAPI webhook receiver and unsubscribe endpoint."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse

from .config import Settings
from .database import Database
from .security import SignatureError, verify_unsubscribe_token, verify_warmy_signature
from .workflows import SalesWorkflows

AETHER_SIGNATURE_LOGO = (
    Path(__file__).resolve().parent / "assets" / "aether-signature-logo.png"
)


def create_app(settings: Settings | None = None, db=None, workflows=None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = db or Database(settings.database_path)
    workflows = workflows or SalesWorkflows(settings, db)
    app = FastAPI(title="Aether Sales Integration", version="0.1.0")
    app.state.settings = settings
    app.state.db = db
    app.state.workflows = workflows

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        try:
            database_ok = bool(db.healthcheck())
        except (sqlite3.Error, OSError):
            database_ok = False
        if not database_ok:
            raise HTTPException(status_code=503, detail="database unavailable")
        return {
            "ok": True,
            "environment": settings.environment,
            "provider_writes_enabled": settings.provider_writes_enabled,
            "warmy_enrollment_enabled": settings.warmy_enrollment_enabled,
            "campaign_activation_ready": settings.campaign_activation_ready,
            "campaign_enrollment_ready": settings.campaign_enrollment_ready,
        }

    @app.get("/assets/aether-signature-logo.png")
    def aether_signature_logo() -> FileResponse:
        if not AETHER_SIGNATURE_LOGO.exists():
            raise HTTPException(status_code=404, detail="signature logo unavailable")
        return FileResponse(
            AETHER_SIGNATURE_LOGO,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.post("/webhooks/warmy")
    async def warmy_webhook(request: Request) -> dict[str, Any]:
        body = await request.body()
        try:
            verify_warmy_signature(
                body,
                request.headers.get("X-Warmy-Signature", ""),
                request.headers.get("X-Warmy-Timestamp", ""),
                settings.warmy_webhook_secret,
            )
        except SignatureError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=400, detail="invalid JSON") from error
        event_id = request.headers.get("X-Warmy-Event-Id", "")
        event_type = request.headers.get("X-Warmy-Event-Type", "") or str(
            payload.get("type") or payload.get("event") or ""
        )
        if not event_id or not event_type:
            raise HTTPException(status_code=400, detail="missing Warmy event identity")
        accepted = db.accept_webhook(
            "warmy",
            event_id,
            event_type,
            payload,
            hashlib.sha256(body).hexdigest(),
            signature_valid=True,
        )
        return {"accepted": accepted, "duplicate": not accepted}

    @app.post("/webhooks/pipedrive")
    async def pipedrive_webhook(request: Request) -> dict[str, Any]:
        if not _valid_basic_auth(
            request.headers.get("Authorization", ""),
            settings.pipedrive_webhook_user,
            settings.pipedrive_webhook_password,
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
            )
        body = await request.body()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=400, detail="invalid JSON") from error
        meta = payload.get("meta") or {}
        event_type = str(
            payload.get("event")
            or ".".join(
                filter(
                    None, (str(meta.get("action") or ""), str(meta.get("entity") or ""))
                )
            )
            or "pipedrive.change"
        )
        event_id = (
            str(meta.get("id") or meta.get("event_id") or "")
            or hashlib.sha256(body).hexdigest()
        )
        accepted = db.accept_webhook(
            "pipedrive",
            event_id,
            event_type,
            payload,
            hashlib.sha256(body).hexdigest(),
            signature_valid=True,
        )
        return {"accepted": accepted, "duplicate": not accepted}

    def resolve_unsubscribe(token: str) -> str:
        try:
            token_id = verify_unsubscribe_token(token, settings.unsubscribe_secret)
        except SignatureError as error:
            raise HTTPException(
                status_code=404, detail="invalid unsubscribe link"
            ) from error
        email = db.resolve_unsubscribe_token(token_id)
        if email is None:
            raise HTTPException(status_code=404, detail="invalid unsubscribe link")
        return email

    def perform_unsubscribe(token: str) -> HTMLResponse:
        email = resolve_unsubscribe(token)
        workflows.unsubscribe(email)
        return HTMLResponse(
            """<!doctype html><html lang="en"><head><meta charset="utf-8">
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <title>Unsubscribed</title></head><body>
            <main style="max-width:42rem;margin:4rem auto;font:18px system-ui;padding:1rem">
            <h1>You’re unsubscribed</h1>
            <p>This address has been removed from Aether’s commercial outreach.</p>
            </main></body></html>"""
        )

    @app.get("/unsubscribe", response_class=HTMLResponse)
    def unsubscribe(t: str = Query(min_length=34, max_length=256)) -> HTMLResponse:
        resolve_unsubscribe(t)
        escaped_token = t.replace("&", "&amp;").replace('"', "&quot;")
        return HTMLResponse(
            f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <title>Confirm unsubscribe</title></head><body>
            <main style="max-width:42rem;margin:4rem auto;font:18px system-ui;padding:1rem">
            <h1>Unsubscribe from Aether outreach?</h1>
            <p>Confirm below to stop future commercial outreach to this address.</p>
            <form method="post" action="/unsubscribe?t={escaped_token}">
            <button type="submit">Unsubscribe</button></form>
            </main></body></html>"""
        )

    @app.post("/unsubscribe", response_class=HTMLResponse)
    def one_click_unsubscribe(
        t: str = Query(min_length=34, max_length=256),
    ) -> HTMLResponse:
        # RFC 8058 clients submit List-Unsubscribe=One-Click in the body. The
        # signed query token carries the authorization, so the body needn't be stored.
        return perform_unsubscribe(t)

    return app


def _valid_basic_auth(header: str, expected_user: str, expected_password: str) -> bool:
    if not expected_user or not expected_password or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    user, separator, password = decoded.partition(":")
    return (
        bool(separator)
        and hmac.compare_digest(user, expected_user)
        and hmac.compare_digest(password, expected_password)
    )


def _module_app() -> FastAPI:
    settings = Settings.from_env()
    return create_app(settings)


app = _module_app()
