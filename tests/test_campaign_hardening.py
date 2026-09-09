from __future__ import annotations

import json
import sys
from itertools import accumulate
from pathlib import Path

import pytest

from integration import cli
from integration.campaign import CampaignManifest, campaign_manifest_hash, load_campaign
from integration.config import ActivationBlocked, Settings


ROOT = Path(__file__).resolve().parent.parent
CAMPAIGN = ROOT / "config" / "aether_campaign.yaml"
SIGNATURE_LOGO_TAG = (
    '<p><img src="{{AETHER_SIGNATURE_LOGO_URL}}" '
    'alt="Aether Facility Services logo" width="160" '
    'style="display:block;max-width:160px;height:auto;"></p>'
)


def _settings(**changes) -> Settings:
    values = {
        "provider_writes_enabled": True,
        "email_templates_approved": True,
        "postal_address": "123 Main St, Phoenix, AZ 85001",
        "public_base_url": "https://sales.example.com",
        "warmy_mailbox_ids": tuple(f"mailbox-{index}" for index in range(1, 7)),
    }
    values.update(changes)
    return Settings(**values)


def test_approved_campaign_copy_has_disclosure_and_safe_step_invariants():
    manifest = load_campaign(CAMPAIGN, _settings())
    signature_parts = (
        "Jordan Whitehurst, Partner",
        "Aether Facility Services, LLC",
        "O: (602) 612-6393",
        "M: (813) 992-0858",
        "2120 W Encanto Blvd, Phoenix, AZ 85009",
    )

    assert all(step.isActive and step.delayHours == 0 for step in manifest.steps)
    assert manifest.trackOpens is True
    assert list(accumulate(step.delayDays for step in manifest.steps)) == [0, 3, 7, 14]
    assert len(manifest.mailboxIds) == len(set(manifest.mailboxIds)) == 6
    assert all(
        "commercial message from aether facility services" in body.lower()
        for step in manifest.steps
        for body in (step.bodyHtml, step.bodyText)
    )
    assert not any(step.subject.casefold().startswith("re:") for step in manifest.steps)
    assert "Phoenix area" not in manifest.steps[1].bodyText
    assert all(
        all(part in body for part in signature_parts)
        for step in manifest.steps
        for body in (step.bodyHtml, step.bodyText)
    )
    assert all(
        'src="https://sales.example.com/assets/aether-signature-logo.png"'
        in step.bodyHtml
        and 'alt="Aether Facility Services logo"' in step.bodyHtml
        and "aether-signature-logo.png" not in step.bodyText
        for step in manifest.steps
    )


def test_campaign_loader_requires_hosted_signature_logo(tmp_path):
    source = CAMPAIGN.read_text(encoding="utf-8")
    path = tmp_path / "campaign.yaml"
    path.write_text(
        source.replace(
            SIGNATURE_LOGO_TAG,
            "",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ActivationBlocked, match="signature-logo placeholder"):
        load_campaign(path, _settings())

    with pytest.raises(ActivationBlocked, match="PUBLIC_BASE_URL"):
        load_campaign(CAMPAIGN, _settings(public_base_url="http://localhost:8187"))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("isActive", False, "all campaign steps must be active"),
        ("delayHours", 1, "delayHours must be zero"),
    ],
)
def test_campaign_manifest_rejects_unsafe_step_settings(field, value, message):
    manifest = load_campaign(CAMPAIGN, _settings())
    payload = manifest.model_dump(mode="json")
    payload["steps"][0][field] = value

    with pytest.raises(ValueError, match=message):
        CampaignManifest.model_validate(payload)


