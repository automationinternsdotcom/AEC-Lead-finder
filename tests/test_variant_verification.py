import copy
from datetime import UTC, datetime, timedelta

import pytest

from integration.config import ActivationBlocked
from integration.database import Database
from integration.variants import (load_plan, variant_hash, complete_campaign_hash,
                                  verify_observation, verification_status)

NOW = datetime(2026, 9, 9, tzinfo=UTC)


def observation():
    return {"observed_at": NOW.isoformat(), "plan": load_plan().model_dump(),
            "source": "computer_use", "source_reference": "captured campaign editor snapshot",
            "base_manifest_hash": "base"}


def test_full_hash_covers_subjects_weights_and_step_identity():
    plan = load_plan().model_dump()
    for mutation in ("subject", "weight", "step"):
        changed = copy.deepcopy(plan)
        if mutation == "subject":
            changed["steps"][0]["variants"][1]["subject"] = "Wrong subject"
        elif mutation == "weight":
            changed["steps"][0]["variants"][0]["weight"] = 60
            changed["steps"][0]["variants"][1]["weight"] = 40
        else:
            changed["steps"][0]["step_index"] = 1
        assert complete_campaign_hash("base", plan) != complete_campaign_hash("base", changed)
        with pytest.raises(ActivationBlocked, match="differ from approval"):
            verify_observation(load_plan(), {**observation(), "plan": changed}, "base", now=NOW)
    reordered = copy.deepcopy(plan)
    reordered["steps"][0]["variants"].reverse()
    assert variant_hash(plan) == variant_hash(reordered)
    assert complete_campaign_hash("base", plan) != complete_campaign_hash("changed-body", plan)


@pytest.mark.parametrize("changes", [
    {"observed_at": (NOW - timedelta(days=2)).isoformat()},
    {"observed_at": (NOW + timedelta(hours=1)).isoformat()},
    {"observed_at": "2026-09-09T00:00:00"},
    {"source": "assumed_from_config"}, {"source_reference": ""},
    {"base_manifest_hash": "other"}, {"plan": {}},
])
def test_unverified_or_stale_observations_fail(changes):
    with pytest.raises(ActivationBlocked):
        verify_observation(load_plan(), {**observation(), **changes}, "base", now=NOW)


def test_omission_is_not_verification_and_saved_observation_expires(tmp_path):
    db = Database(str(tmp_path / "sales.sqlite"))
    plan = load_plan()
    assert verification_status(db, plan.campaign_id, "base", now=NOW)["status"] == "not_verified"
    db.set_state("campaign-variant-observation:" + plan.campaign_id, observation())
    assert verification_status(db, plan.campaign_id, "base", now=NOW)["status"] == "verified"
    assert verification_status(db, plan.campaign_id, "base", now=NOW + timedelta(days=2))["status"] == "not_verified"


def test_invalid_variant_configuration_is_rejected():
    plan = load_plan().model_dump()
    plan["steps"][0]["variants"][0]["weight"] = 1
    with pytest.raises(ValueError, match="total 100"):
        variant_hash(plan)
