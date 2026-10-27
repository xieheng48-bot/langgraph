from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from it_app.graph import default_ticket_tool
from it_app.main import create_app


ALICE = {"Authorization": "Bearer demo-alice"}
BOB = {"Authorization": "Bearer demo-bob"}
IT = {"Authorization": "Bearer demo-it"}


@pytest.fixture
def db(tmp_path):
    return tmp_path / "service.db"


@pytest.fixture
def client(db):
    with TestClient(create_app(db, model_mode="offline")) as test_client:
        yield test_client


def report(client, **changes):
    payload = {"description": "Company Wi-Fi connection failed", "device_id": "LAPTOP-A01", **changes}
    response = client.post("/api/runs", headers=ALICE, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def decide(client, run, decision="approve", **changes):
    return client.post(f"/api/runs/{run['run_id']}/approval", headers=IT, json={"decision": decision, "version": run["version"], "digest": run["digest"], **changes})


def test_identity_and_device_access(client):
    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/runs", headers={"Authorization": "Bearer alice"}).status_code == 401
    assert [item["id"] for item in client.get("/api/devices", headers=ALICE).json()] == ["LAPTOP-A01"]
    denied = client.post("/api/runs", headers=ALICE, json={"description": "Device not working", "device_id": "LAPTOP-B01", "owner": "bob"})
    assert denied.status_code == 403
    assert client.post("/api/runs", headers=IT, json={"description": "Device broken"}).status_code == 403
    assert client.post("/api/runs", headers=ALICE, json={"device_id": "UNKNOWN"}).status_code == 404


def test_missing_information_pauses_and_collects(client):
    run = report(client, description="", device_id=None)
    assert run["status"] == "collecting"
    assert run["pending"][0]["type"] == "missing_fields"
    assert set(run["pending"][0]["required"]) == {"description", "device_id"}
    path = f"/api/runs/{run['run_id']}/details"
    assert client.post(path, headers=ALICE, json={"description": "x"}).status_code == 422
    assert client.post(path, headers=BOB, json={"description": "Wi-Fi broken", "device_id": "LAPTOP-A01"}).status_code == 403
    assert client.post(path, headers=ALICE, json={"description": "Wi-Fi broken", "device_id": "LAPTOP-B01"}).status_code == 403
    response = client.post(path, headers=ALICE, json={"description": "Wi-Fi broken", "device_id": "LAPTOP-A01"})
    assert response.status_code == 200
    updated = response.json()
    assert updated["status"] == "waiting_approval"
    assert updated["pending"][0]["type"] == "approval"
    assert updated["ticket"] is None
    assert client.post(path, headers=ALICE, json={}).status_code == 409


def test_owner_is_authenticated_not_request_body(client):
    run = report(client, owner="bob", approved_by="it")
    assert run["owner"] == "alice"
    assert run["approved_by"] is None
    assert client.get(f"/api/runs/{run['run_id']}", headers=BOB).status_code == 403
    assert client.get("/api/runs", headers=BOB).json() == []
    denied = client.post(f"/api/runs/{run['run_id']}/approval", headers=ALICE, json={"decision": "approve", "version": run["version"], "digest": run["digest"], "reviewer": "it"})
    assert denied.status_code == 403


def test_approval_creates_verified_ticket_once(client):
    run = report(client)
    assert run["model_mode"] == "offline"
    assert run["proposal"]["category"] == "network"
    assert "SOP-NET-001" in run["proposal"]["citation_ids"]
    first = decide(client, run)
    assert first.status_code == 200, first.text
    completed = first.json()
    assert completed["status"] == "completed"
    assert completed["ticket"]["owner"] == "alice"
    assert completed["ticket"]["proposal_digest"] == run["digest"]
    second = decide(client, run)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["ticket"]["id"] == completed["ticket"]["id"]
    assert [item["event"] for item in second.json()["audit"]].count("ticket_created") == 1


def test_rejection_never_creates_ticket(client):
    run = report(client)
    rejected = decide(client, run, "reject", reason="Need more investigation")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["ticket"] is None
    assert decide(client, run, "reject").json()["ticket"] is None
    assert decide(client, run, "approve").status_code == 409
    assert client.post(f"/api/runs/{run['run_id']}/retry", headers=IT).status_code == 409


@pytest.mark.parametrize("changes", [{"version": 999}, {"digest": "0" * 64}])
def test_stale_confirmation_is_rejected(client, changes):
    run = report(client)
    assert decide(client, run, **changes).status_code == 409
    current = client.get(f"/api/runs/{run['run_id']}", headers=ALICE).json()
    assert current["status"] == "waiting_approval"
    assert current["ticket"] is None


def test_pending_approval_survives_database_reopen(db):
    with TestClient(create_app(db, model_mode="offline")) as first:
        run = report(first)
    with TestClient(create_app(db, model_mode="offline")) as second:
        restored = second.get(f"/api/runs/{run['run_id']}", headers=IT).json()
        assert restored["digest"] == run["digest"]
        assert restored["pending"][0]["type"] == "approval"
        approved = decide(second, run)
        assert approved.status_code == 200
        assert approved.json()["status"] == "completed"


def test_missing_information_survives_database_reopen(db):
    with TestClient(create_app(db, model_mode="offline")) as first:
        run = report(first, device_id=None)
    with TestClient(create_app(db, model_mode="offline")) as second:
        resumed = second.post(f"/api/runs/{run['run_id']}/details", headers=ALICE, json={"device_id": "LAPTOP-A01"})
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "waiting_approval"


def test_failure_keeps_approval_and_retries_safely(db):
    attempts = []

    def flaky_tool(repo, state):
        attempts.append(state["run_id"])
        if len(attempts) == 1:
            raise RuntimeError("upstream unavailable with sensitive internals")
        return default_ticket_tool(repo, state)

    with TestClient(create_app(db, model_mode="offline", ticket_tool=flaky_tool)) as client:
        run = report(client)
        failed = decide(client, run).json()
        assert failed["status"] == "tool_failed"
        assert failed["ticket"] is None
        assert "sensitive" not in failed["error"]
        assert decide(client, run).json()["status"] == "tool_failed"
        assert len(attempts) == 1
        assert client.post(f"/api/runs/{run['run_id']}/retry", headers=ALICE).status_code == 403
        retried = client.post(f"/api/runs/{run['run_id']}/retry", headers=IT)
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "completed"
        assert len(attempts) == 2


def test_side_effect_committed_before_tool_error_is_not_duplicated(db):
    def interrupted_tool(repo, state):
        default_ticket_tool(repo, state)
        raise RuntimeError("crashed after the ticket transaction committed")

    with TestClient(create_app(db, model_mode="offline", ticket_tool=interrupted_tool)) as first:
        run = report(first)
        failed = decide(first, run).json()
        assert failed["status"] == "tool_failed"
        ticket_id = failed["ticket"]["id"]
    with TestClient(create_app(db, model_mode="offline")) as second:
        retried = second.post(f"/api/runs/{run['run_id']}/retry", headers=IT).json()
        assert retried["status"] == "completed"
        assert retried["ticket"]["id"] == ticket_id
        assert [event["event"] for event in retried["audit"]].count("ticket_created") == 1


def test_concurrent_confirmation_is_idempotent(client):
    run = report(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: decide(client, run), range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert len({response.json()["ticket"]["id"] for response in responses}) == 1


def test_resume_unfinished_approved_checkpoint_after_reopen(db):
    app = create_app(db, model_mode="offline")
    with TestClient(app) as first:
        run = report(first)
        runtime = app.state.runtime
        with runtime.lock:
            # 模拟审批事务已提交、下一节点尚未执行就重启的边界。
            runtime.repo.decide(run["run_id"], "approve", run["version"], run["digest"], "it", "")
            runtime.graph.update_state(runtime.config(run["run_id"]), {"status": "approved"}, as_node="await_approval")
    with TestClient(create_app(db, model_mode="offline")) as second:
        path = f"/api/runs/{run['run_id']}/resume"
        assert second.post(path, headers=ALICE).status_code == 403
        resumed = second.post(path, headers=IT)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "completed"
        assert second.post(path, headers=IT).status_code == 409


@pytest.mark.parametrize("device_id,expected", [("LAPTOP-A01", "approval"), (None, "missing_fields")])
def test_run_committed_before_first_checkpoint_can_recover(db, device_id, expected):
    app = create_app(db, model_mode="offline")
    with TestClient(app):
        runtime = app.state.runtime
        with runtime.lock:
            run_id = runtime.repo.create_run("alice", "Wi-Fi unavailable", device_id)
            assert runtime.saver.get_tuple(runtime.config(run_id)) is None
    with TestClient(create_app(db, model_mode="offline")) as client:
        viewed = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
        assert viewed["status"] == "collecting"
        assert viewed["can_resume"] is True
        assert client.post(f"/api/runs/{run_id}/resume", headers=BOB).status_code == 403
        resumed = client.post(f"/api/runs/{run_id}/resume", headers=ALICE)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["pending"][0]["type"] == expected
        assert resumed.json()["ticket"] is None
        assert client.post(f"/api/runs/{run_id}/resume", headers=ALICE).status_code == 409


@pytest.mark.parametrize("status", ["rejected", "completed", "model_failed", "tool_failed"])
def test_checkpointless_non_initial_status_cannot_restart_graph(db, status):
    app = create_app(db, model_mode="offline")
    with TestClient(app) as client:
        runtime = app.state.runtime
        with runtime.lock:
            run_id = runtime.repo.create_run("alice", "Wi-Fi unavailable", "LAPTOP-A01")
            runtime.repo.status(run_id, status)
        viewed = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
        assert viewed["can_resume"] is False
        assert client.post(f"/api/runs/{run_id}/resume", headers=IT).status_code == 409
        assert runtime.saver.get_tuple(runtime.config(run_id)) is None
