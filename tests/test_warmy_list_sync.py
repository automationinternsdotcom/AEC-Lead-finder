from __future__ import annotations

import json

import httpx
import pytest

from integration.config import ActivationBlocked, Settings
from integration.database import Database
from integration.providers import WarmyClient
from integration.workflows import SalesWorkflows


CONTACT = {
    "email": "person@example.com",
    "first_name": "Jane",
    "last_name": "Doe",
    "organization_name": "Example Builders",
    "title": "Facilities Director",
    "why_line": "Example Builders opened a new Phoenix facility.",
    "project_property_name": "Example Phoenix Facility",
    "article_url": "https://example.com/article",
}


def _client(handler):
    settings = Settings(
        provider_writes_enabled=True,
        warmy_api_key="test-key",
        warmy_base_url="https://warmy.example/api/v1",
    )
    return WarmyClient(settings, httpx.Client(transport=httpx.MockTransport(handler), base_url=settings.warmy_base_url + "/"))


def test_append_prospect_to_list_uses_documented_idempotent_upsert_payload():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json={
            "data": {"id": "prospect-1"},
            "listId": "list-article-leads",
            "list": {"attached": True},
        })

    client = _client(handler)
    try:
        response = client.append_prospect_to_list(CONTACT, "list-article-leads", "append-1")
    finally:
        client.close()
    assert response["list"]["attached"] is True
    assert len(requests) == 1
    assert requests[0].method == "POST"
    body = json.loads(requests[0].content)
    assert body["listId"] == "list-article-leads"
    assert body["enroll"] is False
    assert body["customFields"]["projectPropertyName"] == "Example Phoenix Facility"


def test_append_prospect_to_list_requires_provider_attachment_acknowledgement():
    client = _client(lambda request: httpx.Response(200, json={"data": {"id": "prospect-1"}}))
    try:
        with pytest.raises(ActivationBlocked, match="attachment acknowledgement"):
            client.append_prospect_to_list(CONTACT, "list-article-leads", "append-1")
    finally:
        client.close()


def test_append_prospect_to_list_rejects_mismatched_list_acknowledgement():
    client = _client(
        lambda request: httpx.Response(
            200,
            json={"data": {"id": "prospect-1"}, "listId": "other-list", "list": {"attached": True}},
        )
    )
    try:
        with pytest.raises(ActivationBlocked, match="acknowledgement mismatch"):
            client.append_prospect_to_list(CONTACT, "list-article-leads", "append-1")
    finally:
        client.close()


def test_update_prospect_sends_complete_custom_field_snapshot():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"id": "prospect-1"}})

    client = _client(handler)
    try:
        client.update_prospect("prospect-1", CONTACT, "update-1")
    finally:
        client.close()
    body = json.loads(requests[0].content)
    assert body["customFields"] == {
        "sourceArticle": "https://example.com/article",
        "whyLine": CONTACT["why_line"],
        "projectPropertyName": "Example Phoenix Facility",
    }


def test_custom_field_merge_preserves_provider_fields_not_in_local_snapshot():
    contact = dict(CONTACT, _existing_custom_fields={
        "providerTag": "keep-me",
        "whyLine": "stale-copy",
    })
    assert WarmyClient._prospect_custom_fields(contact) == {
        "providerTag": "keep-me",
        "sourceArticle": "https://example.com/article",
        "whyLine": CONTACT["why_line"],
        "projectPropertyName": "Example Phoenix Facility",
    }


def test_canonical_list_id_survives_worker_settings_reload(tmp_path):
    workflows = SalesWorkflows(
        Settings(warmy_prospect_list_id="list-article-leads"),
        Database(tmp_path / "sales.sqlite"),
    )
    assert workflows._warmy_list_id() == "list-article-leads"


def test_existing_list_row_without_custom_fields_uses_exact_detail_snapshot(tmp_path):
    def handler(request: httpx.Request):
        if request.url.path.endswith("/prospects"):
            return httpx.Response(200, json={"data": {"prospects": [{"id": "prospect-1", "email": CONTACT["email"]}]}})
        return httpx.Response(200, json={
            "id": "prospect-1",
            "email": CONTACT["email"],
            "customFields": {"providerTag": "keep-me"},
        })

    client = _client(handler)
    workflows = SalesWorkflows(
        Settings(warmy_prospect_list_id="list-article-leads"),
        Database(tmp_path / "sales.sqlite"),
        warmy=client,
    )
    try:
        listing = client.find_prospect_by_email(CONTACT["email"])
        prospect_id, snapshot = workflows._warmy_existing_snapshot(
            CONTACT["email"], "prospect-1", listing
        )
    finally:
        workflows.close()
    assert prospect_id == "prospect-1"
    assert snapshot["customFields"] == {"providerTag": "keep-me"}


def test_provider_prospect_membership_is_required_when_canonical_list_is_configured():
    response = {"id": "prospect-1", "email": CONTACT["email"], "globalStatus": "active",
                "suppressionReason": None, "lastRepliedAt": None,
                "listMemberships": [{"listId": "list-article-leads"}]}
    from integration.providers import validate_provider_prospect
    assert validate_provider_prospect(
        response,
        expected_id="prospect-1",
        expected_email=CONTACT["email"],
        initial_step=True,
        expected_list_id="list-article-leads",
    )["id"] == "prospect-1"


def test_provider_prospect_membership_accepts_authoritative_detail_id_shape():
    from integration.providers import validate_provider_prospect

    response = {
        "id": "prospect-1",
        "email": CONTACT["email"],
        "globalStatus": "active",
        "suppressionReason": None,
        "lastRepliedAt": None,
        "listMemberships": [
            {
                "id": "list-article-leads",
                "name": "Aether AEC Article Leads",
                "color": "#6366f1",
            }
        ],
    }
    assert validate_provider_prospect(
        response,
        expected_id="prospect-1",
        expected_email=CONTACT["email"],
        initial_step=True,
        expected_list_id="list-article-leads",
    )["id"] == "prospect-1"


def test_provider_prospect_membership_missing_or_wrong_is_held():
    from integration.providers import validate_provider_prospect
    for memberships in (None, [{"listId": "other-list"}]):
        with pytest.raises(ActivationBlocked, match="canonical list"):
            validate_provider_prospect(
                {"id": "prospect-1", "email": CONTACT["email"], "globalStatus": "active",
                 "suppressionReason": None, "lastRepliedAt": None,
                 "listMemberships": memberships},
                expected_id="prospect-1",
                expected_email=CONTACT["email"],
                initial_step=True,
                expected_list_id="list-article-leads",
            )
