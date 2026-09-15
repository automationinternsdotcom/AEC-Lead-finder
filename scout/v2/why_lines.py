"""Generate the approved, sourced LinkedIn-style why line for daily outreach."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Callable

from .artifacts import ArtifactStore
from .contracts import LeadEvent, Organization, ReviewItem
from .ids import normalize_text, stable_uuid
from .state import StateStore
from copy_grammar import review_copy


ModelCall = Callable[[str, str, list[dict]], tuple[str, dict]]
WHY_QUESTION_FUTURE = (
    " Is there any chance we could stay in touch regarding your future janitorial needs?"
)
WHY_QUESTION_REVIEW = " Is there any chance you'll be reviewing your janitorial needs?"
WHY_QUESTION_ADDITIONAL_SPACE = (
    " Is there any chance you'll be reviewing your janitorial needs, with the additional space?"
)
WHY_TEMPLATES = {
    "acquisition": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {company} took ownership of {property} in {location}."
        + WHY_QUESTION_FUTURE,
        "slots": ("company", "property", "location"),
    },
    "opening": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {property} is opening in {location}."
        + WHY_QUESTION_FUTURE,
        "slots": ("property", "location"),
    },
    "planned_development": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that plans are moving forward for {project} in {location}."
        + WHY_QUESTION_FUTURE,
        "slots": ("project", "location"),
    },
    "approval": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {project} received {approval}."
        + WHY_QUESTION_FUTURE,
        "slots": ("project", "approval"),
    },
    "construction_start": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that construction started on {project} in {location}."
        + WHY_QUESTION_FUTURE,
        "slots": ("project", "location"),
    },
    "lease_relocation": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {company} is preparing to occupy {property} in {location}."
        + WHY_QUESTION_FUTURE,
        "slots": ("company", "property", "location"),
    },
    "site_acquisition": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {company} acquired {site} in {location}."
        + WHY_QUESTION_FUTURE,
        "slots": ("company", "site", "location"),
    },
    "expansion": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {company} is expanding in {location}."
        + WHY_QUESTION_ADDITIONAL_SPACE,
        "slots": ("company", "location"),
    },
    "funded_facility": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {funding} is supporting {project_or_expansion}."
        + WHY_QUESTION_FUTURE,
        "slots": ("funding", "project_or_expansion"),
    },
    "renovation_conversion": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {property} is being renovated into {new_use}."
        + WHY_QUESTION_REVIEW,
        "slots": ("property", "new_use"),
    },
    "construction_progress": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {project} reached {milestone}."
        + WHY_QUESTION_FUTURE,
        "slots": ("project", "milestone"),
    },
    "completion": {
        "text": "Hi [first name], I wanted to reach out after seeing on the news that {project} was completed in {location}."
        + WHY_QUESTION_FUTURE,
        "slots": ("project", "location"),
    },
}
ROUTING_OUTCOMES = {"route_new_owner", "skip_negative", "skip_general"}

PROMPT = """Select one approved Aether LinkedIn/cold-email opening template for every supplied Arizona property event. Use only the supplied event facts and source URLs; do not search for or invent facts.

Approved templates:
{templates}
- route_new_owner: seller, broker, listing, or unclear ownership transition; do not produce copy.
- skip_negative: closure, bankruptcy, lawsuit, stalled, or abandoned project; do not produce copy.
- skip_general: no specific property-level trigger; do not produce copy.

Rules:
- For a sale or acquisition, use acquisition only when the named organization is the buyer/new owner.
- Keep each slot concise, natural, and free of URLs. A location is one city or neighborhood without state text.
- Return insertion slots only. Do not rewrite the approved template.
- Proofread the assembled sentence for grammar only before returning slots. Include necessary articles in singular noun phrases (e.g. new_use: "a 255-room hotel", not "255-room hotel"). Preserve proper-name capitalization, numbers, event meaning, and the approved call to action. Do not reject a lead for a correctable grammar issue.

