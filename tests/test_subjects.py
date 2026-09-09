from integration.subjects import project_reference
from integration.providers import WarmyClient


def test_named_property():
    assert project_reference({"why_slots": {"project": "envision 56"}, "personalized_why_line": "Envision 56 is opening in Chandler."}) == "Envision 56"


def test_generic_project_gets_sourced_location():
    assert project_reference({"why_slots": {"project": "data center", "location": "scottsdale"}, "personalized_why_line": "discussed a possible data center in Scottsdale"}) == "the data center in Scottsdale"


def test_stale_slot_does_not_override_corrected_why():
    assert project_reference({"why_slots": {"project": "wrong building"}, "personalized_why_line": "A different opportunity"}) == "your properties"


def test_expansion_fallback_without_exclusion():
    assert project_reference({"why_slots": {"company": "Aldi", "location": "phoenix"}, "personalized_why_line": "Aldi is expanding in Phoenix"}) == "your expansion in Phoenix"
    assert project_reference({}) == "your properties"


def test_provider_sends_project_field():
    assert WarmyClient._prospect_custom_fields({"project_property_name": "Envision 56"}) == {"projectPropertyName": "Envision 56"}


def test_corrected_why_supplies_reference_before_stale_slots():
    assert project_reference({"why_slots": {"project": "old name"}, "personalized_why_line": "Hi Mike, I saw plans moving forward for Trax at Cooley Station in Gilbert."}) == "Trax at Cooley Station"
    assert project_reference({"personalized_why_line": "PetSmart opened its 1,700th store in Chandler."}) == "PetSmart's store in Chandler"
