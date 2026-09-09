"""Validated Warmy campaign manifest with immutable safety defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import json
import re

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import ActivationBlocked, Settings

COPY_PLACEHOLDER = "TODO_APPROVED_COPY"
SIGNATURE_LOGO_PLACEHOLDER = "{{AETHER_SIGNATURE_LOGO_URL}}"
BODY_MERGE_VARIABLES = {"firstName", "company", "whyLine", "unsubscribeUrl"}
SUBJECT_MERGE_VARIABLES = {"firstName", "company"}


class CampaignStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stepIndex: int = Field(ge=0)
    type: str = "email"
    subject: str = Field(min_length=1)
    bodyHtml: str = Field(min_length=1)
    bodyText: str = Field(min_length=1)
    delayDays: int = Field(ge=0)
    delayHours: int = Field(default=0, ge=0, le=23)
    isActive: bool = True

    @field_validator("type")
    @classmethod
    def email_only(cls, value: str) -> str:
        if value != "email":
            raise ValueError("Aether's initial campaign must be email-only")
        return value


class CampaignManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str = "Aether AEC evergreen outreach"
    channel: str = "email"
    timezone: str = "America/Phoenix"
    dailySendLimit: int = Field(default=150, ge=1, le=300)
    sendingWindowStart: int = Field(default=8, ge=0, le=23)
    sendingWindowEnd: int = Field(default=16, ge=0, le=23)
    scheduleDays: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    stopOnReply: bool = True
    stopOnBounce: bool = True
    stopOnUnsubscribe: bool = True
    trackOpens: bool = False
    trackClicks: bool = False
    mailboxIds: list[str]
    steps: list[CampaignStep] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def enforce_initial_policy(self):
        if self.channel != "email":
            raise ValueError("campaign channel must be email")
        if self.timezone != "America/Phoenix":
            raise ValueError("campaign timezone must be America/Phoenix")
        if self.scheduleDays != [1, 2, 3, 4, 5]:
            raise ValueError("campaign schedule must be Monday through Friday")
        if (self.sendingWindowStart, self.sendingWindowEnd) != (8, 16):
            raise ValueError("campaign sending window must be 08:00–16:00")
        if not all((self.stopOnReply, self.stopOnBounce, self.stopOnUnsubscribe)):
            raise ValueError("reply, bounce, and unsubscribe stops are mandatory")
        if [step.delayDays for step in self.steps] != [0, 3, 4, 7]:
            raise ValueError(
                "campaign delays must be relative step offsets 0, 3, 4, and 7"
            )
        if [step.stepIndex for step in self.steps] != [0, 1, 2, 3]:
            raise ValueError("campaign step indexes must be 0 through 3")
        if any(not step.isActive for step in self.steps):
            raise ValueError("all campaign steps must be active")
        if any(step.delayHours != 0 for step in self.steps):
            raise ValueError("campaign step delayHours must be zero")
        if len(set(self.mailboxIds)) != len(self.mailboxIds):
            raise ValueError("campaign mailbox IDs must be unique")
        return self


def load_campaign(path: str | Path, settings: Settings) -> CampaignManifest:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    raw["mailboxIds"] = list(settings.warmy_mailbox_ids)
    raw["dailySendLimit"] = settings.warmy_daily_limit
    serialized = yaml.safe_dump(raw)
    if COPY_PLACEHOLDER in serialized:
        raise ActivationBlocked("campaign copy is still a TODO placeholder")
    if not settings.email_templates_approved:
        raise ActivationBlocked("EMAIL_TEMPLATES_APPROVED is not enabled")
    if not settings.postal_address:
        raise ActivationBlocked("AETHER_POSTAL_ADDRESS is required")
    if not settings.signature_logo_url.startswith("https://"):
        raise ActivationBlocked(
            "PUBLIC_BASE_URL (HTTPS) is required for signature logo"
        )
    for index, step in enumerate(raw.get("steps") or []):
        if "{{AETHER_POSTAL_ADDRESS}}" not in str(
            step.get("bodyHtml") or ""
        ) or "{{AETHER_POSTAL_ADDRESS}}" not in str(step.get("bodyText") or ""):
            raise ActivationBlocked(
                f"step {index} is missing the postal-address placeholder"
            )
        if SIGNATURE_LOGO_PLACEHOLDER not in str(step.get("bodyHtml") or ""):
            raise ActivationBlocked(
                f"step {index} is missing the signature-logo placeholder"
            )
    raw = _replace(raw, "{{AETHER_POSTAL_ADDRESS}}", settings.postal_address)
    raw = _replace(raw, SIGNATURE_LOGO_PLACEHOLDER, settings.signature_logo_url)
    manifest = CampaignManifest.model_validate(raw)
    if len(manifest.mailboxIds) != 6:
        raise ActivationBlocked(
            "WARMY_MAILBOX_IDS must contain all six Aether mailboxes"
        )
    for step in manifest.steps:
        if (
            "{{unsubscribeUrl}}" not in step.bodyHtml
            or "{{unsubscribeUrl}}" not in step.bodyText
        ):
            raise ActivationBlocked(
                f"step {step.stepIndex} is missing the unsubscribe link"
            )
        variables: set[str] = set()
        for body in (step.bodyHtml, step.bodyText):
            found, malformed = _merge_variables(body)
            if malformed:
                raise ActivationBlocked(
                    f"step {step.stepIndex} has malformed merge variables"
                )
            variables.update(found)
        unsupported = variables - BODY_MERGE_VARIABLES
        if unsupported:
            raise ActivationBlocked(
                f"step {step.stepIndex} has unsupported merge variables: {sorted(unsupported)}"
            )
        subject_variables, malformed_subject = _merge_variables(step.subject)
        if malformed_subject:
            raise ActivationBlocked(
                f"step {step.stepIndex} subject has malformed merge variables"
            )
        unsupported_subject = subject_variables - SUBJECT_MERGE_VARIABLES
        if unsupported_subject:
            raise ActivationBlocked(
                f"step {step.stepIndex} subject has unsupported merge variables: "
                f"{sorted(unsupported_subject)}"
            )
    return manifest


def _merge_variables(value: str) -> tuple[set[str], bool]:
    """Extract simple merge names and reject filters, underscores, or malformed braces."""
    variables: set[str] = set()
    cursor = 0
    while True:
        opening = value.find("{{", cursor)
        closing = value.find("}}", cursor)
        if closing != -1 and (opening == -1 or closing < opening):
            return variables, True
        if opening == -1:
            return variables, False
        end = value.find("}}", opening + 2)
        if end == -1:
            return variables, True
        inner = value[opening + 2 : end]
        match = re.fullmatch(r"\s*([A-Za-z][A-Za-z0-9]*)\s*", inner)
        if not match:
            return variables, True
        variables.add(match.group(1))
        cursor = end + 2


def campaign_manifest_hash(value: CampaignManifest | dict[str, Any]) -> str:
    raw = value.model_dump(mode="json") if isinstance(value, CampaignManifest) else value
    if isinstance(raw.get("data"), dict):
        raw = raw["data"]
    normalized = {
        key: raw.get(key)
        for key in (
            "name",
            "description",
            "channel",
            "timezone",
            "dailySendLimit",
            "sendingWindowStart",
            "sendingWindowEnd",
            "scheduleDays",
            "stopOnReply",
            "stopOnBounce",
            "stopOnUnsubscribe",
            "trackOpens",
            "trackClicks",
            "mailboxIds",
            "steps",
        )
    }
    normalized["mailboxIds"] = sorted(
        str(item.get("id") if isinstance(item, dict) else item)
        for item in normalized.get("mailboxIds") or []
    )
    step_keys = (
        "stepIndex",
        "type",
        "subject",
        "bodyHtml",
        "bodyText",
        "delayDays",
        "delayHours",
        "isActive",
    )
    normalized["steps"] = sorted(
        [
            {key: item.get(key) for key in step_keys}
            for item in normalized.get("steps") or []
        ],
        key=lambda item: int(item.get("stepIndex", 0)),
    )
    return hashlib.sha256(
        json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _replace(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_replace(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, old, new) for key, item in value.items()}
    return value
