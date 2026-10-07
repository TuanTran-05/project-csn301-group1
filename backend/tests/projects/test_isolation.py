"""Nothing may cross a project boundary: devices, changes, chat, audit, AI."""

import pytest

from fakes.fake_ai_provider import FakeAIProvider
from network_copilot.audit.service import record_event
from network_copilot.devices.model import Device
from network_copilot.extensions import db

from .conftest import scope


@pytest.fixture
def ctx(client, two_projects, alice_headers, bob_headers, ssh_factory):
    a, b, ra, rb = two_projects
    return {
        "client": client,
        "a": a, "b": b, "ra": ra, "rb": rb,
        "alice": scope(alice_headers, a),
        "bob": scope(bob_headers, b),
        # Alice naming Bob's project: must never work.
        "alice_in_b": scope(alice_headers, b),
    }


def test_same_hostname_and_ip_are_allowed_in_different_projects(ctx):
    for key in ("alice", "bob"):
        items = ctx["client"].get("/api/devices", headers=ctx[key]).get_json()["items"]
        assert [(d["hostname"], d["management_ip"]) for d in items] == [("R1", "10.10.0.1")]
    assert ctx["ra"].id != ctx["rb"].id


def test_hostname_is_still_unique_inside_a_project(ctx):
    response = ctx["client"].post(
        "/api/devices",
        headers=ctx["alice"],
        json={"hostname": "R1", "management_ip": "10.10.0.50", "device_type": "cisco_ios", "role": "core"},
    )
    assert response.status_code == 409


def test_device_listing_is_scoped(ctx):
    client = ctx["client"]
    client.post(
        "/api/devices",
        headers=ctx["alice"],
        json={"hostname": "R2", "management_ip": "10.10.0.2", "device_type": "cisco_ios", "role": "core"},
    )
    alice_hosts = {d["hostname"] for d in client.get("/api/devices", headers=ctx["alice"]).get_json()["items"]}
    bob_hosts = {d["hostname"] for d in client.get("/api/devices", headers=ctx["bob"]).get_json()["items"]}
    assert alice_hosts == {"R1", "R2"}
    assert bob_hosts == {"R1"}


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/devices/{id}"),
        ("put", "/api/devices/{id}"),
        ("delete", "/api/devices/{id}"),
        ("post", "/api/devices/{id}/test-connection"),
        ("get", "/api/devices/{id}/backups"),
        ("get", "/api/devices/{id}/status"),
        ("get", "/api/devices/{id}/snapshots"),
        ("post", "/api/devices/{id}/refresh"),
    ],
)
def test_another_projects_device_id_is_not_found(ctx, method, path):
    """Alice is inside her own project but names Bob's device id."""
    response = getattr(ctx["client"], method)(
        path.format(id=ctx["rb"].id), headers=ctx["alice"], json={}
    )
    assert response.status_code == 404


def test_readonly_command_cannot_target_another_projects_device(ctx, ssh_factory):
    ssh_factory.set_client("R1", default_output="secret output")
    response = ctx["client"].post(
        "/api/commands/execute-readonly",
        headers=ctx["alice"],
        json={"device_id": ctx["rb"].id, "command": "show ip route"},
    )
    assert response.status_code == 404
    assert ssh_factory.get("R1").calls == []


def test_command_history_is_scoped(ctx, ssh_factory):
    ssh_factory.set_client("R1", default_output="ok")
    client = ctx["client"]
    client.post(
        "/api/commands/execute-readonly",
        headers=ctx["bob"],
        json={"device_id": ctx["rb"].id, "command": "show ip route"},
    )
    assert client.get("/api/commands/history", headers=ctx["alice"]).get_json()["items"] == []
    assert len(client.get("/api/commands/history", headers=ctx["bob"]).get_json()["items"]) == 1


def test_change_preview_cannot_target_another_projects_device(ctx, admin_headers):
    response = ctx["client"].post(
        "/api/changes/preview",
        headers=scope(admin_headers, ctx["a"]),
        json={"device_id": ctx["rb"].id, "commands": ["vlan 20"]},
    )
    assert response.status_code == 404


def test_changes_are_invisible_from_another_project(ctx, admin_headers):
    client = ctx["client"]
    created = client.post(
        "/api/changes/preview",
        headers=scope(admin_headers, ctx["b"]),
        json={"device_id": ctx["rb"].id, "commands": ["vlan 20"]},
    )
    assert created.status_code == 201
    change_id = created.get_json()["id"]

    in_a = scope(admin_headers, ctx["a"])
    assert client.get("/api/changes", headers=in_a).get_json()["items"] == []
    assert client.get(f"/api/changes/{change_id}", headers=in_a).status_code == 404
    for action in ("approve", "apply", "cancel"):
        assert client.post(f"/api/changes/{change_id}/{action}", headers=in_a, json={}).status_code == 404
    # Still pending: the foreign attempts changed nothing.
    again = client.get(f"/api/changes/{change_id}", headers=scope(admin_headers, ctx["b"]))
    assert again.get_json()["status"] == "pending_approval"


WRITE_ALL = {
    "intent": "configure",
    "operations": [
        {
            "device_hostnames": ["*"],
            "execution_mode": "exec",
            "commands": ["write memory"],
            "verification_commands": [],
        }
    ],
    "explanation": "Saving every configuration.",
}


