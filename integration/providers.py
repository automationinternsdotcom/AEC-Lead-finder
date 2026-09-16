"""Provider clients with explicit write gates and bounded error handling."""

from __future__ import annotations

import base64
import json
import os
import selectors
import subprocess
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as email_policy
from typing import Any

import httpx
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import ActivationBlocked, Settings
from scout.copy_grammar import review_copy


class ProviderError(RuntimeError):
    def __init__(self, provider: str, status_code: int, code: str, message: str):
        super().__init__(f"{provider} {status_code} {code}: {message}")
        self.provider = provider
        self.status_code = status_code
        self.code = code
        self.retryable = status_code in {408, 409, 425, 429, 500, 502, 503, 504}


class GmailHistoryExpired(RuntimeError):
    """The Gmail history cursor aged out and requires a bounded resync."""


class WarmyMcpClient:
    """Small stdio MCP client for the documented WarmySender launcher.

    ``@warmysender/mcp`` is an official launcher around ``mcp-remote``.  It
    speaks newline-delimited JSON-RPC on stdin/stdout, so the worker can use
    the same authenticated MCP surface as the interactive connector without
    depending on a Codex tool session or browser state.
    """

    PROTOCOL_VERSION = "2025-06-18"
    # Verified official launcher release; avoid silently changing the
    # transport contract on a future npm publish.
    LAUNCHER_PACKAGE = "@warmysender/mcp@1.0.2"
    MAX_PAGES = 1000

    def __init__(self, settings: Settings):
        if not settings.warmy_api_key:
            raise ActivationBlocked("Warmy MCP requires WARMY_API_KEY")
        env = os.environ.copy()
        # The launcher documents this environment variable. Do not put the
        # credential in argv, logs, or a persisted artifact.
        env["WARMYSENDER_API_KEY"] = settings.warmy_api_key
        env["WARMYSENDER_MCP_URL"] = settings.warmy_mcp_url
        try:
            self.process = subprocess.Popen(
                ["npx", "-y", self.LAUNCHER_PACKAGE],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # Keep launcher diagnostics away from the protocol stream and
                # avoid filling an unread stderr pipe during a long run.
                stderr=subprocess.DEVNULL,
                text=False,
                env=env,
                bufsize=0,
            )
        except OSError as error:
            raise ProviderError("warmy-mcp", 503, "mcp_unavailable", "official launcher could not start") from error
        self.timeout = 30.0
        self._selector = selectors.DefaultSelector()
        if self.process.stdout is None or self.process.stdin is None:
            self.close()
            raise ProviderError("warmy-mcp", 503, "mcp_unavailable", "official launcher has no stdio transport")
        self._selector.register(self.process.stdout, selectors.EVENT_READ)
        self._stdout_buffer = bytearray()
        self._next_id = 0
        try:
            self._request(
                "initialize",
                {
                    "protocolVersion": self.PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "aether-background-worker", "version": "1"},
                },
            )
            self._notify("notifications/initialized", {})
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        selector = getattr(self, "_selector", None)
        process = getattr(self, "process", None)
        if selector is not None:
            selector.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise ProviderError("warmy-mcp", 503, "mcp_unavailable", "MCP stdin closed")
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n").encode())
        self.process.stdin.flush()

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.process.stdin is None:
            raise ProviderError("warmy-mcp", 503, "mcp_unavailable", "MCP stdin closed")
        self._next_id += 1
        request_id = self._next_id
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if b"\n" not in self._stdout_buffer and (remaining <= 0 or not self._selector.select(remaining)):
                raise ProviderError("warmy-mcp", 503, "mcp_timeout", f"MCP {method} timed out")
            if b"\n" in self._stdout_buffer:
                line, _, self._stdout_buffer = self._stdout_buffer.partition(b"\n")
            else:
                fd = self.process.stdout.fileno() if self.process.stdout else -1
                chunk = os.read(fd, 4096) if fd >= 0 else b""
                if not chunk:
                    raise ProviderError("warmy-mcp", 503, "mcp_unavailable", f"MCP closed during {method}")
                self._stdout_buffer.extend(chunk)
                if len(self._stdout_buffer) > 1_000_000:
                    raise ProviderError("warmy-mcp", 503, "mcp_invalid_response", f"MCP {method} response exceeded buffer limit")
                continue
            try:
                response = json.loads(line.decode())
            except json.JSONDecodeError:
                # mcp-remote must keep stdout protocol-clean, but ignore an
                # incidental non-JSON line rather than treating it as a reply.
                continue
            if response.get("id") != request_id:
                continue
            if response.get("error"):
                error = response["error"]
                message = str(error.get("message") or "MCP request failed") if isinstance(error, dict) else "MCP request failed"
                raise ProviderError("warmy-mcp", 503, "mcp_tool_error", message)
            result = response.get("result")
            if not isinstance(result, dict):
                raise ProviderError("warmy-mcp", 503, "mcp_invalid_response", f"MCP {method} returned no result")
            return result

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError") is True:
            text = next(
                (item.get("text") for item in result.get("content", [])
                 if isinstance(item, dict) and isinstance(item.get("text"), str)),
                "MCP tool failed",
            )
            raise ProviderError("warmy-mcp", 503, "mcp_tool_error", text)
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for item in result.get("content", []):
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            try:
                value = json.loads(item.get("text", ""))
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                return value
        raise ProviderError("warmy-mcp", 503, "mcp_invalid_response", f"MCP tool {name} returned no object")


