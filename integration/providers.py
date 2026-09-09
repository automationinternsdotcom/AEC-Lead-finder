"""Provider clients with explicit write gates and bounded error handling."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as email_policy
from typing import Any

import httpx
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import Settings


class ProviderError(RuntimeError):
    def __init__(self, provider: str, status_code: int, code: str, message: str):
        super().__init__(f"{provider} {status_code} {code}: {message}")
        self.provider = provider
        self.status_code = status_code
        self.code = code
        self.retryable = status_code in {408, 409, 425, 429, 500, 502, 503, 504}


class GmailHistoryExpired(RuntimeError):
    """The Gmail history cursor aged out and requires a bounded resync."""


class WarmyClient:
    def __init__(self, settings: Settings, transport: httpx.Client | None = None):
        self.settings = settings
        self.http = transport or httpx.Client(
            base_url=settings.warmy_base_url + "/",
            timeout=30,
            headers={
                "Authorization": f"Bearer {settings.warmy_api_key}",
                "Accept": "application/json",
            },
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

    def create_prospect(self, contact: dict[str, Any], operation_key: str) -> dict:
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

    def find_prospect_by_email(self, email: str) -> dict[str, Any] | None:
        normalized = email.strip().casefold()
        response = self._request(
            "GET", "prospects", params={"email": normalized}
        )
        data = response.get("data") if isinstance(response, dict) else response
        if isinstance(data, dict):
            rows = next(
                (
                    data[key]
                    for key in ("prospects", "items", "results")
                    if isinstance(data.get(key), list)
                ),
                [],
            )
        else:
            rows = data if isinstance(data, list) else []
        return next(
            (
                row
                for row in rows
                if str(row.get("email") or "").strip().casefold() == normalized
            ),
            None,
        )

    @staticmethod
    def _prospect_custom_fields(contact: dict[str, Any]) -> dict[str, Any]:
        values = {
            "aetherLeadEventId": contact.get("lead_event_id", ""),
            "aetherOutreachId": contact.get("outreach_id", ""),
            "aetherContactCandidateId": contact.get(
                "source_contact_candidate_id", ""
            ),
            "sourceArticle": contact.get("article_url", ""),
            "unsubscribeUrl": contact.get("unsubscribe_url", ""),
            "whyLine": contact.get("why_line", ""),
        }
        return {key: value for key, value in values.items() if value not in ("", None)}

    def update_prospect(
        self, prospect_id: str, contact: dict[str, Any], operation_key: str
    ) -> dict:
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

    def enroll(
        self, campaign_id: str, prospect_ids: list[str], operation_key: str
    ) -> dict:
        if not self.settings.warmy_enrollment_enabled:
            from .config import ActivationBlocked

            raise ActivationBlocked("Warmy enrollment is disabled")
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