def test_campaign_manifest_rejects_duplicate_mailbox_ids():
    manifest = load_campaign(CAMPAIGN, _settings())
    payload = manifest.model_dump(mode="json")
    payload["mailboxIds"][-1] = payload["mailboxIds"][0]

    with pytest.raises(ValueError, match="mailbox IDs must be unique"):
        CampaignManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        ("A facilities idea for {{company}}", "Hello {{whyLine}}"),
        ("{{whyLine}}", "{{company_name}}"),
        ("{{whyLine}}", "{{company | upper}}"),
        ("{{whyLine}}", "{{company}"),
        ("{{whyLine}}", "company}}"),
    ],
)
def test_campaign_loader_rejects_unsupported_or_malformed_merge_variables(
    tmp_path, needle, replacement
):
    source = CAMPAIGN.read_text(encoding="utf-8")
    path = tmp_path / "campaign.yaml"
    path.write_text(source.replace(needle, replacement, 1), encoding="utf-8")

    with pytest.raises(ActivationBlocked):
        load_campaign(path, _settings())


def test_create_campaign_draft_versions_idempotency_and_verifies_readback(
    monkeypatch, capsys
):
    settings = _settings()
    manifest = load_campaign(CAMPAIGN, settings)
    expected_hash = campaign_manifest_hash(manifest)

    class FakeWarmy:
        def __init__(self, _settings):
            self.create_key = ""
            self.read_id = ""

        def create_campaign(self, payload, operation_key):
            assert payload == manifest.model_dump(mode="json")
            self.create_key = operation_key
            return {"data": {"id": "campaign-42"}}

        def get_campaign(self, campaign_id):
            self.read_id = campaign_id
            payload = manifest.model_dump(mode="json")
            payload.update({"id": campaign_id, "status": "paused"})
            return {"data": payload}

        def close(self):
            pass

    warmy = None

    def make_warmy(value):
        nonlocal warmy
        warmy = FakeWarmy(value)
        return warmy

    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(cli, "WarmyClient", make_warmy)
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "create-campaign-draft", str(CAMPAIGN), "--apply"],
    )

    assert cli.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["campaign_id"] == "campaign-42"
    assert output["manifest_hash"] == expected_hash
    assert output["mailbox_verification"] == {
        "status": "verified",
        "requires_ui_check": False,
    }
    assert warmy is not None
    assert warmy.create_key == f"aether-campaign-evergreen-v1:{expected_hash}"
    assert warmy.read_id == "campaign-42"


def test_update_campaign_patches_existing_warmy_campaign_and_verifies_readback(
    monkeypatch, capsys
):
    settings = _settings(warmy_campaign_id="campaign-live")
    manifest = load_campaign(CAMPAIGN, settings)
    expected_hash = campaign_manifest_hash(manifest)

    class FakeWarmy:
        def __init__(self, _settings):
            self.update_call = None
            self.read_id = ""

        def update_campaign(self, campaign_id, payload, operation_key):
            self.update_call = (campaign_id, payload, operation_key)
            return {"data": {"id": campaign_id}}

        def get_campaign(self, campaign_id):
            self.read_id = campaign_id
            payload = manifest.model_dump(mode="json")
            payload.update({"id": campaign_id, "status": "paused"})
            return {"data": payload}

        def close(self):
            pass

    warmy = None

    def make_warmy(value):
        nonlocal warmy
        warmy = FakeWarmy(value)
        return warmy

    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(cli, "WarmyClient", make_warmy)
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "update-campaign", str(CAMPAIGN), "--apply"],
    )

    assert cli.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["campaign_id"] == "campaign-live"
    assert output["manifest_hash"] == expected_hash
    assert output["mailbox_verification"] == {
        "status": "verified",
        "requires_ui_check": False,
    }
    assert warmy is not None
    assert warmy.update_call == (
        "campaign-live",
        manifest.model_dump(mode="json"),
        f"aether-campaign-update-v1:campaign-live:{expected_hash}",
    )
    assert warmy.read_id == "campaign-live"


