"""Completeness-checked deterministic and fuzzy event deduplication."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import re
from datetime import datetime, timezone
from typing import Callable, Iterable

from .artifacts import ArtifactStore
from .contracts import DiscoveryCandidate, LeadEvent, ReviewItem
from .ids import normalize_text, stable_hash
from .ids import stable_uuid
from .state import StateStore


class DedupContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DedupGroup:
    kept_id: str
    member_ids: tuple[str, ...]


def remove_redundant_groups(groups: Iterable[dict]) -> list[dict]:
    """Drop repeated/subset groups without inventing any new equivalences.

    Grok can append a singleton already covered by an earlier group. Conflicting
    overlaps, unknown IDs and missing IDs still fail the normal coverage check.
    """
    result = []
    for group in groups:
        members = set(group.get("member_ids") or [])
        if not members or group.get("kept_id") not in members:
            result.append(group)
            continue
        valid = lambda other: bool(other.get("member_ids")) and other.get("kept_id") in other["member_ids"]
        if any(valid(other) and members <= set(other["member_ids"]) for other in result):
            continue
        result = [other for other in result if not (valid(other) and set(other["member_ids"]) < members)]
        result.append(group)
    return result


def event_fingerprint(organization: str, event: str, location: str, event_date: str = "") -> str:
    return stable_hash(
        normalize_text(organization),
        normalize_text(event),
        normalize_text(location),
        str(event_date or "")[:10],
    )


def validate_fuzzy_groups(input_ids: Iterable[str], groups: Iterable[dict]) -> list[DedupGroup]:
    expected = list(input_ids)
    expected_set = set(expected)
    if len(expected) != len(expected_set):
        raise DedupContractError("input IDs must be unique")
    parsed: list[DedupGroup] = []
    seen: list[str] = []
    for raw in groups:
        kept = str(raw.get("kept_id") or "").strip()
        members = tuple(str(value).strip() for value in raw.get("member_ids") or [])
        if not kept or not members or kept not in members:
            raise DedupContractError("each group requires a kept_id included in nonempty member_ids")
        parsed.append(DedupGroup(kept, members))
        seen.extend(members)
    unknown = sorted(set(seen) - expected_set)
    counts = Counter(seen)
    missing = sorted(expected_set - set(seen))
    duplicates = sorted(key for key, count in counts.items() if count != 1)
    if unknown or missing or duplicates:
        raise DedupContractError(
            f"fuzzy groups must cover every ID exactly once; unknown={unknown}, missing={missing}, duplicates={duplicates}"
        )
    return parsed


def dedupe_candidates_exact(candidates: Iterable[DiscoveryCandidate]) -> list[DiscoveryCandidate]:
    groups: dict[str, list[DiscoveryCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.canonical_url, []).append(candidate)
    kept: list[DiscoveryCandidate] = []
    for group in groups.values():
        ranked = sorted(
            group,
            key=lambda item: (
                item.record_status.value != "valid",
                item.published_at is None,
                -len(item.title),
                item.candidate_id,
            ),
        )
        winner = ranked[0]
        ids = [item.candidate_id for item in ranked]
        kept.append(
            winner.model_copy(
                update={
                    "metadata": {
                        **winner.metadata,
                        "exact_duplicate_candidate_ids": ids,
                    }
                }
            )
        )
    return kept


FUZZY_PROMPT = """Group these lead events only when they describe the same real-world property/project/transaction. Return strict JSON only as a list of objects with kept_id and member_ids. Every input lead_event_id must appear exactly once, kept_id must be one of its member_ids, and no new IDs may be invented.

