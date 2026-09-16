import httpx
import pytest

from integration.config import ActivationBlocked, Settings
from integration.providers import WarmyClient, validate_provider_prospect


@pytest.mark.parametrize("status", ["running", "scheduled", "paused"])
def test_provider_enrollment_rejects_non_draft_campaigns(status):
    writes = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"id": "campaign-1", "status": status}})
        writes.append(request)
        return httpx.Response(200, json={"data": {"enrolled": 1}})

    settings = Settings(
        warmy_base_url="https://warmy.test/api",
        warmy_enrollment_enabled=True,
        provider_writes_enabled=True,
    )
    client = WarmyClient(
        settings,
        transport=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://warmy.test/api/",
        ),
    )
    with pytest.raises(ActivationBlocked, match="draft campaign"):
        client.enroll("campaign-1", ["prospect-1"], "operation-1")
    assert writes == []


def test_provider_enrollment_allows_draft_campaign_only():
    writes = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"id": "campaign-1", "status": "draft"}})
        writes.append(request)
        return httpx.Response(200, json={"data": {"enrolled": 1}})

    settings = Settings(
        warmy_base_url="https://warmy.test/api",
        warmy_enrollment_enabled=True,
        provider_writes_enabled=True,
    )
    client = WarmyClient(
        settings,
        transport=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://warmy.test/api/",
        ),
    )
    assert client.enroll("campaign-1", ["prospect-1"], "operation-1")["data"]["enrolled"] == 1
    assert len(writes) == 1


def test_provider_prospect_actual_shape_requires_active_unsuppressed_unreplied():
    base = {
        "id": "prospect-1",
        "email": "jane@example.com",
        "globalStatus": "active",
        "suppressionReason": "",
        "lastRepliedAt": None,
    }
    assert validate_provider_prospect(
        {"data": base},
        expected_id="prospect-1",
        expected_email="jane@example.com",
        initial_step=True,
    )["globalStatus"] == "active"
    for field, value in (("globalStatus", "suppressed"), ("suppressionReason", "unsubscribe"), ("lastRepliedAt", "2026-09-15T10:00:00Z")):
        candidate = {**base, field: value}
        with pytest.raises(ActivationBlocked):
            validate_provider_prospect(
                candidate,
                expected_id="prospect-1",
                expected_email="jane@example.com",
                initial_step=True,
            )
    missing_status = {key: value for key, value in base.items() if key != "globalStatus"}
    with pytest.raises(ActivationBlocked, match="globalStatus"):
        validate_provider_prospect(
            missing_status,
            expected_id="prospect-1",
            expected_email="jane@example.com",
            initial_step=True,
        )