def test_verify_campaign_signature_reads_warmy_campaign(monkeypatch, capsys):
    settings = _settings(warmy_campaign_id="campaign-live")
    manifest = load_campaign(CAMPAIGN, settings)

    class FakeWarmy:
        def __init__(self, _settings):
            self.read_id = ""

        def get_campaign(self, campaign_id):
            self.read_id = campaign_id
            payload = manifest.model_dump(mode="json")
            payload.update({"id": campaign_id, "status": "paused"})
            return {"data": payload}

        def close(self):
            pass

    warmy = None

    def make_warmy(value):
        nonlocal warmy
        warmy = FakeWarmy(value)
        return warmy

    monkeypatch.setattr(cli.Settings, "from_env", staticmethod(lambda: settings))
    monkeypatch.setattr(cli, "WarmyClient", make_warmy)
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "verify-campaign-signature"],
    )

    assert cli.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "campaign_id": "campaign-live",
        "signature_logo_url": "https://sales.example.com/assets/aether-signature-logo.png",
        "status": "verified",
        "steps": 4,
        "track_opens": True,
    }
    assert warmy is not None
    assert warmy.read_id == "campaign-live"


def test_verify_campaign_signature_rejects_missing_live_logo():
    with pytest.raises(ActivationBlocked, match="missing logo in steps \\[0\\]"):
        cli._verify_campaign_signature_payload(
            {
                "steps": [
                    {
                        "stepIndex": 0,
                        "bodyHtml": (
                            "Jordan Whitehurst, Partner "
                            "Aether Facility Services, LLC "
                            "O: (602) 612-6393 "
                            "M: (813) 992-0858 "
                            "2120 W Encanto Blvd, Phoenix, AZ 85009"
                        ),
                    }
                ]
            },
            "https://sales.example.com/assets/aether-signature-logo.png",
        )


def test_campaign_draft_readback_without_mailboxes_reports_ui_check():
    manifest = load_campaign(CAMPAIGN, _settings())
    payload = manifest.model_dump(mode="json")
    payload.pop("mailboxIds")
    payload.update({"id": "campaign-42", "status": "paused"})

    class FakeWarmy:
        def get_campaign(self, campaign_id):
            assert campaign_id == "campaign-42"
            return {"data": payload}

    campaign_id, readback, mailbox_verification = cli._verify_campaign_draft(
        FakeWarmy(),
        {"data": {"id": "campaign-42"}},
        campaign_manifest_hash(manifest),
        manifest.model_dump(mode="json"),
    )
    assert campaign_id == "campaign-42"
    assert readback["data"]["status"] == "paused"
    assert mailbox_verification == {
        "status": "not_returned",
        "requires_ui_check": True,
        "reason": "Warmy readback omitted mailboxIds/mailboxes; verify in the UI",
    }


def test_campaign_draft_readback_rejects_present_mailbox_mismatch():
    manifest = load_campaign(CAMPAIGN, _settings())
    payload = manifest.model_dump(mode="json")
    payload.update({"id": "campaign-42", "status": "paused", "mailboxIds": ["other-mailbox"]})

    class FakeWarmy:
        def get_campaign(self, campaign_id):
            assert campaign_id == "campaign-42"
            return {"data": payload}

    with pytest.raises(ActivationBlocked, match="campaign mailbox set mismatch"):
        cli._verify_campaign_draft(
            FakeWarmy(),
            {"data": {"id": "campaign-42"}},
            campaign_manifest_hash(manifest),
            manifest.model_dump(mode="json"),
        )


def test_campaign_draft_readback_rejects_running_campaign():
    manifest = load_campaign(CAMPAIGN, _settings())
    payload = manifest.model_dump(mode="json")
    payload.update({"id": "campaign-42", "status": "running"})

    class FakeWarmy:
        def get_campaign(self, campaign_id):
            assert campaign_id == "campaign-42"
            return {"data": payload}

    with pytest.raises(ActivationBlocked, match="unsafe campaign status running"):
        cli._verify_campaign_draft(
            FakeWarmy(), {"data": {"id": "campaign-42"}}, campaign_manifest_hash(manifest)
        )


def test_enrollment_readiness_does_not_require_campaign_start_flag():
    settings = Settings(campaign_start_enabled=False)

    assert "CAMPAIGN_START_ENABLED" not in settings.campaign_enrollment_missing()
    assert "CAMPAIGN_START_ENABLED" in settings.campaign_activation_missing()