def test_wildcard_batch_only_freezes_the_current_projects_devices(ctx, admin_headers, app):
    from conftest import _create_device

    _create_device("R2", "10.10.0.2", "core", project=ctx["a"])
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=WRITE_ALL)

    response = ctx["client"].post(
        "/api/ai/chat",
        headers=scope(admin_headers, ctx["a"]),
        json={"message": "write memory on every device"},
    )
    assert response.status_code == 200
    batch = response.get_json()["batch"]
    assert batch["project_id"] == ctx["a"].id
    frozen = {child["device"]["id"] for child in batch["changes"]}
    expected = {
        d.id for d in db.session.query(Device).filter_by(project_id=ctx["a"].id)
    }
    assert len(expected) == 2
    assert frozen == expected
    assert ctx["rb"].id not in frozen


def test_batches_are_scoped_to_their_project(ctx, admin_headers, app):
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(responses=WRITE_ALL)
    client = ctx["client"]
    batch = client.post(
        "/api/ai/chat",
        headers=scope(admin_headers, ctx["b"]),
        json={"message": "write memory everywhere"},
    ).get_json()["batch"]

    in_a = scope(admin_headers, ctx["a"])
    assert client.get("/api/change-batches", headers=in_a).get_json()["items"] == []
    assert client.get(f"/api/change-batches/{batch['id']}", headers=in_a).status_code == 404
    for action in ("approve", "apply", "cancel"):
        assert client.post(f"/api/change-batches/{batch['id']}/{action}", headers=in_a, json={}).status_code == 404


def test_ai_context_contains_only_the_current_projects_devices(ctx, admin_headers, app):
    from conftest import _create_device

    _create_device("ONLY-IN-B", "10.10.0.77", "core", project=ctx["b"])
    provider = FakeAIProvider(
        responses={"intent": "chat", "operations": [], "explanation": "hi"}
    )
    app.config["AI_PROVIDER_INSTANCE"] = provider

    ctx["client"].post(
        "/api/ai/chat", headers=scope(admin_headers, ctx["a"]), json={"message": "hello"}
    )
    context = provider.prompts[0]["context"]
    assert [d["hostname"] for d in context["devices"]] == ["R1"]
    assert "ONLY-IN-B" not in str(provider.prompts[0])


def test_ai_cannot_address_a_device_of_another_project(ctx, admin_headers, app, ssh_factory):
    from conftest import _create_device

    _create_device("ONLY-IN-B", "10.10.0.77", "core", project=ctx["b"])
    ssh_factory.set_client("ONLY-IN-B", default_output="leaked")
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(
        responses={
            "intent": "monitor",
            "operations": [
                {
                    "device_hostnames": ["ONLY-IN-B"],
                    "execution_mode": "exec",
                    "commands": ["show ip route"],
                    "verification_commands": [],
                }
            ],
            "explanation": "Checking.",
        }
    )
    response = ctx["client"].post(
        "/api/ai/chat",
        headers=scope(admin_headers, ctx["a"]),
        json={"message": "show routes on ONLY-IN-B"},
    )
    assert response.status_code >= 400
    assert ssh_factory.get("ONLY-IN-B").calls == []


def test_chat_sessions_and_messages_are_scoped(ctx, app):
    client = ctx["client"]
    session = client.post("/api/chat/sessions", headers=ctx["alice"]).get_json()
    app.config["AI_PROVIDER_INSTANCE"] = FakeAIProvider(
        responses={"intent": "chat", "operations": [], "explanation": "hi"}
    )
    client.post(
        "/api/ai/chat",
        headers=ctx["alice"],
        json={"message": "private to alpha", "session_id": session["id"]},
    )

    assert client.get("/api/chat/sessions", headers=ctx["bob"]).get_json()["items"] == []
    # Bob naming Alice's session id falls back to his own (empty) transcript.
    leaked = client.get(
        f"/api/chat/messages?session_id={session['id']}", headers=ctx["bob"]
    ).get_json()["items"]
    assert leaked == []
    own = client.get(
        f"/api/chat/messages?session_id={session['id']}", headers=ctx["alice"]
    ).get_json()["items"]
    assert any(m["content"] == "private to alpha" for m in own)


def test_audit_logs_are_scoped(ctx, admin_headers):
    record_event(action="x.alpha", result="success", device_id=ctx["ra"].id)
    record_event(action="x.beta", result="success", device_id=ctx["rb"].id)
    client = ctx["client"]
    in_a = {item["action"] for item in client.get("/api/audit-logs", headers=scope(admin_headers, ctx["a"])).get_json()["items"]}
    assert "x.alpha" in in_a and "x.beta" not in in_a


def test_dashboard_counts_only_the_current_project(ctx):
    from conftest import _create_device

    _create_device("R2", "10.10.0.2", "core", project=ctx["a"])
    client = ctx["client"]
    a = client.get("/api/dashboard/summary", headers=ctx["alice"]).get_json()
    b = client.get("/api/dashboard/summary", headers=ctx["bob"]).get_json()
    assert a["devices"]["by_role"]["core"]["total"] == 2
    assert b["devices"]["by_role"]["core"]["total"] == 1


def test_topology_is_addressed_by_project_in_the_url(ctx):
    client = ctx["client"]
    assert client.get(f"/api/projects/{ctx['b'].id}/topology", headers=ctx["alice"]).status_code == 404
    assert client.get(f"/api/projects/{ctx['a'].id}/topology", headers=ctx["alice"]).status_code == 200


def test_deleting_a_project_leaves_the_other_one_working(ctx, alice_headers):
    client = ctx["client"]
    assert client.delete(f"/api/projects/{ctx['a'].id}", headers=alice_headers).status_code == 204
    assert len(client.get("/api/devices", headers=ctx["bob"]).get_json()["items"]) == 1
