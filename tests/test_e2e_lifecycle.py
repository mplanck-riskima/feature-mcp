# tests/test_e2e_lifecycle.py
"""
In-process E2E lifecycle tests.

Uses FakeMCP (same shim as test_tools.py) for MCP tool calls and
FastAPI TestClient for REST state verification — both sharing one
FeatureStore instance, so state is always consistent.
"""
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from urllib.parse import quote

from feature_store import FeatureStore, _now_iso
from mcp_tools import register_tools
from rest_api import create_api_router


# ── FakeMCP shim ──────────────────────────────────────────────────────────

class FakeMCP:
    def __init__(self):
        self._tools: dict = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator

    def call(self, tool_name, **kwargs):
        return self._tools[tool_name](**kwargs)


# ── Shared fixture ────────────────────────────────────────────────────────

@pytest.fixture
def e2e(tmp_project):
    store = FeatureStore([str(tmp_project)])
    mcp = FakeMCP()
    register_tools(mcp, store)
    app = FastAPI()
    app.include_router(create_api_router(store), prefix="/api")
    client = TestClient(app)
    return mcp, store, client, tmp_project


def _enc(path):
    return quote(str(path), safe="")


# ── TestHappyPath ─────────────────────────────────────────────────────────

class TestHappyPath:
    def test_start_creates_feature(self, e2e):
        mcp, store, client, proj = e2e
        result = json.loads(mcp.call("feature_start",
                                     project_dir=str(proj),
                                     session_id="sess-1",
                                     name="my-feat"))
        assert result["status"] == "started"
        feat = store.get_session_feature(proj, "sess-1")
        assert feat is not None
        assert feat["name"] == "my-feat"
        assert (proj / ".claude" / "features" / "my_feat.json").exists()

    def test_milestone_recorded(self, e2e):
        mcp, store, client, proj = e2e
        mcp.call("feature_start", project_dir=str(proj), session_id="sess-1", name="my-feat")
        result = json.loads(mcp.call("feature_add_milestone",
                                     project_dir=str(proj),
                                     session_id="sess-1",
                                     text="Wired up the API"))
        assert result["status"] == "added"
        feat = store.read_feature(proj, "my-feat")
        assert len(feat["milestones"]) == 1
        assert feat["milestones"][0]["text"] == "Wired up the API"
        assert "timestamp" in feat["milestones"][0]

    def test_rest_returns_active_feature(self, e2e):
        mcp, store, client, proj = e2e
        mcp.call("feature_start", project_dir=str(proj), session_id="sess-1", name="my-feat")
        r = client.get(f"/api/projects/{_enc(proj)}/features")
        assert r.status_code == 200
        features = r.json()
        assert any(f["name"] == "my-feat" and f["status"] == "active" for f in features)

    def test_complete_writes_markdown(self, e2e):
        mcp, store, client, proj = e2e
        mcp.call("feature_start", project_dir=str(proj), session_id="sess-1", name="my-feat")
        result = json.loads(mcp.call("feature_complete",
                                     project_dir=str(proj),
                                     session_id="sess-1",
                                     summary="Built the full pipeline."))
        assert result["status"] == "completed"
        md = proj / "features" / "my-feat.md"
        assert md.exists()
        content = md.read_text()
        assert "Built the full pipeline." in content
        assert "# my-feat" in content

    def test_complete_unregisters_session(self, e2e):
        mcp, store, client, proj = e2e
        mcp.call("feature_start", project_dir=str(proj), session_id="sess-1", name="my-feat")
        mcp.call("feature_complete", project_dir=str(proj), session_id="sess-1", summary="Done.")
        assert store.get_session_feature(proj, "sess-1") is None

    def test_rest_returns_completed_feature(self, e2e):
        mcp, store, client, proj = e2e
        mcp.call("feature_start", project_dir=str(proj), session_id="sess-1", name="my-feat")
        mcp.call("feature_complete", project_dir=str(proj), session_id="sess-1", summary="Done.")
        r = client.get(f"/api/projects/{_enc(proj)}/features")
        features = r.json()
        assert any(f["name"] == "my-feat" and f["status"] == "completed" for f in features)


# ── TestConflictResolution ────────────────────────────────────────────────

