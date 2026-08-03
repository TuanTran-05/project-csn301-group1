import pytest

from network_copilot.changes import service as change_service
from network_copilot.changes.capabilities import assess_change


def test_preview_freezes_capability_metadata(app, admin_user, access_switch):
    change = change_service.create_preview(
        admin_user.id,
        access_switch.id,
        ["interface Gi0/2", "description STUDENT"],
    )

    payload = change.to_dict()
    assert payload["capability_tier"] == "best_effort"
    assert payload["verification_level"] == "best_effort"
    assert payload["operation_families"] == []
    assert payload["operation_expectations"] == []
    assert payload["verification_plan"] == []
    assert payload["rollback_guidance"] == []


def test_initial_assessment_is_best_effort(app):
    assessment = assess_change(
        ["vlan 30", "name STUDENT"], "config", "cisco_ios"
    )
    assert assessment.capability_tier == "best_effort"
    assert assessment.verification_level == "best_effort"
    assert assessment.expectations == ()
