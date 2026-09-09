"""Subject-variant approval and bounded, source-backed observation verification.

This is an internal schema, not an invented Warmy API payload. A supported MCP,
API or Computer Use read must supply the observation when variants are omitted
from the ordinary campaign response. Absence never means verified.
"""
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .config import ActivationBlocked

PLAN_PATH = Path(__file__).resolve().parents[1] / "config/aether_subject_variants.json"


class SubjectVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    weight: int = Field(gt=0, le=100, strict=True)


class VariantStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_index: int = Field(ge=0, le=3)
    variants: list[SubjectVariant] = Field(min_length=2)

    @model_validator(mode="after")
    def valid_split(self):
        if sum(v.weight for v in self.variants) != 100:
            raise ValueError("variant weights must total 100")
        if len({v.label for v in self.variants}) != len(self.variants):
            raise ValueError("variant labels must be unique")
        from .campaign import _merge_variables, SUBJECT_MERGE_VARIABLES
        for variant in self.variants:
            variables, malformed = _merge_variables(variant.subject)
            if malformed or variables - SUBJECT_MERGE_VARIABLES:
                raise ValueError("unsupported subject merge variables")
        return self


class VariantPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_id: str = Field(min_length=1)
    steps: list[VariantStep] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_steps(self):
        if len({s.step_index for s in self.steps}) != len(self.steps):
            raise ValueError("variant step indexes must be unique")
        return self


def load_plan(path=PLAN_PATH):
    return VariantPlan.model_validate(json.loads(Path(path).read_text()))


def variant_hash(plan):
    plan = VariantPlan.model_validate(plan) if isinstance(plan, dict) else plan
    value = plan.model_dump()
    value["steps"].sort(key=lambda s: s["step_index"])
    for step in value["steps"]:
        step["variants"].sort(key=lambda v: v["label"])
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def complete_campaign_hash(base_manifest_hash, plan):
    return hashlib.sha256(f"campaign-v2:{base_manifest_hash}:{variant_hash(plan)}".encode()).hexdigest()


def verify_observation(plan, observation, base_manifest_hash, *, now=None):
    now = now or datetime.now(UTC)
    try:
        observed = datetime.fromisoformat(observation["observed_at"])
        actual = VariantPlan.model_validate(observation["plan"])
        valid_time = observed.tzinfo is not None and timedelta(0) <= now - observed <= timedelta(hours=24)
    except (KeyError, ValueError, TypeError) as exc:
        raise ActivationBlocked("Variant observation is missing or malformed") from exc
    if not valid_time:
        raise ActivationBlocked("Variant observation must be timezone-aware and within the last 24 hours")
    if observation.get("source") not in {"warmysender_mcp", "warmysender_api", "computer_use"} or not observation.get("source_reference"):
        raise ActivationBlocked("Variant observation requires a supported read surface and source reference")
    if observation.get("base_manifest_hash") != base_manifest_hash:
        raise ActivationBlocked("Variant observation belongs to a different base campaign snapshot")
    if variant_hash(actual) != variant_hash(plan):
        raise ActivationBlocked("Campaign subject variants or weights differ from approval")
    return {"status": "verified", "complete_campaign_hash": complete_campaign_hash(base_manifest_hash, plan),
            "observed_at": observed.isoformat(), "source": observation["source"],
            "source_reference": observation["source_reference"]}


def verification_status(db, campaign_id, base_manifest_hash, *, plan_path=PLAN_PATH, now=None):
    plan = load_plan(plan_path)
    if plan.campaign_id != campaign_id:
        return {"status": "not_configured"}
    observation = db.get_state("campaign-variant-observation:" + campaign_id)
    if not observation:
        return {"status": "not_verified", "reason": "Variant-capable read required; ordinary campaign hash omits A/B state"}
    try:
        return verify_observation(plan, observation, base_manifest_hash, now=now)
    except ActivationBlocked as exc:
        return {"status": "not_verified", "reason": str(exc)}


def require_verified(db, campaign_id, base_manifest_hash):
    result = verification_status(db, campaign_id, base_manifest_hash)
    if result["status"] == "not_verified":
        raise ActivationBlocked(result["reason"])
    return result