def validate_provider_prospect(
    response: dict[str, Any],
    *,
    expected_id: str,
    expected_email: str,
    initial_step: bool,
    expected_list_id: str | None = None,
) -> dict[str, Any]:
    """Validate Warmy's documented prospect read before a literal start.

    Warmy exposes ``globalStatus``, ``suppressionReason``, and
    ``lastRepliedAt`` on the prospect object. Missing status is not evidence of
    sendability, and direct-object and ``data``-wrapped responses are both
    accepted because the connector exposes both shapes.
    """
    prospect = response.get("data", response) if isinstance(response, dict) else None
    if not isinstance(prospect, dict):
        raise ActivationBlocked("provider prospect readback is unavailable")
    if str(prospect.get("id") or "").strip() != expected_id:
        raise ActivationBlocked("provider prospect ID differs from frozen recipient")
    live_email = str(prospect.get("email") or "").strip().casefold()
    if not live_email or live_email != expected_email.strip().casefold():
        raise ActivationBlocked("provider prospect email differs from frozen recipient")
    global_status = str(prospect.get("globalStatus") or "").strip().casefold()
    if not global_status:
        raise ActivationBlocked("provider prospect globalStatus is required")
    if global_status != "active":
        raise ActivationBlocked("provider prospect is not globally active")
    if str(prospect.get("suppressionReason") or "").strip():
        raise ActivationBlocked("provider prospect has a suppression reason")
    if str(prospect.get("lastRepliedAt") or "").strip():
        raise ActivationBlocked("provider prospect has a prior reply")
    if initial_step and any(
        str(prospect.get(field) or "").strip()
        for field in ("lastContactedAt", "last_contacted_at", "lastSentAt", "last_sent_at")
    ):
        raise ActivationBlocked("initial recipient already has provider contact activity; reconcile before retry")
    if expected_list_id:
        memberships = prospect.get("listMemberships")
        if not isinstance(memberships, list) or not any(
            isinstance(item, dict)
            and expected_list_id.strip() in {
                str(item.get("listId") or "").strip(),
                # Warmy prospect-detail responses identify memberships with
                # ``id``; some list endpoints use the older ``listId`` key.
                str(item.get("id") or "").strip(),
            }
            for item in memberships
        ):
            raise ActivationBlocked("provider prospect is not a member of the canonical list")
    return prospect


