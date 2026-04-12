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
