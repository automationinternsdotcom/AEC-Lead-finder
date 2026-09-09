"""Typed article qualification and completeness-safe model handling."""
from __future__ import annotations

import json
import re
from html import unescape
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .artifacts import ArtifactStore
from .contracts import (
    DiscoveryCandidate,
    Evidence,
    LeadEvent,
    Organization,
    Person,
    RecordStatus,
    ReviewItem,
)
from .ids import event_id, organization_id, person_id, stable_hash, stable_uuid
from .state import StateStore


ModelCall = Callable[[str, str, list[dict]], tuple[str, dict]]


LEAD_GUIDANCE = """Qualifying triggers include a specific Arizona commercial property opening, lease-up, tenant relocation, ownership acquisition by the buyer/new owner, construction start, completion, approved development, permit, rezoning, expansion, renovation, redevelopment, or other property-level operational change.
Reject macro market reports, opinion columns, broker/vendor awards, generic company news, seller-only listings, financing without a named property use, out-of-state projects, residential-only single-family work, and articles where the property/operator cannot be identified.
Choose business_name as the entity most likely to buy or influence facilities services: operator, tenant, owner, property manager, developer, or community/asset name. Do not use the publisher, broker, seller, architect, or contractor unless that entity is the operator/owner/manager.
service_angle must name a concrete Aether fit such as recurring janitorial, common-area cleaning, day porter, evening/overnight cleaning, turnover/lease-up support, pressure washing, floor care, or maintenance coordination."""


QUALIFICATION_PROMPT = """Open and read the exact article URL below using web search. Determine whether it reports a specific Arizona commercial-property event that could create a facilities-services opportunity. Never infer rejection from missing data.

Candidate ID: {candidate_id}
URL: {url}
Title: {title}
Requested article window: {window_start} through {window_end}, inclusive.

Guidance:
{lead_guidance}

Return strict JSON with these keys:
qualified, business_name, person, event, date_posted, location, summary, state,
priority, property_type, service_angle, filter_reason, confidence.

For an explicit non-qualifying article or an article published outside the requested window, return qualified=false and a nonempty filter_reason. For a qualifying article, date_posted must be the exact article publication date and fall inside the requested window; state must be Arizona, priority must be high or medium, confidence must be high or low, and business_name, event, and location must be nonempty. Use empty strings for unknown optional values. Return no prose."""


BATCH_QUALIFICATION_PROMPT = """Qualify this bounded batch using only the supplied saved article evidence. Do not search the web and do not identify people. For every exact candidate_id, decide whether the article reports a specific Arizona commercial-property event that creates a facilities-services opportunity.

Requested article window: {window_start} through {window_end}, inclusive.

Guidance:
{lead_guidance}

Return strict JSON only as one object mapping every exact candidate_id to an object with keys: qualified, business_name, event, date_posted, location, summary, state, priority, property_type, service_angle, filter_reason, confidence. date_posted must be YYYY-MM-DD or an empty string, never a timestamp. Include every submitted ID exactly once and invent no IDs. An explicit rejection or an article outside the requested window requires a specific filter_reason. A qualification requires a date inside the requested window, state Arizona, priority high or medium, confidence high or low, and nonempty business_name, event, and location.

Candidates:
{candidates}"""


class JudgmentPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    qualified: bool
    business_name: str = ""
    person: str = ""
    event: str = ""
    date_posted: date | None = None
    location: str = ""
    summary: str = ""
    state: str = ""
    priority: str = ""
    property_type: str = "other"
    service_angle: str = ""
    filter_reason: str = ""
    confidence: str = "high"

    @field_validator("date_posted", mode="before")
    @classmethod
    def blank_date_is_unknown(cls, value):
        if value in ("", None):
            return None
        if isinstance(value, str):
            cleaned = value.strip()
            for date_format in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
                try:
                    return datetime.strptime(cleaned, date_format).date()
                except ValueError:
                    continue
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            threshold = 0.5 if 0 <= value <= 1 else 50
            return "high" if value >= threshold else "low"
        return value

    @model_validator(mode="after")
    def complete_decision(self) -> "JudgmentPayload":
        if not self.qualified:
            if not self.filter_reason.strip():
                raise ValueError("explicit rejections require filter_reason")
            return self
        missing = [
            field
            for field in (
                "business_name",
                "event",
                "date_posted",
                "location",
                "state",
                "priority",
            )
            if not str(getattr(self, field) or "").strip()
        ]
        if missing:
            raise ValueError(f"qualified result missing fields: {', '.join(missing)}")
        if self.state != "Arizona":
            raise ValueError("qualified result must be in Arizona")
        if self.priority.casefold() not in {"high", "medium"}:
            raise ValueError("qualified result priority must be high or medium")
        if self.confidence not in {"high", "low"}:
            raise ValueError("confidence must be high or low")
        return self


class QualificationResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    events: list[LeadEvent] = Field(default_factory=list)
    people: list[Person] = Field(default_factory=list)
    reviews: list[ReviewItem] = Field(default_factory=list)
    rejected_candidate_ids: list[str] = Field(default_factory=list)


class QualificationService:
    def __init__(
        self,
        state: StateStore,
        artifacts: ArtifactStore,
        model: str,
        call_model: ModelCall | None = None,
        workers: int = 1,
        window_start: date | None = None,
        window_end: date | None = None,
        batch_size: int = 20,
    ):
        self.state = state
        self.artifacts = artifacts
        self.model = model
        self.call_model = call_model or _default_model_call
        self.workers = max(1, workers)
        self.window_start = window_start
        self.window_end = window_end
        self.batch_size = max(1, min(batch_size, 25))

    def qualify(
        self, candidates: list[DiscoveryCandidate], *, retry_review: bool = False
    ) -> QualificationResult:
        result = QualificationResult()
        eligible = []
        for candidate in candidates:
            if candidate.record_status != RecordStatus.VALID and not (
                retry_review and candidate.record_status == RecordStatus.REVIEW
            ):
                continue
            if retry_review and candidate.record_status == RecordStatus.REVIEW:
                candidate = candidate.model_copy(
                    update={"record_status": RecordStatus.VALID, "validation_errors": []}
                )
            eligible.append(candidate)
        eligible.sort(key=lambda item: item.candidate_id)
        batches = list(_chunks(eligible, self.batch_size))
        if not batches:
            return result
        completed = self.state.completed_provider_target_ids(
            self.artifacts.run_id, "qualify", "discovery_candidate_batch"
        )
        pending = [batch for batch in batches if self._batch_id(batch) not in completed]
        if self.workers == 1:
            outcomes = map(self._qualify_batch, pending)
        else:
            pool = ThreadPoolExecutor(max_workers=self.workers)
            outcomes = pool.map(self._qualify_batch, pending)
        try:
            for batch_result in outcomes:
                result.events.extend(batch_result.events)
                result.people.extend(batch_result.people)
                result.reviews.extend(batch_result.reviews)
                result.rejected_candidate_ids.extend(
                    batch_result.rejected_candidate_ids
                )
        finally:
            if self.workers != 1:
                pool.shutdown(wait=True, cancel_futures=True)
        return result

    def _batch_id(self, candidates: list[DiscoveryCandidate]) -> str:
        return stable_hash(
            self.window_start or "",
            self.window_end or "",
            *(item.candidate_id for item in candidates),
        )[:20]

    def _qualify_batch(
        self, candidates: list[DiscoveryCandidate]
    ) -> QualificationResult:
        result = QualificationResult()
        batch_id = self._batch_id(candidates)
        attempt_number = self.state.next_provider_attempt_number(
            self.artifacts.run_id,
            "qualify",
            "discovery_candidate_batch",
            batch_id,
        )
        attempt_id = stable_uuid(
            "attempt",
            self.artifacts.run_id,
            "qualify-batch",
            batch_id,
            attempt_number,
        )
        payload = [
            {
                "candidate_id": item.candidate_id,
                "url": item.canonical_url,
                "title": item.title,
                "published_at": str(item.published_at or ""),
                "saved_article_excerpt": _candidate_excerpt(item, 2_500),
            }
            for item in candidates
        ]
        prompt = BATCH_QUALIFICATION_PROMPT.format(
            window_start=self.window_start or "not specified",
            window_end=self.window_end or "not specified",
            lead_guidance=LEAD_GUIDANCE,
            candidates=json.dumps(payload, sort_keys=True, ensure_ascii=False),
        )
        request = self.artifacts.write_raw(
            "qualify",
            f"{attempt_id}-request.json",
            {"model": self.model, "prompt": prompt},
        )
        started = datetime.now(timezone.utc).isoformat()
        try:
            text, usage = self.call_model(self.model, prompt, [])
        except Exception as exc:
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=self.artifacts.run_id,
                stage="qualify",
                provider="model",
                target_type="discovery_candidate_batch",
                target_id=batch_id,
                status="failed",
                request_artifact_path=request["path"],
                error={"type": type(exc).__name__, "message": str(exc)},
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            raise
        response = self.artifacts.write_raw_text(
            "qualify", f"{attempt_id}-response.txt", text
        )
        invalid: dict[str, Exception] = {}
        judgments: dict[str, JudgmentPayload] = {}
        try:
            raw = _parse_json(text)
            if not isinstance(raw, dict):
                raise ValueError("batch qualification response must be an object")
            expected = {item.candidate_id for item in candidates}
            missing = expected - set(raw)
            unknown = set(raw) - expected
            for candidate_id_value in missing:
                invalid[candidate_id_value] = ValueError(
                    "qualification response omitted candidate ID"
                )
            for candidate_id_value in expected & set(raw):
                try:
                    judgments[candidate_id_value] = JudgmentPayload.model_validate(
                        raw[candidate_id_value]
                    )
                except Exception as exc:
                    invalid[candidate_id_value] = exc
            if unknown and not invalid:
                # Unknown IDs cannot mutate state. Preserve valid expected results,
                # but keep the provider attempt in review for contract auditing.
                invalid["__unknown_ids__"] = ValueError(
                    f"qualification response invented IDs: {sorted(unknown)}"
                )
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
            invalid = {item.candidate_id: exc for item in candidates}

        for candidate in candidates:
            if candidate.candidate_id in invalid:
                review = self._quarantine_without_attempt(
                    candidate, response["path"], invalid[candidate.candidate_id]
                )
                result.reviews.append(review)
                continue
            event, person, review, rejected = self._apply_payload(
                candidate, judgments[candidate.candidate_id]
            )
            if event:
                result.events.append(event)
            if person:
                result.people.append(person)
            if review:
                result.reviews.append(review)
            if rejected:
                result.rejected_candidate_ids.append(rejected)

        self.state.record_provider_attempt(
            attempt_id=attempt_id,
            run_id=self.artifacts.run_id,
            stage="qualify",
            provider="model",
            target_type="discovery_candidate_batch",
            target_id=batch_id,
            status="review" if invalid else "completed",
            token_usage=usage,
            request_artifact_path=request["path"],
            response_artifact_path=response["path"],
            error=(
                {
                    "type": "PartialValidationError",
                    "message": f"{len(invalid)} candidate judgments were invalid",
                }
                if invalid
                else {}
            ),
            started_at=started,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        return result

    def _qualify_one(
        self, candidate: DiscoveryCandidate
    ) -> tuple[LeadEvent | None, Person | None, ReviewItem | None, str]:
        attempt_number = self.state.next_provider_attempt_number(
            candidate.run_id,
            "qualify",
            "discovery_candidate",
            candidate.candidate_id,
        )
        attempt_id = stable_uuid(
            "attempt",
            candidate.run_id,
            "qualify",
            candidate.candidate_id,
            attempt_number,
        )
        prompt = QUALIFICATION_PROMPT.format(
            candidate_id=candidate.candidate_id,
            url=candidate.canonical_url,
            title=candidate.title,
            window_start=self.window_start or "not specified",
            window_end=self.window_end or "not specified",
            lead_guidance=LEAD_GUIDANCE,
        )
        request = self.artifacts.write_raw(
            "qualify", f"{attempt_id}-request.json", {"model": self.model, "prompt": prompt}
        )
        started = datetime.now(timezone.utc).isoformat()
        try:
            text, usage = self.call_model(self.model, prompt, [{"type": "web_search"}])
            response = self.artifacts.write_raw_text(
                "qualify", f"{attempt_id}-response.txt", text
            )
            payload = JudgmentPayload.model_validate(_parse_json(text))
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return self._quarantine(
                candidate,
                attempt_id,
                request["path"],
                locals().get("response", {}).get("path", ""),
                started,
                exc,
            )
        except Exception as exc:
            self.state.record_provider_attempt(
                attempt_id=attempt_id,
                run_id=candidate.run_id,
                stage="qualify",
                provider="model",
                target_type="discovery_candidate",
                target_id=candidate.candidate_id,
                status="failed",
                request_artifact_path=request["path"],
                error={"type": type(exc).__name__, "message": str(exc)},
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            raise
        self.state.record_provider_attempt(
            attempt_id=attempt_id,
            run_id=candidate.run_id,
            stage="qualify",
            provider="model",
            target_type="discovery_candidate",
            target_id=candidate.candidate_id,
            status="completed",
            token_usage=usage,
            request_artifact_path=request["path"],
            response_artifact_path=response["path"],
            started_at=started,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        return self._apply_payload(candidate, payload)

    def _apply_payload(
        self, candidate: DiscoveryCandidate, payload: JudgmentPayload
    ) -> tuple[LeadEvent | None, Person | None, ReviewItem | None, str]:
        if not payload.qualified:
            self._reject_candidate(candidate, payload.filter_reason)
            self._record_completion(candidate, "rejected")
            return None, None, None, candidate.candidate_id

        effective_date = payload.date_posted or (
            candidate.published_at.date() if candidate.published_at else None
        )
        if effective_date is not None and (
            (self.window_start is not None and effective_date < self.window_start)
            or (self.window_end is not None and effective_date > self.window_end)
        ):
            reason = (
                "qualified_event_date_outside_requested_window:"
                f"{effective_date}:{self.window_start or ''}:{self.window_end or ''}"
            )
            self._reject_candidate(candidate, reason)
            self._record_completion(candidate, "rejected")
            return None, None, None, candidate.candidate_id

        org_id = organization_id(payload.business_name, "", payload.location)
        support_ids = candidate.metadata.get("exact_duplicate_candidate_ids") or [
            candidate.candidate_id
        ]
        support_candidates = {
            item.candidate_id: item
            for item in self.state.candidates_for_run(candidate.run_id)
            if item.candidate_id in support_ids
        }
        evidence = [
            Evidence(
                url=support_candidates[item].canonical_url,
                supports="Source article for the qualified property event.",
                provider=support_candidates[item].provider,
            )
            for item in support_ids
            if item in support_candidates
        ] or [
            Evidence(
                url=candidate.canonical_url,
                supports="Source article for the qualified property event.",
                provider=candidate.provider,
            )
        ]
        organization = Organization(
            organization_id=org_id,
            canonical_name=payload.business_name.strip(),
            location=payload.location.strip(),
            evidence=evidence,
        )
        self.state.save_organization(organization)
        lead_event_id = event_id(
            org_id,
            payload.event,
            payload.location,
            (payload.date_posted or (candidate.published_at.date() if candidate.published_at else "")),
        )
        low_confidence = payload.confidence == "low"
        event = LeadEvent(
            lead_event_id=lead_event_id,
            run_id=candidate.run_id,
            organization_id=org_id,
            primary_candidate_id=candidate.candidate_id,
            supporting_candidate_ids=list(support_ids),
            event=payload.event.strip(),
            location=payload.location.strip(),
            date_posted=payload.date_posted or (
                candidate.published_at.date() if candidate.published_at else None
            ),
            summary=payload.summary.strip(),
            priority=payload.priority,
            property_type=payload.property_type.strip() or "other",
            service_angle=payload.service_angle.strip(),
            filter_reason=payload.filter_reason.strip(),
            confidence=payload.confidence,
            record_status=RecordStatus.REVIEW if low_confidence else RecordStatus.VALID,
            validation_errors=["qualification_confidence_low"] if low_confidence else [],
            evidence=evidence,
        )
        self.state.save_lead_event(event)
        person = None
        if payload.person.strip():
            pid = person_id(payload.person, org_id)
            person = Person(
                person_id=pid,
                organization_id=org_id,
                name=payload.person.strip(),
                evidence=evidence,
                inferred_identity=True,
            )
            self.state.save_person(person)
        review = None
        if low_confidence:
            review = ReviewItem(
                review_id=stable_uuid(
                    "review", candidate.run_id, "qualify", event.lead_event_id, "low-confidence"
                ),
                run_id=candidate.run_id,
                stage="qualify",
                record_type="lead_event",
                record_id=event.lead_event_id,
                reason_code="qualification_confidence_low",
                validation_errors=["qualification_confidence_low"],
            )
            self.state.add_review(review)
        self._record_completion(
            candidate, "review" if low_confidence else "qualified"
        )
        return event, person, review, ""

    def _record_completion(
        self, candidate: DiscoveryCandidate, outcome: str
    ) -> None:
        if self.window_start is None or self.window_end is None:
            return
        self.state.record_qualification_completion(
            candidate_id=candidate.candidate_id,
            since_date=self.window_start.isoformat(),
            stamp=self.window_end.isoformat(),
            run_id=candidate.run_id,
            outcome=outcome,
        )

    def _quarantine_without_attempt(
        self,
        candidate: DiscoveryCandidate,
        response_path: str,
        exc: Exception,
    ) -> ReviewItem:
        error = f"{type(exc).__name__}:{exc}"
        updated = candidate.model_copy(
            update={
                "record_status": RecordStatus.REVIEW,
                "validation_errors": [*candidate.validation_errors, error],
            }
        )
        self.state.save_candidate(updated)
        review = ReviewItem(
            review_id=stable_uuid(
                "review", candidate.run_id, "qualify", candidate.candidate_id
            ),
            run_id=candidate.run_id,
            stage="qualify",
            record_type="discovery_candidate",
            record_id=candidate.candidate_id,
            reason_code="model_contract_invalid",
            validation_errors=[error],
            raw_artifact_path=response_path,
        )
        self.state.add_review(review)
        return review

    def _reject_candidate(self, candidate: DiscoveryCandidate, reason: str) -> None:
        errors = list(candidate.validation_errors)
        if reason and reason not in errors:
            errors.append(reason)
        rejected = candidate.model_copy(
            update={
                "record_status": RecordStatus.REJECTED,
                "validation_errors": errors,
            }
        )
        self.state.save_candidate(rejected)
        self.state.retire_events_for_candidate(
            candidate.run_id,
            candidate.candidate_id,
            reason or "qualification_rejected_on_retry",
        )

    def _quarantine(
        self,
        candidate: DiscoveryCandidate,
        attempt_id: str,
        request_path: str,
        response_path: str,
        started: str,
        exc: Exception,
    ) -> tuple[None, None, ReviewItem, str]:
        error = f"{type(exc).__name__}:{exc}"
        updated = candidate.model_copy(
            update={
                "record_status": RecordStatus.REVIEW,
                "validation_errors": [*candidate.validation_errors, error],
            }
        )
        self.state.save_candidate(updated)
        review = ReviewItem(
            review_id=stable_uuid("review", candidate.run_id, "qualify", candidate.candidate_id),
            run_id=candidate.run_id,
            stage="qualify",
            record_type="discovery_candidate",
            record_id=candidate.candidate_id,
            reason_code="model_contract_invalid",
            validation_errors=[error],
            raw_artifact_path=response_path,
        )
        self.state.add_review(review)
        self.state.record_provider_attempt(
            attempt_id=attempt_id,
            run_id=candidate.run_id,
            stage="qualify",
            provider="model",
            target_type="discovery_candidate",
            target_id=candidate.candidate_id,
            status="review",
            request_artifact_path=request_path,
            response_artifact_path=response_path,
            error={"type": type(exc).__name__, "message": str(exc)},
            started_at=started,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        return None, None, review, ""


def _parse_json(text: str) -> object:
    cleaned = re.sub(r"<<ccr:[^>]+>>", "", str(text or ""))
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError("model response did not contain a JSON object")
    return json.loads(match.group())


def _candidate_excerpt(candidate: DiscoveryCandidate, limit: int) -> str:
    if not candidate.raw_artifact_path:
        return ""
    try:
        raw = Path(candidate.raw_artifact_path).read_text(
            encoding="utf-8", errors="replace"
        )[:200_000]
    except OSError:
        return ""
    raw = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", raw)
    value = unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _chunks(
    values: list[DiscoveryCandidate], size: int
) -> list[list[DiscoveryCandidate]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _default_model_call(model: str, prompt: str, tools: list[dict]) -> tuple[str, dict]:
    import llm

    return llm.call(
        model,
        prompt,
        tools=tools,
        text_format="json_object",
        with_usage=True,
    )