Events:
{events}"""


class FuzzyEventDeduper:
    def __init__(
        self,
        state: StateStore,
        artifacts: ArtifactStore,
        model: str,
        call_model: Callable[[str, str, list[dict]], tuple[str, dict]] | None = None,
    ):
        self.state = state
        self.artifacts = artifacts
        self.model = model
        self.call_model = call_model or _default_model_call

    def dedupe(self, events: list[LeadEvent], attempts: int = 2) -> tuple[list[LeadEvent], list[ReviewItem]]:
        if len(events) < 2:
            return events, []
        orgs = {item.organization_id: item for item in self.state.organizations()}
        inputs = [
            {
                "lead_event_id": event.lead_event_id,
                "organization": orgs.get(event.organization_id).canonical_name
                if orgs.get(event.organization_id)
                else event.organization_id,
                "event": event.event,
                "location": event.location,
                "date_posted": str(event.date_posted or ""),
            }
            for event in events
        ]
        prompt = FUZZY_PROMPT.format(events=json.dumps(inputs, sort_keys=True))
        last_error: Exception | None = None
        for attempt_number in range(1, attempts + 1):
            attempt_number = self.state.next_provider_attempt_number(
                self.artifacts.run_id,
                "dedup",
                "lead_event_batch",
                self.artifacts.run_id,
            )
            attempt_id = stable_uuid("attempt", self.artifacts.run_id, "dedup", attempt_number)
            request = self.artifacts.write_raw(
                "dedup", f"{attempt_id}-request.json", {"model": self.model, "prompt": prompt}
            )
            started = datetime.now(timezone.utc).isoformat()
            try:
                text, usage = self.call_model(self.model, prompt, [])
                response = self.artifacts.write_raw_text(
                    "dedup", f"{attempt_id}-response.txt", text
                )
                raw_groups = remove_redundant_groups(_parse_group_list(text))
                groups = validate_fuzzy_groups(
                    [event.lead_event_id for event in events], raw_groups
                )
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                last_error = exc
                self.state.record_provider_attempt(
                    attempt_id=attempt_id,
                    run_id=self.artifacts.run_id,
                    stage="dedup",
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
                stage="dedup",
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
            return self._apply(events, groups), []
        error = f"{type(last_error).__name__}:{last_error}"
        reviews = [
            ReviewItem(
                review_id=stable_uuid("review", self.artifacts.run_id, "dedup", event.lead_event_id),
                run_id=self.artifacts.run_id,
                stage="dedup",
                record_type="lead_event",
                record_id=event.lead_event_id,
                reason_code="fuzzy_dedup_contract_invalid",
                validation_errors=[error],
                retry_count=attempts,
            )
            for event in events
        ]
        for review in reviews:
            self.state.add_review(review)
        return events, reviews

    def _apply(
        self,
        events: list[LeadEvent],
        groups: list[DedupGroup],
    ) -> list[LeadEvent]:
        by_id = {event.lead_event_id: event for event in events}
        out: list[LeadEvent] = []
        for group in groups:
            kept = by_id[group.kept_id]
            members = [by_id[item] for item in group.member_ids]
            sources = list(
                dict.fromkeys(
                    candidate
                    for member in members
                    for candidate in member.supporting_candidate_ids
                )
            )
            evidence = list(
                {
                    (item.url, item.supports, item.provider): item
                    for member in members
                    for item in member.evidence
                }.values()
            )
            merged = kept.model_copy(
                update={"supporting_candidate_ids": sources, "evidence": evidence}
            )
            self.state.save_lead_event(merged)
            for member in members:
                self.state.save_event_merge(
                    self.artifacts.run_id, member.lead_event_id, kept.lead_event_id
                )
            out.append(merged)
        return out


def _parse_group_list(text: str) -> list[dict]:
    cleaned = re.sub(r"<<ccr:[^>]+>>", "", str(text or ""))
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if not match:
        raise DedupContractError("dedup response did not contain a JSON list")
    value = json.loads(match.group())
    if not isinstance(value, list):
        raise DedupContractError("dedup response must be a list")
    return value


def _default_model_call(model: str, prompt: str, tools: list[dict]) -> tuple[str, dict]:
    import llm

    return llm.call(model, prompt, tools=tools, text_format="json_object", with_usage=True)
