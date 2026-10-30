"""Remembered PNETLab login: typed once per project, never shown again."""

import pytest

from network_copilot.audit.model import AuditLog
from network_copilot.credentials.model import ProjectPnetlabLogin
from network_copilot.extensions import db

from integrations.test_pnetlab import NODES, SOURCE, TOPOLOGY

SEEN = []


class RecordingLab:
    def __init__(self, params):
        SEEN.append(dict(params))

    def nodes(self, lab):
        return NODES

    def topology(self, lab):
        return TOPOLOGY


@pytest.fixture
def lab(app):
    SEEN.clear()
    app.config["PNETLAB_CLIENT_FACTORY"] = RecordingLab
    return SEEN


@pytest.fixture
def urls(project):
    base = f"/api/projects/{project.id}/topology"
    return {"preview": f"{base}/import/pnetlab/preview", "import": f"{base}/import/pnetlab",
            "login": f"{base}/pnetlab-login"}


def test_nothing_is_saved_unless_asked(client, admin_headers, urls, lab):
    assert client.post(urls["preview"], headers=admin_headers, json=SOURCE).status_code == 200
    assert client.get(urls["login"], headers=admin_headers).get_json()["saved"] is None


def test_a_working_login_is_remembered_and_reused(client, admin_headers, urls, lab):
    response = client.post(urls["preview"], headers=admin_headers, json={**SOURCE, "remember": True})
    assert response.status_code == 200
    saved = client.get(urls["login"], headers=admin_headers)
    assert saved.get_json()["saved"] == {
        "url": "http://172.16.0.5", "username": "admin", "lab": "/Course/Lab1.unl",
        "api_mode": "auto", "verify_tls": True, "has_password": True,
        "updated_at": saved.get_json()["saved"]["updated_at"],
    }
    assert "pnet-pass" not in saved.get_data(as_text=True)
    assert "pnet-pass" not in db.session.query(ProjectPnetlabLogin).one().password_encrypted

    # Next time only the address and account are sent; the saved password is used.
    lab.clear()
    body = {k: v for k, v in SOURCE.items() if k != "password"}
    assert client.post(urls["preview"], headers=admin_headers, json=body).status_code == 200
    assert lab[0]["password"] == "pnet-pass"
    # Even an empty form works: everything comes from the saved login.
    lab.clear()
    imported = client.post(urls["import"], headers=admin_headers, json={"nodes": [{"id": "1"}]})
    assert imported.status_code == 200, imported.get_json()
    assert (lab[0]["url"], lab[0]["username"], lab[0]["password"]) == ("http://172.16.0.5", "admin", "pnet-pass")


def test_the_saved_password_is_never_sent_to_another_server_or_account(client, admin_headers, urls, lab):
    client.post(urls["preview"], headers=admin_headers, json={**SOURCE, "remember": True})
    lab.clear()
    other = {**SOURCE, "url": "http://172.16.0.9"}
    del other["password"]
    assert client.post(urls["preview"], headers=admin_headers, json=other).status_code == 422
    other = {**SOURCE, "username": "someone"}
    del other["password"]
    assert client.post(urls["preview"], headers=admin_headers, json=other).status_code == 422
    assert lab == []


def test_a_typed_password_wins_and_forgetting_removes_it(client, admin_headers, urls, lab):
    client.post(urls["preview"], headers=admin_headers, json={**SOURCE, "remember": True})
    lab.clear()
    client.post(urls["preview"], headers=admin_headers, json={**SOURCE, "password": "typed-now"})
    assert lab[0]["password"] == "typed-now"
    assert client.delete(urls["login"], headers=admin_headers).get_json()["saved"] is None
    body = {k: v for k, v in SOURCE.items() if k != "password"}
    assert client.post(urls["preview"], headers=admin_headers, json=body).status_code == 422


def test_viewers_cannot_read_or_forget_it(client, admin_headers, viewer_headers, urls, lab):
    client.post(urls["preview"], headers=admin_headers, json={**SOURCE, "remember": True})
    assert client.get(urls["login"], headers=viewer_headers).status_code == 403
    assert client.delete(urls["login"], headers=viewer_headers).status_code == 403


def test_the_password_never_reaches_the_audit_log(client, admin_headers, urls, lab):
    client.post(urls["preview"], headers=admin_headers, json={**SOURCE, "remember": True})
    client.post(urls["import"], headers=admin_headers, json={"nodes": [{"id": "1"}]})
    logged = " ".join(str(row.details) for row in db.session.query(AuditLog))
    assert "pnet-pass" not in logged
