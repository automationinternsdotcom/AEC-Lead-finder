from recipient_policy import assess_recipient, candidate_rank_key


def test_only_objective_address_failures_are_hard_blocks():
    invalid = assess_recipient(verification_status="invalid")
    catch_all = assess_recipient(verification_status="catch_all")
    unknown = assess_recipient(
        verification_status="unknown",
        verification_reason="email_domain_organization_mismatch_mx_valid",
    )

    assert invalid.hard_failures == ("source_email_invalid",)
    assert catch_all.eligible
    assert unknown.eligible
    assert "catch_all_address" in catch_all.quality_signals
    assert "organization_domain_mismatch" in unknown.quality_signals


def test_low_role_is_selection_preference_unless_it_is_the_only_address():
    only = assess_recipient(
        verification_status="unknown", role_score=5, alternative_email_count=1
    )
    alternative = assess_recipient(
        verification_status="unknown", role_score=5, alternative_email_count=2
    )

    assert only.eligible
    assert "sole_email_role_fallback" in only.quality_signals
    assert alternative.selection_blocks == ("recipient_role_score_below_70",)


def test_eligible_fallback_ranks_ahead_of_role_blocked_current_employer():
    current = candidate_rank_key(
        current_employer=True,
        role_score=50,
        local_scope=False,
        non_fallback_provider=True,
        verification_status="unknown",
        evidence_count=1,
        stable_id="current",
    )
    former = candidate_rank_key(
        current_employer=False,
        role_score=100,
        local_scope=True,
        non_fallback_provider=True,
        verification_status="verified",
        evidence_count=3,
        stable_id="former",
    )

    assert former < current
    assert assess_recipient(verification_status="verified", role_score=100,
                            primary=True, alternative_email_count=2).eligible


def test_current_employer_preference_remains_within_eligible_candidates():
    common = dict(role_score=90, local_scope=True, non_fallback_provider=True,
                  verification_status="unknown", evidence_count=1, stable_id="person")
    assert candidate_rank_key(current_employer=True, **common) < candidate_rank_key(current_employer=False, **common)
