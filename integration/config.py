"""Environment configuration with outbound activation gates."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

REQUIRED_DEAL_FIELDS = {
    "aether_lead_event_id",
    "aether_outreach_id",
    "aether_contact_candidate_id",
    "canonical_company_id",
    "event_role",
    "outreach_sequence_id",
    "warmy_prospect_id",
    "outreach_state",
    "reply_disposition",
    "reply_received_at",
    "unsubscribe_url",
    "article_url",
    "date_posted",
}
REQUIRED_PERSON_FIELDS = {
    "aether_person_id",
    "verification_status",
    "suppressed",
    "suppression_reason",
    "unsubscribe_url",
}
REQUIRED_DISPOSITIONS = {
    "pending_review",
    "positive",
    "negative",
    "out_of_office",
    "unsubscribe",
    "other",
}
REQUIRED_PERSON_ENUM_VALUES = {
    "verification_status.pending",
    "verification_status.valid",
    "verification_status.invalid",
    "verification_status.catch_all",
    "verification_status.unknown",
    "suppressed.yes",
    "suppressed.no",
}


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def _json_map(name: str) -> dict[str, str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a JSON object")
    return {str(key): str(item) for key, item in value.items()}


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: str = "aether_sales.sqlite"
    environment: str = "development"
    public_base_url: str = "http://localhost:8187"
    log_level: str = "INFO"

    provider_writes_enabled: bool = False
    warmy_enrollment_enabled: bool = False
    campaign_start_enabled: bool = False
    pipedrive_automation_ready: bool = False
    email_templates_approved: bool = False
    postal_address: str = ""

    warmy_api_key: str = ""
    warmy_webhook_secret: str = ""
    warmy_base_url: str = "https://warmysender.com/api/v1"
    warmy_campaign_id: str = ""
    warmy_campaign_manifest_hash: str = ""
    warmy_mailbox_ids: tuple[str, ...] = ()
    warmy_mailbox_emails: dict[str, str] = field(default_factory=dict)
    warmy_daily_limit: int = 150
    warmy_verification_policy_version: str = "warmy-verify-v1"

    pipedrive_api_token: str = ""
    pipedrive_domain: str = ""
    pipedrive_pipeline_id: int = 47
    pipedrive_stage_id: int = 311
    pipedrive_jordan_user_id: int = 11380767
    pipedrive_webhook_user: str = ""
    pipedrive_webhook_password: str = ""
    pipedrive_deal_fields: dict[str, str] = field(default_factory=dict)
    pipedrive_person_fields: dict[str, str] = field(default_factory=dict)
    pipedrive_person_enum_values: dict[str, str] = field(default_factory=dict)
    pipedrive_reply_disposition_values: dict[str, str] = field(default_factory=dict)

    gmail_service_account_json: str = ""
    gmail_forward_to: str = "jw@aetherclean.com"
    gmail_monitored_mailboxes: tuple[str, ...] = ()
    gmail_reply_forwarding_enabled: bool = True

    unsubscribe_secret: str = ""
    alert_email: str = "jon@automationinterns.com"
    max_attempts: int = 8
    worker_batch_size: int = 15
    worker_lease_seconds: int = 300

    @property
    def signature_logo_url(self) -> str:
        return f"{self.public_base_url}/assets/aether-signature-logo.png"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(ENV_FILE, override=False)
        mailboxes = tuple(
            value.strip()
            for value in os.environ.get("WARMY_MAILBOX_IDS", "").split(",")
            if value.strip()
        )
        monitored = tuple(
            value.strip().lower()
            for value in os.environ.get("GMAIL_MONITORED_MAILBOXES", "").split(",")
            if value.strip()
        )
        return cls(
            database_path=os.environ.get("AETHER_SALES_DB_PATH", "aether_sales.sqlite"),
            environment=os.environ.get("AETHER_ENVIRONMENT", "development"),
            public_base_url=os.environ.get(
                "PUBLIC_BASE_URL", "http://localhost:8187"
            ).rstrip("/"),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            provider_writes_enabled=_flag("PROVIDER_WRITES_ENABLED"),
            warmy_enrollment_enabled=_flag("WARMY_ENROLLMENT_ENABLED"),
            campaign_start_enabled=_flag("CAMPAIGN_START_ENABLED"),
            pipedrive_automation_ready=_flag("PIPEDRIVE_AUTOMATION_READY"),
            email_templates_approved=_flag("EMAIL_TEMPLATES_APPROVED"),
            postal_address=os.environ.get("AETHER_POSTAL_ADDRESS", "").strip(),
            warmy_api_key=os.environ.get("WARMY_API_KEY", ""),
            warmy_webhook_secret=os.environ.get("WARMY_WEBHOOK_SECRET", ""),
            warmy_base_url=os.environ.get(
                "WARMY_BASE_URL", "https://warmysender.com/api/v1"
            ).rstrip("/"),
            warmy_campaign_id=os.environ.get("WARMY_CAMPAIGN_ID", ""),
            warmy_campaign_manifest_hash=os.environ.get(
                "WARMY_CAMPAIGN_MANIFEST_HASH", ""
            ).strip(),
            warmy_mailbox_ids=mailboxes,
            warmy_mailbox_emails=_json_map("WARMY_MAILBOX_EMAILS"),
            warmy_daily_limit=_int("WARMY_DAILY_LIMIT", 150),
            warmy_verification_policy_version=os.environ.get(
                "WARMY_VERIFICATION_POLICY_VERSION", "warmy-verify-v1"
            ).strip(),
            pipedrive_api_token=os.environ.get("PIPEDRIVE_API_TOKEN", ""),
            pipedrive_domain=os.environ.get("PIPEDRIVE_DOMAIN", ""),
            pipedrive_pipeline_id=_int("PIPEDRIVE_PIPELINE_ID", 47),
            pipedrive_stage_id=_int("PIPEDRIVE_STAGE_ID", 311),
            pipedrive_jordan_user_id=_int("PIPEDRIVE_JORDAN_USER_ID", 11380767),
            pipedrive_webhook_user=os.environ.get("PIPEDRIVE_WEBHOOK_USER", ""),
            pipedrive_webhook_password=os.environ.get("PIPEDRIVE_WEBHOOK_PASSWORD", ""),
            pipedrive_deal_fields=_json_map("PIPEDRIVE_DEAL_FIELDS"),
            pipedrive_person_fields=_json_map("PIPEDRIVE_PERSON_FIELDS"),
            pipedrive_person_enum_values=_json_map(
                "PIPEDRIVE_PERSON_ENUM_VALUES"
            ),
            pipedrive_reply_disposition_values=_json_map(
                "PIPEDRIVE_REPLY_DISPOSITION_VALUES"
            ),
            gmail_service_account_json=os.environ.get("GMAIL_SERVICE_ACCOUNT_JSON", ""),
            gmail_forward_to=os.environ.get(
                "GMAIL_FORWARD_TO", "jw@aetherclean.com"
            ).strip(),
            gmail_monitored_mailboxes=monitored,
            gmail_reply_forwarding_enabled=_flag(
                "GMAIL_REPLY_FORWARDING_ENABLED", True
            ),
            unsubscribe_secret=os.environ.get("UNSUBSCRIBE_SECRET", ""),
            alert_email=os.environ.get("ALERT_EMAIL", "jon@automationinterns.com"),
            max_attempts=_int("WORKER_MAX_ATTEMPTS", 8),
            worker_batch_size=_int("WORKER_BATCH_SIZE", 15),
            worker_lease_seconds=_int("WORKER_LEASE_SECONDS", 300),
        )

    @property
    def campaign_activation_ready(self) -> bool:
        return not self.campaign_activation_missing()

    @property
    def campaign_enrollment_ready(self) -> bool:
        return not self.campaign_enrollment_missing()

    def require_provider_writes(self) -> None:
        if not self.provider_writes_enabled:
            raise ActivationBlocked("provider writes are disabled")

    def require_campaign_activation(self) -> None:
        missing = self.campaign_activation_missing()
        if missing:
            raise ActivationBlocked(
                "campaign activation blocked: " + ", ".join(missing)
            )

    def campaign_activation_missing(self) -> list[str]:
        missing = self.campaign_enrollment_missing()
        if not self.campaign_start_enabled:
            missing.insert(2, "CAMPAIGN_START_ENABLED")
        return missing

    def require_campaign_enrollment(self) -> None:
        missing = self.campaign_enrollment_missing()
        if missing:
            raise ActivationBlocked(
                "campaign enrollment blocked: " + ", ".join(missing)
            )

    def campaign_enrollment_missing(self) -> list[str]:
        missing: list[str] = []
        if not self.provider_writes_enabled:
            missing.append("PROVIDER_WRITES_ENABLED")
        if not self.warmy_enrollment_enabled:
            missing.append("WARMY_ENROLLMENT_ENABLED")
        if not self.email_templates_approved:
            missing.append("EMAIL_TEMPLATES_APPROVED")
        if not self.pipedrive_automation_ready:
            missing.append("PIPEDRIVE_AUTOMATION_READY")
        if not self.postal_address:
            missing.append("AETHER_POSTAL_ADDRESS")
        if not self.public_base_url.startswith("https://"):
            missing.append("PUBLIC_BASE_URL (HTTPS)")
        if not self.unsubscribe_secret:
            missing.append("UNSUBSCRIBE_SECRET")
        if not self.warmy_api_key:
            missing.append("WARMY_API_KEY")
        if not self.warmy_webhook_secret:
            missing.append("WARMY_WEBHOOK_SECRET")
        if not self.warmy_campaign_id:
            missing.append("WARMY_CAMPAIGN_ID")
        if not self.warmy_campaign_manifest_hash:
            missing.append("WARMY_CAMPAIGN_MANIFEST_HASH")
        mailbox_ids = set(self.warmy_mailbox_ids)
        if len(self.warmy_mailbox_ids) != 6 or len(mailbox_ids) != 6:
            missing.append("WARMY_MAILBOX_IDS (six unique IDs)")
        if set(self.warmy_mailbox_emails) != mailbox_ids:
            missing.append("WARMY_MAILBOX_EMAILS (exact ID map)")
        if not self.pipedrive_api_token or not self.pipedrive_domain:
            missing.append("PIPEDRIVE_API_TOKEN/PIPEDRIVE_DOMAIN")
        if not self.pipedrive_webhook_user or not self.pipedrive_webhook_password:
            missing.append("PIPEDRIVE webhook basic auth")
        if not REQUIRED_DEAL_FIELDS.issubset(self.pipedrive_deal_fields):
            missing.append("PIPEDRIVE_DEAL_FIELDS (complete semantic map)")
        if not REQUIRED_PERSON_FIELDS.issubset(self.pipedrive_person_fields):
            missing.append("PIPEDRIVE_PERSON_FIELDS (complete semantic map)")
        if not REQUIRED_PERSON_ENUM_VALUES.issubset(
            self.pipedrive_person_enum_values
        ):
            missing.append("PIPEDRIVE_PERSON_ENUM_VALUES (complete option map)")
        configured_dispositions = {
            value.casefold().replace(" ", "_")
            for value in self.pipedrive_reply_disposition_values.values()
        }
        if not REQUIRED_DISPOSITIONS.issubset(configured_dispositions):
            missing.append("PIPEDRIVE_REPLY_DISPOSITION_VALUES (all options)")
        if self.gmail_reply_forwarding_enabled:
            if not self.gmail_service_account_json:
                missing.append("GMAIL_SERVICE_ACCOUNT_JSON")
            monitored = set(self.gmail_monitored_mailboxes)
            expected_mailboxes = {
                email.casefold() for email in self.warmy_mailbox_emails.values()
            }
            if len(self.gmail_monitored_mailboxes) != 6 or len(monitored) != 6:
                missing.append("GMAIL_MONITORED_MAILBOXES (six unique mailboxes)")
            elif monitored != expected_mailboxes:
                missing.append("GMAIL_MONITORED_MAILBOXES (match Warmy mailbox map)")
        return missing


class ActivationBlocked(RuntimeError):
    """Raised when a live side effect has not been explicitly enabled."""