Events:
{events}

Return strict JSON only as an object mapping every exact lead_event_id to:
{{"template_key":"", "slots":{{}}}}
Include every submitted ID exactly once and invent no IDs."""


class WhyLineContractError(ValueError):
    pass


class WhyLineService:
    def __init__(
        self,
        state: StateStore,
        artifacts: ArtifactStore,
        model: str,
        call_model: ModelCall | None = None,
    ):
        self.state = state
        self.artifacts = artifacts
        self.model = model
        self.call_model = call_model or _default_model_call

    def generate(
        self,
        events: list[LeadEvent],
        organizations: list[Organization],
        attempts: int = 2,
    ) -> tuple[list[LeadEvent], list[ReviewItem]]:
        if not events:
            return [], []
        organizations_by_id = {item.organization_id: item for item in organizations}
        event_inputs = []
        for event in events:
            organization = organizations_by_id.get(event.organization_id)
            event_inputs.append(
                {
                    "lead_event_id": event.lead_event_id,
                    "organization": organization.canonical_name if organization else "",
                    "aliases": organization.aliases if organization else [],
                    "event": event.event,
                    "location": event.location,
                    "summary": event.summary,
                    "property_type": event.property_type,
                    "source_urls": [item.url for item in event.evidence],
                }
            )
        prompt = PROMPT.format(
            templates=_template_catalog(),
            events=json.dumps(event_inputs, sort_keys=True),
        )
        last_error: Exception | None = None
        for attempt_number in range(1, attempts + 1):
            attempt_id = stable_uuid(
                "attempt", self.artifacts.run_id, "why-lines", attempt_number
            )
            request = self.artifacts.write_raw(
                "why-lines",
                f"{attempt_id}-request.json",
                {"model": self.model, "prompt": prompt},
            )
            started = datetime.now(timezone.utc).isoformat()
            try:
                text, usage = self.call_model(self.model, prompt, [])
                response = self.artifacts.write_raw_text(
                    "why-lines", f"{attempt_id}-response.txt", text
                )
                rendered = parse_why_lines(text, events, organizations_by_id)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                last_error = exc
                self.state.record_provider_attempt(
                    attempt_id=attempt_id,
                    run_id=self.artifacts.run_id,
                    stage="why-lines",
                    provider="model",
                    target_type="lead_event_batch",
                    target_id=self.artifacts.run_id,
                    status="review",
                    request_artifact_path=request["path"],
                    response_artifact_path=locals().get("response", {}).get("path", ""),
                    error={"type": type(exc).__name__, "message": str(exc)},
                    started_at=started,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                continue
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.artifacts.run_id,
                stage="why-lines",
                provider="model",
                target_type="lead_event_batch",
                target_id=self.artifacts.run_id,
                status="completed",
                token_usage=usage,
                request_artifact_path=request["path"],
                response_artifact_path=response["path"],
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            updated = []
            for event in events:
                item = rendered[event.lead_event_id]
                enriched = event.model_copy(
                    update={
                        "why_line": item["why_line"],
                        "why_template_key": item["template_key"],
                        "why_line_status": item["status"],
                        "why_sources": [evidence.url for evidence in event.evidence],
                    }
                )
                self.state.save_lead_event(enriched)
                updated.append(enriched)
            return updated, []

        error_text = f"{type(last_error).__name__}:{last_error}"
        reviews = [
            ReviewItem(
                review_id=stable_uuid(
                    "review", self.artifacts.run_id, "why-lines", event.lead_event_id
                ),
                run_id=self.artifacts.run_id,
                stage="why-lines",
                record_type="lead_event",
                record_id=event.lead_event_id,
                reason_code="why_line_batch_invalid",
                validation_errors=[error_text],
                retry_count=attempts,
            )
            for event in events
        ]
        for review in reviews:
            self.state.add_review(review)
        return events, reviews


def parse_why_lines(
    text: str,
    events: list[LeadEvent],
    organizations_by_id: dict[str, Organization],
) -> dict[str, dict[str, str]]:
    cleaned = re.sub(r"<<ccr:[^>]+>>", "", str(text or ""))
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise WhyLineContractError("why-line response did not contain a JSON object")
    payload = json.loads(match.group())
    expected = {event.lead_event_id for event in events}
    if not isinstance(payload, dict) or set(payload) != expected:
        actual = set(payload) if isinstance(payload, dict) else set()
        raise WhyLineContractError(
            f"why-line IDs must match exactly; missing={sorted(expected - actual)}, unknown={sorted(actual - expected)}"
        )
    events_by_id = {event.lead_event_id: event for event in events}
    rendered: dict[str, dict[str, str]] = {}
    for event_id, raw in payload.items():
        if not isinstance(raw, dict):
            raise WhyLineContractError(f"why-line selection for {event_id} must be an object")
        template_key = str(raw.get("template_key") or "").strip()
        slots = raw.get("slots") or {}
        if not isinstance(slots, dict):
            raise WhyLineContractError(f"why-line slots for {event_id} must be an object")
        if template_key in ROUTING_OUTCOMES:
            if slots:
                raise WhyLineContractError(f"routing outcome {event_id} cannot contain slots")
            rendered[event_id] = {
                "why_line": "",
                "template_key": template_key,
                "status": "skip",
            }
            continue
        template = WHY_TEMPLATES.get(template_key)
        if not template:
            raise WhyLineContractError(f"unknown why-line template for {event_id}")
        required = set(template["slots"])
        if set(slots) != required:
            raise WhyLineContractError(f"why-line slots do not match template for {event_id}")
        values = {key: _clean_slot(value) for key, value in slots.items()}
        if any(not value or len(value.split()) > 16 for value in values.values()):
            raise WhyLineContractError(f"invalid why-line slot length for {event_id}")
        if any(re.search(r"(?:https?://|www\.)", value, re.I) for value in values.values()):
            raise WhyLineContractError(f"why-line slot contains a URL for {event_id}")
        if "location" in values:
            location = values["location"]
            if (
                "," in location
                or len(location.split()) > 3
                or normalize_text(location).split()[-1:] in (["az"], ["arizona"])
            ):
                raise WhyLineContractError(f"why-line location is invalid for {event_id}")
        if "company" in values:
            organization = organizations_by_id.get(events_by_id[event_id].organization_id)
            values["company"] = _resolve_company(values["company"], organization)
            if not values["company"]:
                raise WhyLineContractError(f"why-line company is invalid for {event_id}")
        line = review_copy(str(template["text"]).format(**values))
        if (
            not line.startswith(
                "Hi [first name], I wanted to reach out after seeing on the news that "
            )
            or not line.endswith("?")
            or ". Is there" not in line
            or not 20 <= len(line.split()) <= 55
            or "—" in line
            or "–" in line
        ):
            raise WhyLineContractError(f"rendered why line is invalid for {event_id}")
        rendered[event_id] = {
            "why_line": line,
            "template_key": template_key,
            "status": "valid",
        }
    return rendered


def _template_catalog() -> str:
    return "\n".join(
        f'- {key}: {value["text"]} Required slots: {", ".join(value["slots"])}.'
        for key, value in WHY_TEMPLATES.items()
    )


def _clean_slot(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" .")


def _resolve_company(value: str, organization: Organization | None) -> str:
    if not organization or len(value.split()) > 3:
        return ""
    wanted = normalize_text(value)
    for known in [organization.canonical_name, *organization.aliases]:
        if normalize_text(known) == wanted:
            return known
    return ""


def _default_model_call(model: str, prompt: str, tools: list[dict]) -> tuple[str, dict]:
    import llm

    return llm.call(model, prompt, tools=tools, text_format="json_object", with_usage=True)
