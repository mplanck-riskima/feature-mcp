# tests/test_rest_api.py
import json
import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI
from feature_store import FeatureStore, _now_iso
from rest_api import create_api_router
from urllib.parse import quote


def _encode(path):
    return quote(str(path), safe="")


@pytest.fixture
def client(tmp_project):
    store = FeatureStore([str(tmp_project)])
    app = FastAPI()
    app.include_router(create_api_router(store), prefix="/api")
    return TestClient(app), store, tmp_project


def test_get_features_empty(client):
    c, store, tmp_project = client
    r = c.get(f"/api/projects/{_encode(tmp_project)}/features")
    assert r.status_code == 200
    assert r.json() == []


def test_get_features_unknown_project(client):
    c, store, tmp_project = client
    r = c.get(f"/api/projects/{_encode('/nonexistent')}/features")
    assert r.status_code == 404


def test_post_cost_no_active_feature(client):
    c, store, tmp_project = client
    r = c.post(
        f"/api/projects/{_encode(tmp_project)}/sessions/sess-x/cost",
        json={"cost_usd": 0.1, "input_tokens": 100, "output_tokens": 10},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "no_active_feature"


def test_post_cost_accumulates(client):
    c, store, tmp_project = client
    now = _now_iso()
    data = {
        "name": "feat", "status": "active", "session_id": "sess-1",
        "sessions": [{"session_id": "sess-1", "session_start": now,
                       "source": "cli", "status": "active"}],
        "milestones": [], "started_at": now, "completed_at": None,
        "total_cost_usd": 0.0, "total_input_tokens": 0, "total_output_tokens": 0,
    }
    store.write_feature(tmp_project, "feat", data)
    store.register_session(tmp_project, "sess-1", "feat")
    r = c.post(
        f"/api/projects/{_encode(tmp_project)}/sessions/sess-1/cost",
        json={"cost_usd": 0.5, "input_tokens": 500, "output_tokens": 50},
    )
    assert r.status_code == 200
    updated = store.read_feature(tmp_project, "feat")
    assert updated["total_cost_usd"] == pytest.approx(0.5)
    assert updated["total_input_tokens"] == 500


def test_get_features_returns_list(client):
    c, store, tmp_project = client
    now = _now_iso()
    store.write_feature(tmp_project, "feat-a", {
        "name": "feat-a", "status": "active", "sessions": [], "milestones": [],
        "started_at": now, "completed_at": None,
        "total_cost_usd": 0.0, "total_input_tokens": 0, "total_output_tokens": 0,
    })
    r = c.get(f"/api/projects/{_encode(tmp_project)}/features")
    assert r.status_code == 200
    assert r.json()[0]["name"] == "feat-a"


# ── helpers ────────────────────────────────────────────────────────────────
def _started_fixture(store, tmp_project, session_id="sess-a", name="e2e-feat"):
    """Helper: start a feature via store directly (so tests don't depend on REST start)."""
    now = _now_iso()
    data = {
        "name": name, "status": "active", "session_id": session_id,
        "sessions": [{"session_id": session_id, "session_start": now,
                       "source": "rest", "status": "active"}],
        "milestones": [], "started_at": now, "completed_at": None,
        "total_cost_usd": 0.0, "total_input_tokens": 0, "total_output_tokens": 0,
        "prompt_count": 0,
    }
    store.write_feature(tmp_project, name, data)
    store.register_session(tmp_project, session_id, name)
    return data


# ── POST /api/projects ─────────────────────────────────────────────────────
def test_register_project_new(client, tmp_path):
    c, store, tmp_project = client
    new_dir = tmp_path / "new_proj"
    new_dir.mkdir()
    r = c.post("/api/projects", json={"project_dir": str(new_dir)})
    assert r.status_code == 200
    assert r.json()["status"] == "registered"
    assert (new_dir / ".claude" / "features").exists()
    # now recognized by store
    r2 = c.get(f"/api/projects/{_encode(new_dir)}/features")
    assert r2.status_code == 200


def test_register_project_idempotent(client):
    c, store, tmp_project = client
    r1 = c.post("/api/projects", json={"project_dir": str(tmp_project)})
    r2 = c.post("/api/projects", json={"project_dir": str(tmp_project)})
    assert r1.status_code == 200
    assert r2.status_code == 200


# ── POST .../features/{name}/start ────────────────────────────────────────
def test_rest_start_creates_feature(client):
    c, store, tmp_project = client
    r = c.post(
        f"/api/projects/{_encode(tmp_project)}/features/my-feat/start",
        json={"session_id": "sess-1"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "started"
    assert store.get_session_feature(tmp_project, "sess-1")["name"] == "my-feat"


def test_rest_start_conflict(client):
    c, store, tmp_project = client
    _started_fixture(store, tmp_project, "sess-existing", "shared")
    r = c.post(
        f"/api/projects/{_encode(tmp_project)}/features/shared/start",
        json={"session_id": "sess-new"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "conflict"
    assert "conflicting_session_id" in r.json()


def test_rest_start_force(client):
    c, store, tmp_project = client
    _started_fixture(store, tmp_project, "sess-old", "shared")
    r = c.post(
        f"/api/projects/{_encode(tmp_project)}/features/shared/start",
        json={"session_id": "sess-new", "force": True},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "started"
    assert store.get_session_feature(tmp_project, "sess-old") is None


def test_rest_start_unknown_project(client):
    c, store, tmp_project = client
    r = c.post(
        f"/api/projects/{_encode('/nonexistent')}/features/x/start",
        json={"session_id": "s"},
    )
    assert r.status_code == 404


# ── POST .../sessions/{id}/resume ─────────────────────────────────────────
def test_rest_resume_feature(client):
    c, store, tmp_project = client
    _started_fixture(store, tmp_project, "sess-1", "my-feat")
    # complete it first so it can be resumed
    feat = store.get_session_feature(tmp_project, "sess-1")
    feat["status"] = "completed"
    feat["completed_at"] = _now_iso()
    store.write_feature(tmp_project, "my-feat", feat)
    store.unregister_session("sess-1")

    r = c.post(
        f"/api/projects/{_encode(tmp_project)}/sessions/sess-2/resume",
        json={"feature_name": "my-feat"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "resumed"
    assert store.get_session_feature(tmp_project, "sess-2")["name"] == "my-feat"


def test_rest_resume_not_found(client):
    c, store, tmp_project = client
    r = c.post(
        f"/api/projects/{_encode(tmp_project)}/sessions/sess-1/resume",
        json={"feature_name": "ghost"},
    )
    assert r.status_code == 200
    assert "error" in r.json()


# ── POST .../sessions/{id}/complete ───────────────────────────────────────
def test_rest_complete_writes_markdown(client):
    c, store, tmp_project = client
    _started_fixture(store, tmp_project, "sess-1", "done-feat")
    r = c.post(
        f"/api/projects/{_encode(tmp_project)}/sessions/sess-1/complete",
        json={"summary": "Built the thing."},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "completed"
    md = tmp_project / "features" / "done-feat.md"
    assert md.exists()
    assert "Built the thing." in md.read_text()


def test_rest_complete_unregisters_session(client):
    c, store, tmp_project = client
    _started_fixture(store, tmp_project, "sess-1", "done-feat")
    c.post(
        f"/api/projects/{_encode(tmp_project)}/sessions/sess-1/complete",
        json={"summary": "Done."},
    )
    assert store.get_session_feature(tmp_project, "sess-1") is None


def test_rest_complete_no_active(client):
    c, store, tmp_project = client
    r = c.post(
        f"/api/projects/{_encode(tmp_project)}/sessions/nobody/complete",
        json={"summary": "x"},
    )
    assert r.status_code == 200
    assert "error" in r.json()


# ── POST .../sessions/{id}/discard ────────────────────────────────────────
def test_rest_discard_deletes_json(client):
    c, store, tmp_project = client
    _started_fixture(store, tmp_project, "sess-1", "dead-feat")
    r = c.post(f"/api/projects/{_encode(tmp_project)}/sessions/sess-1/discard")
    assert r.status_code == 200
    assert r.json()["status"] == "discarded"
    # JSON file removed → GET /features returns empty
    r2 = c.get(f"/api/projects/{_encode(tmp_project)}/features")
    assert r2.json() == []


def test_rest_discard_archives_markdown(client):
    c, store, tmp_project = client
    _started_fixture(store, tmp_project, "sess-1", "dead-feat")
    md = tmp_project / "features" / "dead-feat.md"
    md.parent.mkdir(exist_ok=True)
    md.write_text("# dead-feat\nsome content")
    c.post(f"/api/projects/{_encode(tmp_project)}/sessions/sess-1/discard")
    assert not md.exists()
    assert (tmp_project / "features" / "_archived" / "dead-feat.md").exists()


# ── POST .../sessions/{id}/milestone ──────────────────────────────────────
def test_rest_milestone_added(client):
    c, store, tmp_project = client
    _started_fixture(store, tmp_project, "sess-1", "wip")
    r = c.post(
        f"/api/projects/{_encode(tmp_project)}/sessions/sess-1/milestone",
        json={"text": "reached checkpoint"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "added"
    feat = store.read_feature(tmp_project, "wip")
    assert feat["milestones"][0]["text"] == "reached checkpoint"


def test_rest_milestone_no_active(client):
    c, store, tmp_project = client
    r = c.post(
        f"/api/projects/{_encode(tmp_project)}/sessions/nobody/milestone",
        json={"text": "x"},
    )
    assert r.status_code == 200
    assert "error" in r.json()