class WarmyClient:
    MAX_PROSPECT_PAGES = 1000
    def __init__(self, settings: Settings, transport: httpx.Client | None = None):
        self.settings = settings
        self.mcp: WarmyMcpClient | None = None
        self.http = transport or httpx.Client(
            base_url=settings.warmy_base_url + "/",
            timeout=30,
            headers={
                "Authorization": f"Bearer {settings.warmy_api_key}",
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        if self.mcp:
            self.mcp.close()
        self.http.close()

    def _mcp_client(self) -> WarmyMcpClient | None:
        if not self.settings.warmy_mcp_enabled:
            return None
        if self.mcp is None:
            self.mcp = WarmyMcpClient(self.settings)
        return self.mcp

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str = "",
        write: bool = False,
    ) -> dict[str, Any]:
        if write:
            self.settings.require_provider_writes()
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        response = self.http.request(
            method,
            path.lstrip("/"),
            json=payload,
            params=params,
            headers=headers,
        )
        body = response.json() if response.content else {}
        if not 200 <= response.status_code < 300:
            error = body.get("error") or {}
            raise ProviderError(
                "warmy",
                response.status_code,
                str(error.get("code") or "http_error"),
                str(error.get("message") or response.reason_phrase),
            )
        return body

    def create_prospect(
        self,
        contact: dict[str, Any],
        operation_key: str,
        *,
        list_id: str | None = None,
    ) -> dict:
        self.settings.require_provider_writes()
        mcp = self._mcp_client()
        if mcp:
            arguments: dict[str, Any] = {
                "email": contact["email"],
                "first_name": contact.get("first_name", ""),
                "last_name": contact.get("last_name", ""),
                "company": contact.get("organization_name", ""),
                "role": contact.get("title", ""),
                "phone": contact.get("phone", ""),
                "linkedin_url": contact.get("linkedin", ""),
                "enroll": False,
                "custom_fields": self._prospect_custom_fields(contact),
                "idempotency_key": operation_key,
            }
            if list_id:
                arguments["list_id"] = list_id
            return mcp.call_tool("create_prospect", {
                key: value for key, value in arguments.items()
                if value not in ("", None)
            })
        custom_fields = self._prospect_custom_fields(contact)
        payload = {
            "email": contact["email"],
            "firstName": contact.get("first_name", ""),
            "lastName": contact.get("last_name", ""),
            "company": contact.get("organization_name", ""),
            "role": contact.get("title", ""),
            "phone": contact.get("phone", ""),
            "linkedinUrl": contact.get("linkedin", ""),
            "enroll": False,
            "customFields": custom_fields,
        }
        if list_id:
            payload["listId"] = list_id
        payload = {
            key: value for key, value in payload.items() if value not in ("", None)
        }
        return self._request(
            "POST",
            "prospects",
            payload=payload,
            idempotency_key=operation_key,
            write=True,
        )

    def append_prospect_to_list(
        self,
        contact: dict[str, Any],
        list_id: str,
        operation_key: str,
    ) -> dict:
        """Idempotently upsert a prospect and attach it to one known list.

        Warmy's documented ``POST /prospects`` operation reuses an existing
        prospect by email and accepts ``listId``.  ``enroll:false`` remains in
        the payload so this operation only changes list membership; it never
        directly enrolls or starts a campaign.  Require the documented
        attachment acknowledgement instead of claiming success from an ID
        alone.
        """
        normalized_list_id = str(list_id or "").strip()
        if not normalized_list_id:
            raise ActivationBlocked("Warmy canonical prospect list ID is required")
        self.settings.require_provider_writes()
        mcp = self._mcp_client()
        if mcp:
            # This is intentionally a membership-only upsert. A follow-up
            # update_prospect call carries the complete custom-field snapshot
            # only when the workflow has new content to sync.
            response = mcp.call_tool(
                "create_prospect",
                {
                    "email": contact["email"],
                    "list_id": normalized_list_id,
                    "enroll": False,
                    "idempotency_key": operation_key,
                },
            )
        else:
            # REST fallback is also membership-only. Never reuse the full
            # prospect create payload here: that can overwrite existing
            # provider fields while all we need is list attachment.
            response = self._request(
                "POST",
                "prospects",
                payload={
                    "email": contact["email"],
                    "listId": normalized_list_id,
                    "enroll": False,
                },
                idempotency_key=operation_key,
                write=True,
            )
        data = response.get("data", response) if isinstance(response, dict) else None
        # The documented response places list/listId at the root. Accept a
        # nested shape only as a compatibility fallback, never as inference.
        attached = response.get("list") if isinstance(response, dict) else None
        if attached is None and isinstance(data, dict):
            attached = data.get("list")
        mcp_membership_ack = isinstance(data, dict) and any(
            isinstance(item, dict)
            and normalized_list_id in {
                str(item.get("id") or "").strip(),
                str(item.get("listId") or "").strip(),
            }
            for item in (data.get("listMemberships") or [])
        )
        if (not isinstance(attached, dict) or attached.get("attached") is not True) and not mcp_membership_ack:
            raise ActivationBlocked("Warmy list append lacks an explicit attachment acknowledgement")
        echoed_list_id = response.get("listId") if isinstance(response, dict) else None
        if echoed_list_id is None and isinstance(data, dict):
            echoed_list_id = data.get("listId")
        if echoed_list_id is not None and echoed_list_id != normalized_list_id:
            raise ActivationBlocked(
                f"Warmy list acknowledgement mismatch: expected {normalized_list_id}, "
                f"got {echoed_list_id or '<missing>'}"
            )
        return response

    def find_prospect_by_email(self, email: str) -> dict[str, Any] | None:
        normalized = email.strip().casefold()
        mcp = self._mcp_client()
        if mcp:
            cursor: str | None = None
            seen_cursors: set[str] = set()
            for page_number in range(1, self.MAX_PROSPECT_PAGES + 1):
                arguments: dict[str, Any] = {
                    "filters": [{"field": "email", "op": "eq", "value": normalized}],
                    "limit": 100,
                }
                if cursor:
                    arguments["cursor"] = cursor
                response = mcp.call_tool("list_prospects", arguments)
                rows, pagination = self._prospect_page(response, source="MCP")
                match = next(
                    (row for row in rows
                     if str(row.get("email") or "").strip().casefold() == normalized),
                    None,
                )
                if match is not None:
                    return match
                has_more = pagination["has_more"]
                next_cursor = pagination.get("next_cursor", pagination.get("nextCursor"))
                next_cursor = str(next_cursor or "").strip()
                if not has_more:
                    return None
                if not next_cursor or next_cursor in seen_cursors:
                    raise ActivationBlocked("Warmy MCP email lookup pagination repeated or omitted its cursor; hold, do not create")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            raise ActivationBlocked("Warmy MCP email lookup exceeded its maximum page guard; hold, do not create")
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for page_number in range(1, self.MAX_PROSPECT_PAGES + 1):
            params: dict[str, Any] = {"email": normalized, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = self._request("GET", "prospects", params=params)
            rows, pagination = self._prospect_page(response, source="REST")
            match = next(
                (row for row in rows
                 if str(row.get("email") or "").strip().casefold() == normalized),
                None,
            )
            if match is not None:
                return match
            if not pagination["has_more"]:
                return None
            next_cursor = str(pagination["next_cursor"] or "").strip()
            if next_cursor in seen_cursors:
                raise ActivationBlocked("Warmy REST email lookup pagination repeated its cursor; hold, do not create")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise ActivationBlocked("Warmy REST email lookup exceeded its maximum page guard; hold, do not create")

    @staticmethod
    def _prospect_page(
        response: dict[str, Any], *, source: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Normalize and validate a documented cursor-paginated page.

        Missing pagination is not evidence that a searched email is absent;
        fail closed so callers hold the contact instead of creating a duplicate.
        """
        if not isinstance(response, dict):
            raise ActivationBlocked(f"Warmy {source} prospect lookup returned an unrecognized page; hold, do not create")
        data: Any = response.get("data", response) if isinstance(response, dict) else response
        pagination: Any = response.get("pagination", {}) if isinstance(response, dict) else {}
        if isinstance(data, dict):
            pagination = data.get("pagination", pagination)
            rows = next((data[key] for key in ("prospects", "items", "results", "data")
                         if isinstance(data.get(key), list)), None)
        else:
            rows = data if isinstance(data, list) else []
        if rows is None or not isinstance(pagination, dict):
            raise ActivationBlocked(f"Warmy {source} prospect lookup returned an unrecognized page; hold, do not create")
        has_more = pagination.get("has_more", pagination.get("hasMore"))
        if not isinstance(has_more, bool):
            raise ActivationBlocked(f"Warmy {source} prospect lookup omitted valid pagination; hold, do not create")
        next_cursor = pagination.get("next_cursor", pagination.get("nextCursor"))
        if has_more and not str(next_cursor or "").strip():
            raise ActivationBlocked(f"Warmy {source} prospect lookup omitted its next cursor; hold, do not create")
        pagination = {"has_more": has_more, "next_cursor": next_cursor}
        return ([row for row in rows if isinstance(row, dict)], pagination)

    @staticmethod
    def _prospect_custom_fields(contact: dict[str, Any]) -> dict[str, Any]:
        existing = contact.get("_existing_custom_fields")
        values = {
            "aetherLeadEventId": contact.get("lead_event_id", ""),
            "aetherOutreachId": contact.get("outreach_id", ""),
            "aetherContactCandidateId": contact.get(
                "source_contact_candidate_id", ""
            ),
            "sourceArticle": contact.get("article_url", ""),
            "unsubscribeUrl": contact.get("unsubscribe_url", ""),
            "whyLine": review_copy(contact.get("why_line", "")),
            "projectPropertyName": contact.get("project_property_name", ""),
        }
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update({key: value for key, value in values.items() if value not in ("", None)})
        return merged

    def update_prospect(
        self, prospect_id: str, contact: dict[str, Any], operation_key: str
    ) -> dict:
        """Sync a complete contact snapshot, not an individual custom-field patch.

        Warmy replaces the entire customFields object on PATCH. Callers making
        targeted corrections must preserve the existing full field dictionary.
        """
        self.settings.require_provider_writes()
        mcp = self._mcp_client()
        if mcp:
            arguments: dict[str, Any] = {
                "prospect_id": prospect_id,
                "first_name": contact.get("first_name", ""),
                "last_name": contact.get("last_name", ""),
                "company": contact.get("organization_name", ""),
                "role": contact.get("title", ""),
                "phone": contact.get("phone", ""),
                "linkedin_url": contact.get("linkedin") or None,
                "custom_fields": self._prospect_custom_fields(contact),
                "idempotency_key": operation_key,
            }
            return mcp.call_tool("update_prospect", {
                key: value for key, value in arguments.items()
                if value not in ("", None)
            })
        payload = {
            "firstName": contact.get("first_name", ""),
            "lastName": contact.get("last_name", ""),
            "company": contact.get("organization_name", ""),
            "role": contact.get("title", ""),
            "phone": contact.get("phone", ""),
            "linkedinUrl": contact.get("linkedin") or None,
            "customFields": self._prospect_custom_fields(contact),
        }
        payload = {
            key: value
            for key, value in payload.items()
            if value not in ("", None)
        }
        return self._request(
            "PATCH",
            f"prospects/{prospect_id}",
            payload=payload,
            idempotency_key=operation_key,
            write=True,
        )

    def verify_email(self, email: str, operation_key: str) -> dict:
        self.settings.require_provider_writes()
        from .database import Database
        delay = Database(self.settings.database_path).reserve_provider_slot("warmy.verification", 8.0)
        if delay:
            time.sleep(delay)
        return self._request(
            "POST",
            "verification/verify",
            payload={"email": email},
            idempotency_key=operation_key,
            write=True,
        )

    def create_verification_batch(
        self, emails: list[str], name: str, operation_key: str
    ) -> dict:
        return self._request(
            "POST",
            "verification/batches",
            payload={"emails": emails, "name": name},
            idempotency_key=operation_key,
            write=True,
        )

    def get_verification_batch(self, batch_id: str) -> dict:
        return self._request("GET", f"verification/batches/{batch_id}")

    def get_verification_results(self, batch_id: str) -> dict:
        return self._request("GET", f"verification/batches/{batch_id}/results")

    def get_campaign(self, campaign_id: str) -> dict:
        return self._request("GET", f"campaigns/{campaign_id}")

    def get_prospect(self, prospect_id: str) -> dict:
        """Read the provider prospect immediately before activation."""
        mcp = self._mcp_client()
        if mcp:
            return mcp.call_tool("get_prospect", {"prospect_id": prospect_id})
        # Warmy does not document/support GET /prospects/{id} on the REST API;
        # never issue that known-invalid request as a pretend fallback.
        raise ActivationBlocked("Warmy prospect detail requires the documented MCP get_prospect tool")

    def enroll(
        self, campaign_id: str, prospect_ids: list[str], operation_key: str
    ) -> dict:
        if not self.settings.warmy_enrollment_enabled:
            raise ActivationBlocked("Warmy enrollment is disabled")
        campaign_response = self.get_campaign(campaign_id)
        campaign = campaign_response.get("data", campaign_response) if isinstance(campaign_response, dict) else None
        if not isinstance(campaign, dict) or str(campaign.get("status") or "").casefold() != "draft":
            raise ActivationBlocked("Warmy enrollment is permitted only for a draft campaign")
        return self._request(
            "POST",
            f"campaigns/{campaign_id}/enrollments",
            payload={"prospectIds": prospect_ids},
            idempotency_key=operation_key,
            write=True,
        )

    def unenroll(self, campaign_id: str, emails: list[str], operation_key: str) -> dict:
        return self._request(
            "DELETE",
            f"campaigns/{campaign_id}/enrollments",
            payload={"emails": emails},
            idempotency_key=operation_key,
            write=True,
        )

    def suppress(self, emails: list[str], reason: str, operation_key: str) -> dict:
        return self._request(
            "POST",
            "prospects/suppress",
            payload={"emails": emails, "reason": reason},
            idempotency_key=operation_key,
            write=True,
        )

    def create_campaign(self, manifest: dict[str, Any], operation_key: str) -> dict:
        return self._request(
            "POST",
            "campaigns",
            payload=manifest,
            idempotency_key=operation_key,
            write=True,
        )

    def update_campaign(
        self, campaign_id: str, manifest: dict[str, Any], operation_key: str
    ) -> dict:
        return self._request(
            "PATCH",
            f"campaigns/{campaign_id}",
            payload=manifest,
            idempotency_key=operation_key,
            write=True,
        )

    def start_campaign(self, campaign_id: str, operation_key: str) -> dict:
        self.settings.require_campaign_activation()
        from .database import Database
        db = Database(self.settings.database_path)
        db.valid_frozen_send_approval(
            campaign_id,
            self.settings.warmy_campaign_manifest_hash,
            require_future=True,
        )
        from .send_gate import validate_live_literal_campaign
        approval_state = db.get_state(f"send-approval:{campaign_id}") or {}
        manifest = approval_state.get("manifest") or {}
        messages = manifest.get("messages") or []
        if len(messages) != 1:
            raise ActivationBlocked(
                "live activation requires a dedicated one-recipient one-step frozen campaign"
            )
        current = self.get_campaign(campaign_id)
        campaign = current.get("data") if isinstance(current, dict) else current
        if not isinstance(campaign, dict):
            campaign = current
        if str(campaign.get("status") or "").casefold() not in {"draft", "paused"}:
            raise ActivationBlocked(
                "campaign must be draft or paused immediately before activation"
            )
        # Warmy API readback omits exact campaign membership/mailboxes.  The
        # operator must first record a fresh, hashed UI read for those fields;
        # an unfiltered prospect listing is not acceptable activation evidence.
        live_read_evidence = db.get_state(f"send-live-read:{campaign_id}")
        provider_prospect_id = str(messages[0].get("provider_prospect_id") or "").strip()
        if not provider_prospect_id:
            raise ActivationBlocked("frozen recipient lacks a provider prospect ID")
        prospect_response = self.get_prospect(provider_prospect_id)
        validate_provider_prospect(
            prospect_response,
            expected_id=provider_prospect_id,
            expected_email=str(messages[0].get("recipient_email") or ""),
            initial_step=int(messages[0].get("step_index") or 0) == 0,
            expected_list_id=self.settings.warmy_prospect_list_id,
        )
        validate_live_literal_campaign(
            current,
            messages[0],
            expected_campaign_id=campaign_id,
            expected_mailbox_ids=set(self.settings.warmy_mailbox_ids),
            live_read_evidence=live_read_evidence,
        )
        db.reserve_frozen_sends(campaign_id, manifest)
        return self._request(
            "POST",
            f"campaigns/{campaign_id}/start",
            idempotency_key=operation_key,
            write=True,
        )

    def pause_campaign(self, campaign_id: str, operation_key: str) -> dict:
        return self._request(
            "POST",
            f"campaigns/{campaign_id}/pause",
            idempotency_key=operation_key,
            write=True,
        )

    def create_webhook(self, url: str, events: list[str], operation_key: str) -> dict:
        return self._request(
            "POST",
            "webhooks",
            payload={"url": url, "events": events},
            idempotency_key=operation_key,
            write=True,
        )

    def list_webhooks(self) -> list[dict]:
        response = self._request("GET", "webhooks")
        data = response.get("data") if isinstance(response, dict) else response
        return data if isinstance(data, list) else []

    def list_mailboxes(self) -> dict:
        return self._request("GET", "mailboxes")


class PipedriveClient:
    def __init__(self, settings: Settings, transport: httpx.Client | None = None):
        self.settings = settings
        base_url = f"https://{settings.pipedrive_domain}.pipedrive.com/api/"
        self.http = transport or httpx.Client(
            base_url=base_url,
            timeout=30,
            params={"api_token": settings.pipedrive_api_token},
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self.http.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        write: bool = False,
    ) -> Any:
        if write:
            self.settings.require_provider_writes()
        response = self.http.request(
            method, path.lstrip("/"), json=payload, params=params
        )
        body = response.json() if response.content else {}
        if not 200 <= response.status_code < 300 or body.get("success") is False:
            raise ProviderError(
                "pipedrive",
                response.status_code,
                str(body.get("error") or "http_error"),
                str(
                    body.get("error_info")
                    or body.get("error")
                    or response.reason_phrase
                ),
            )
        return body.get("data")

    @staticmethod
    def _search_id(data: Any) -> int | str | None:
        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            return None
        item = items[0].get("item") or items[0]
        return item.get("id")

    def find_organization(self, name: str) -> int | None:
        data = self._request(
            "GET",
            "v2/organizations/search",
            params={"term": name, "fields": "name", "exact_match": "true", "limit": 1},
        )
        value = self._search_id(data)
        return int(value) if value is not None else None

    def create_organization(self, name: str, owner_id: int, location: str = "") -> int:
        payload: dict[str, Any] = {"name": name, "owner_id": owner_id}
        if location:
            payload["address"] = {"value": location}
        data = self._request("POST", "v2/organizations", payload=payload, write=True)
        return int(data["id"])

    def find_person(self, email: str) -> int | None:
        data = self._request(
            "GET",
            "v2/persons/search",
            params={
                "term": email,
                "fields": "email",
                "exact_match": "true",
                "limit": 1,
            },
        )
        value = self._search_id(data)
        return int(value) if value is not None else None

    def create_person(
        self,
        name: str,
        email: str,
        organization_id: int,
        owner_id: int,
        custom_fields: dict[str, Any] | None = None,
    ) -> int:
        data = self._request(
            "POST",
            "v2/persons",
            payload={
                "name": name,
                "owner_id": owner_id,
                "org_id": organization_id,
                "emails": [{"value": email, "primary": True, "label": "work"}],
                "custom_fields": custom_fields or {},
            },
            write=True,
        )
        return int(data["id"])

    def update_person(self, person_id: int, custom_fields: dict[str, Any]) -> dict:
        return self._request(
            "PATCH",
            f"v2/persons/{person_id}",
            payload={"custom_fields": custom_fields},
            write=True,
        )

    def create_lead(
        self,
        title: str,
        person_id: int | None,
        organization_id: int,
        owner_id: int,
        custom_fields: dict[str, Any],
    ) -> str:
        payload: dict[str, Any] = {
            "title": title[:255],
            "organization_id": organization_id,
            "owner_id": owner_id,
            **custom_fields,
        }
        if person_id is not None:
            payload["person_id"] = person_id
        data = self._request("POST", "v1/leads", payload=payload, write=True)
        return str(data["id"])

    def find_lead_by_outreach_id(self, outreach_id: str) -> str | None:
        data = self._request(
            "GET",
            "v2/leads/search",
            params={
                "term": outreach_id,
                "fields": "custom_fields",
                "exact_match": "true",
                "limit": 1,
            },
        )
        value = self._search_id(data)
        return str(value) if value is not None else None

    def find_lead_by_event_id(self, lead_event_id: str) -> str | None:
        return self.find_lead_by_outreach_id(lead_event_id)

    def update_lead(self, lead_id: str, fields: dict[str, Any]) -> dict:
        return self._request("PATCH", f"v1/leads/{lead_id}", payload=fields, write=True)

    def archive_lead(self, lead_id: str) -> dict:
        return self.update_lead(lead_id, {"is_archived": True})

    def add_lead_activity(
        self,
        lead_id: str,
        subject: str,
        owner_id: int,
        *,
        note: str = "",
        activity_type: str = "task",
    ) -> int:
        data = self._request(
            "POST",
            "v1/activities",
            payload={
                "lead_id": lead_id,
                "subject": subject,
                "type": activity_type,
                "user_id": owner_id,
                "due_date": datetime.now(UTC).date().isoformat(),
                "note": note,
            },
            write=True,
        )
        return int(data["id"])

    def add_note(
        self, content: str, *, lead_id: str = "", deal_id: int | None = None
    ) -> int:
        payload: dict[str, Any] = {"content": content}
        if lead_id:
            payload["lead_id"] = lead_id
        if deal_id is not None:
            payload["deal_id"] = deal_id
        data = self._request("POST", "v1/notes", payload=payload, write=True)
        return int(data["id"])

    def convert_lead(self, lead_id: str, stage_id: int) -> str:
        data = self._request(
            "POST",
            f"v2/leads/{lead_id}/convert/deal",
            payload={"stage_id": stage_id},
            write=True,
        )
        return str(data.get("conversion_id") or data.get("id"))

    def conversion_status(self, lead_id: str, conversion_id: str) -> dict:
        return self._request(
            "GET", f"v2/leads/{lead_id}/convert/status/{conversion_id}"
        )

    def update_deal(self, deal_id: int, fields: dict[str, Any]) -> dict:
        return self._request("PATCH", f"v2/deals/{deal_id}", payload=fields, write=True)

    def find_deal_by_sequence_id(self, sequence_id: str) -> int | None:
        data = self._request(
            "GET",
            "v2/deals/search",
            params={
                "term": sequence_id,
                "fields": "custom_fields",
                "exact_match": "true",
                "limit": 1,
            },
        )
        value = self._search_id(data)
        return int(value) if value is not None else None

    def create_webhook(
        self,
        url: str,
        *,
        name: str,
        event_action: str = "change",
        event_object: str = "*",
    ) -> dict:
        payload: dict[str, Any] = {
            "subscription_url": url,
            "event_action": event_action,
            "event_object": event_object,
            "name": name,
            "user_id": self.settings.pipedrive_jordan_user_id,
            "version": "2.0",
        }
        if self.settings.pipedrive_webhook_user:
            payload["http_auth_user"] = self.settings.pipedrive_webhook_user
            payload["http_auth_password"] = self.settings.pipedrive_webhook_password
        return self._request("POST", "v1/webhooks", payload=payload, write=True)

    def list_webhooks(self) -> list[dict]:
        return self._request("GET", "v1/webhooks") or []

    def list_fields(self, resource: str) -> list[dict]:
        if resource not in {"dealFields", "personFields"}:
            raise ValueError("unsupported field resource")
        data = self._request("GET", f"v2/{resource}", params={"limit": 500}) or []
        return data.get("items", []) if isinstance(data, dict) else data

    def create_field(
        self, resource: str, name: str, field_type: str, options=None
    ) -> dict:
        if resource not in {"dealFields", "personFields"}:
            raise ValueError("unsupported field resource")
        payload: dict[str, Any] = {"field_name": name, "field_type": field_type}
        if options:
            payload["options"] = options
        return self._request("POST", f"v2/{resource}", payload=payload, write=True)


class GmailClient:
    SCOPES = (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    )

    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.gmail_service_account_json:
            raise ValueError("GMAIL_SERVICE_ACCOUNT_JSON is required")
        info = json.loads(settings.gmail_service_account_json)
        self.base_credentials = service_account.Credentials.from_service_account_info(
            info, scopes=self.SCOPES
        )

    def _service(self, mailbox: str):
        credentials = self.base_credentials.with_subject(mailbox)
        return build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def get_message(
        self, mailbox: str, message_id: str, *, format: str = "raw"
    ) -> dict:
        return (
            self._service(mailbox)
            .users()
            .messages()
            .get(userId=mailbox, id=message_id, format=format)
            .execute()
        )

    def find_message(self, mailbox: str, rfc822_message_id: str) -> str | None:
        query = f"rfc822msgid:{rfc822_message_id.strip('<>')}"
        response = (
            self._service(mailbox)
            .users()
            .messages()
            .list(userId=mailbox, q=query, maxResults=1)
            .execute()
        )
        messages = response.get("messages") or []
        return str(messages[0]["id"]) if messages else None

    def parse_raw_message(self, payload: dict) -> tuple[Any, bytes]:
        raw = payload.get("raw", "")
        padded = raw + "=" * (-len(raw) % 4)
        message_bytes = base64.urlsafe_b64decode(padded)
        return BytesParser(policy=email_policy).parsebytes(message_bytes), message_bytes

    def forward_message(self, mailbox: str, message_id: str, to: str) -> dict:
        self.settings.require_provider_writes()
        payload = self.get_message(mailbox, message_id, format="raw")
        original, message_bytes = self.parse_raw_message(payload)
        forward = EmailMessage()
        forward["To"] = to
        forward["From"] = mailbox
        subject = str(original.get("Subject") or "Reply")
        forward["Subject"] = (
            subject if subject.lower().startswith("fwd:") else f"Fwd: {subject}"
        )
        forward.set_content(
            "Forwarded automatically by the Aether sales integration for Jordan's review.\n"
            "Sent by Codex on Jon Schack's behalf.\n"
            "The original reply is attached."
        )
        forward.add_attachment(
            message_bytes,
            maintype="message",
            subtype="rfc822",
            filename="original-reply.eml",
        )
        encoded = base64.urlsafe_b64encode(forward.as_bytes()).decode("ascii")
        return (
            self._service(mailbox)
            .users()
            .messages()
            .send(userId=mailbox, body={"raw": encoded})
            .execute()
        )

    def send_text(self, mailbox: str, to: str, subject: str, body: str) -> dict:
        self.settings.require_provider_writes()
        message = EmailMessage()
        message["To"] = to
        message["From"] = mailbox
        message["Subject"] = subject
        message.set_content(body)
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        return (
            self._service(mailbox)
            .users()
            .messages()
            .send(userId=mailbox, body={"raw": encoded})
            .execute()
        )

    def profile(self, mailbox: str) -> dict:
        return self._service(mailbox).users().getProfile(userId=mailbox).execute()

    def history(self, mailbox: str, start_history_id: str) -> dict:
        service = self._service(mailbox)
        history: list[dict[str, Any]] = []
        page_token = None
        latest_history_id = start_history_id
        try:
            while True:
                request = (
                    service.users()
                    .history()
                    .list(
                        userId=mailbox,
                        startHistoryId=start_history_id,
                        historyTypes=["messageAdded"],
                        labelId="INBOX",
                        pageToken=page_token,
                    )
                )
                response = request.execute()
                history.extend(response.get("history") or [])
                latest_history_id = str(response.get("historyId") or latest_history_id)
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as error:
            if getattr(error.resp, "status", None) == 404:
                raise GmailHistoryExpired(
                    f"Gmail history cursor expired for {mailbox}"
                ) from error
            raise
        return {"history": history, "historyId": latest_history_id}

    def recent_inbox_messages(
        self, mailbox: str, *, newer_than_days: int = 7, limit: int = 500
    ) -> list[str]:
        service = self._service(mailbox)
        page_token = None
        message_ids: list[str] = []
        while len(message_ids) < limit:
            response = (
                service.users()
                .messages()
                .list(
                    userId=mailbox,
                    q=f"in:inbox newer_than:{max(1, newer_than_days)}d",
                    maxResults=min(500, limit - len(message_ids)),
                    pageToken=page_token,
                )
                .execute()
            )
            message_ids.extend(
                str(item["id"])
                for item in response.get("messages") or []
                if item.get("id")
            )
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return message_ids