class TestConflictResolution:
    def test_concurrent_start_returns_conflict(self, e2e):
        mcp, store, client, proj = e2e
        mcp.call("feature_start", project_dir=str(proj), session_id="sess-a", name="shared")
        result = json.loads(mcp.call("feature_start",
                                     project_dir=str(proj),
                                     session_id="sess-b",
                                     name="shared"))
        assert result["status"] == "conflict"
        assert result["conflicting_session_id"] == "sess-a"
        assert "recommendation" in result

    def test_force_abandons_old_session(self, e2e):
        mcp, store, client, proj = e2e
        mcp.call("feature_start", project_dir=str(proj), session_id="sess-a", name="shared")
        result = json.loads(mcp.call("feature_start",
                                     project_dir=str(proj),
                                     session_id="sess-b",
                                     name="shared",
                                     force=True))
        assert result["status"] == "started"
        assert store.get_session_feature(proj, "sess-a") is None
        feat = store.read_feature(proj, "shared")
        statuses = [s["status"] for s in feat["sessions"]]
        assert "abandoned" in statuses

    def test_forced_session_active(self, e2e):
        mcp, store, client, proj = e2e
        mcp.call("feature_start", project_dir=str(proj), session_id="sess-a", name="shared")
        mcp.call("feature_start", project_dir=str(proj), session_id="sess-b", name="shared", force=True)
        feat = store.get_session_feature(proj, "sess-b")
        assert feat is not None
        assert feat["name"] == "shared"


# ── TestResumePath ────────────────────────────────────────────────────────

class TestResumePath:
    def test_resume_completed_feature(self, e2e):
        mcp, store, client, proj = e2e
        mcp.call("feature_start", project_dir=str(proj), session_id="sess-1", name="old-feat")
        mcp.call("feature_complete", project_dir=str(proj), session_id="sess-1", summary="v1 done.")
        result = json.loads(mcp.call("feature_resume",
                                     project_dir=str(proj),
                                     session_id="sess-2",
                                     feature_name="old-feat"))
        assert result["status"] == "resumed"
        assert store.get_session_feature(proj, "sess-2")["name"] == "old-feat"

    def test_milestone_after_resume(self, e2e):
        mcp, store, client, proj = e2e
        mcp.call("feature_start", project_dir=str(proj), session_id="sess-1", name="old-feat")
        mcp.call("feature_complete", project_dir=str(proj), session_id="sess-1", summary="v1 done.")
        mcp.call("feature_resume", project_dir=str(proj), session_id="sess-2", feature_name="old-feat")
        mcp.call("feature_add_milestone", project_dir=str(proj), session_id="sess-2", text="v2 checkpoint")
        feat = store.read_feature(proj, "old-feat")
        assert any(m["text"] == "v2 checkpoint" for m in feat["milestones"])

    def test_complete_after_resume(self, e2e):
        mcp, store, client, proj = e2e
        mcp.call("feature_start", project_dir=str(proj), session_id="sess-1", name="old-feat")
        mcp.call("feature_complete", project_dir=str(proj), session_id="sess-1", summary="v1 done.")
        mcp.call("feature_resume", project_dir=str(proj), session_id="sess-2", feature_name="old-feat")
        result = json.loads(mcp.call("feature_complete",
                                     project_dir=str(proj),
                                     session_id="sess-2",
                                     summary="v2 complete."))
        assert result["status"] == "completed"
        feat = store.read_feature(proj, "old-feat")
        assert feat["status"] == "completed"
        md = proj / "features" / "old-feat.md"
        assert "v2 complete." in md.read_text()


# ── TestDiscardPath ───────────────────────────────────────────────────────

class TestDiscardPath:
    def test_discard_marks_discarded(self, e2e):
        mcp, store, client, proj = e2e
        mcp.call("feature_start", project_dir=str(proj), session_id="sess-1", name="dead-feat")
        result = json.loads(mcp.call("feature_discard",
                                     project_dir=str(proj),
                                     session_id="sess-1"))
        assert result["status"] == "discarded"
        feat = store.read_feature(proj, "dead-feat")
        assert feat["status"] == "discarded"
        assert store.get_session_feature(proj, "sess-1") is None

    def test_discard_archives_doc(self, e2e):
        mcp, store, client, proj = e2e
        mcp.call("feature_start", project_dir=str(proj), session_id="sess-1", name="dead-feat")
        md = proj / "features" / "dead-feat.md"
        md.parent.mkdir(exist_ok=True)
        md.write_text("# dead-feat\nsome content")
        mcp.call("feature_discard", project_dir=str(proj), session_id="sess-1")
        assert not md.exists()
        assert (proj / "features" / "_archived" / "dead-feat.md").exists()

    def test_rest_discard_removes_feature(self, e2e):
        mcp, store, client, proj = e2e
        # Start via MCP, discard via REST (REST endpoint deletes JSON entirely)
        mcp.call("feature_start", project_dir=str(proj), session_id="sess-1", name="dead-feat")
        r = client.post(f"/api/projects/{_enc(proj)}/sessions/sess-1/discard")
        assert r.status_code == 200
        assert r.json()["status"] == "discarded"
        r2 = client.get(f"/api/projects/{_enc(proj)}/features")
        assert r2.json() == []
