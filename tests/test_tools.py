# tests/test_tools.py
import json
import pytest
from pathlib import Path
from feature_store import FeatureStore, _now_iso
from mcp_tools import register_tools


class FakeMCP:
    """Captures tool registrations so we can call them directly in tests."""
    def __init__(self):
        self._tools: dict = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator

    def call(self, tool_name, **kwargs):
        return self._tools[tool_name](**kwargs)


@pytest.fixture
def mcp_fixture(tmp_project, store):
    mcp = FakeMCP()
    register_tools(mcp, store)
    return mcp, store, tmp_project


def _active_feature(name, session_id):
    now = _now_iso()
    return {
        "name": name, "status": "active", "session_id": session_id,
        "sessions": [{"session_id": session_id, "session_start": now,
                       "source": "cli", "status": "active"}],
        "milestones": [], "started_at": now, "completed_at": None,
        "total_cost_usd": 0.0, "total_input_tokens": 0, "total_output_tokens": 0,
    }


# --- feature_context ---

def test_feature_context_no_active(mcp_fixture, tmp_project):
    mcp, store, _ = mcp_fixture
    result = json.loads(mcp.call("feature_context",
                                  project_dir=str(tmp_project), session_id="sess-x"))
    assert result["active_feature"] is None
    assert result["all_features"] == []


def test_feature_context_with_active(mcp_fixture, tmp_project, store):
    mcp, store, _ = mcp_fixture
    data = _active_feature("my-feat", "sess-1")
    store.write_feature(tmp_project, "my-feat", data)
    store.register_session(tmp_project, "sess-1", "my-feat")
    result = json.loads(mcp.call("feature_context",
                                  project_dir=str(tmp_project), session_id="sess-1"))
    assert result["active_feature"]["name"] == "my-feat"


# --- feature_list ---

def test_feature_list_returns_summary(mcp_fixture, tmp_project, store):
    mcp, store, _ = mcp_fixture
    data = _active_feature("feat-a", "sess-1")
    store.write_feature(tmp_project, "feat-a", data)
    result = json.loads(mcp.call("feature_list", project_dir=str(tmp_project)))
    assert len(result) == 1
    assert result[0]["name"] == "feat-a"
    assert "total_cost_usd" in result[0]


# --- feature_start ---

def test_feature_start_creates_feature(mcp_fixture, tmp_project, store):
    mcp, store, _ = mcp_fixture
    result = json.loads(mcp.call("feature_start",
                                  project_dir=str(tmp_project),
                                  session_id="sess-1", name="new-feat"))
    assert result["status"] == "started"
    assert store.get_session_feature(tmp_project, "sess-1")["name"] == "new-feat"


def test_feature_start_conflict_returns_warning(mcp_fixture, tmp_project, store):
    mcp, store, _ = mcp_fixture
    data = _active_feature("shared-feat", "sess-existing")
    store.write_feature(tmp_project, "shared-feat", data)
    store.register_session(tmp_project, "sess-existing", "shared-feat")
    result = json.loads(mcp.call("feature_start",
                                  project_dir=str(tmp_project),
                                  session_id="sess-new", name="shared-feat"))
    assert result["status"] == "conflict"
    assert "conflicting_session_id" in result
    assert "recommendation" in result


def test_feature_start_force_abandons_old_session(mcp_fixture, tmp_project, store):
    mcp, store, _ = mcp_fixture
    data = _active_feature("shared-feat", "sess-old")
    store.write_feature(tmp_project, "shared-feat", data)
    store.register_session(tmp_project, "sess-old", "shared-feat")
    result = json.loads(mcp.call("feature_start",
                                  project_dir=str(tmp_project),
                                  session_id="sess-new", name="shared-feat", force=True))
    assert result["status"] == "started"
    assert store.get_session_feature(tmp_project, "sess-old") is None
    feat = store.read_feature(tmp_project, "shared-feat")
    statuses = [s["status"] for s in feat["sessions"]]
    assert "abandoned" in statuses


def test_feature_start_autocompletes_previous(mcp_fixture, tmp_project, store):
    mcp, store, _ = mcp_fixture
    data = _active_feature("old-feat", "sess-1")
    store.write_feature(tmp_project, "old-feat", data)
    store.register_session(tmp_project, "sess-1", "old-feat")
    mcp.call("feature_start", project_dir=str(tmp_project),
              session_id="sess-1", name="new-feat")
    old = store.read_feature(tmp_project, "old-feat")
    assert old["status"] == "completed"


# --- feature_resume ---

def test_feature_resume_no_conflict(mcp_fixture, tmp_project, store):
    mcp, store, _ = mcp_fixture
    now = _now_iso()
    data = {"name": "old-feat", "status": "completed", "session_id": None,
            "sessions": [], "milestones": [], "started_at": now, "completed_at": now,
            "total_cost_usd": 0.0, "total_input_tokens": 0, "total_output_tokens": 0}
    store.write_feature(tmp_project, "old-feat", data)
    result = json.loads(mcp.call("feature_resume",
                                  project_dir=str(tmp_project),
                                  session_id="sess-new", feature_name="old-feat"))
    assert result["status"] == "resumed"
    assert store.get_session_feature(tmp_project, "sess-new")["name"] == "old-feat"


def test_feature_resume_conflict_requires_force(mcp_fixture, tmp_project, store):
    mcp, store, _ = mcp_fixture
    data = _active_feature("live-feat", "sess-live")
    store.write_feature(tmp_project, "live-feat", data)
    store.register_session(tmp_project, "sess-live", "live-feat")
    result = json.loads(mcp.call("feature_resume",
                                  project_dir=str(tmp_project),
                                  session_id="sess-other", feature_name="live-feat"))
    assert result["status"] == "conflict"


# --- feature_complete ---

def test_feature_complete_writes_markdown(mcp_fixture, tmp_project, store):
    mcp, store, _ = mcp_fixture
    data = _active_feature("done-feat", "sess-1")
    store.write_feature(tmp_project, "done-feat", data)
    store.register_session(tmp_project, "sess-1", "done-feat")
    result = json.loads(mcp.call("feature_complete",
                                  project_dir=str(tmp_project),
                                  session_id="sess-1", summary="Built the thing."))
    assert result["status"] == "completed"
    md_path = tmp_project / "features" / "done-feat.md"
    assert md_path.exists()
    content = md_path.read_text()
    assert "Built the thing." in content
    assert "# done-feat" in content


def test_feature_complete_unregisters_session(mcp_fixture, tmp_project, store):
    mcp, store, _ = mcp_fixture
    data = _active_feature("done-feat", "sess-1")
    store.write_feature(tmp_project, "done-feat", data)
    store.register_session(tmp_project, "sess-1", "done-feat")
    mcp.call("feature_complete", project_dir=str(tmp_project),
              session_id="sess-1", summary="Done.")
    assert store.get_session_feature(tmp_project, "sess-1") is None


def test_feature_complete_no_active(mcp_fixture, tmp_project):
    mcp, store, _ = mcp_fixture
    result = json.loads(mcp.call("feature_complete",
                                  project_dir=str(tmp_project),
                                  session_id="sess-nobody", summary="x"))
    assert "error" in result


# --- feature_discard ---

def test_feature_discard_archives_markdown(mcp_fixture, tmp_project, store):
    mcp, store, _ = mcp_fixture
    data = _active_feature("dead-feat", "sess-1")
    store.write_feature(tmp_project, "dead-feat", data)
    store.register_session(tmp_project, "sess-1", "dead-feat")
    md = tmp_project / "features" / "dead-feat.md"
    md.parent.mkdir(exist_ok=True)
    md.write_text("# dead-feat\nsome content")
    result = json.loads(mcp.call("feature_discard",
                                  project_dir=str(tmp_project), session_id="sess-1"))
    assert result["status"] == "discarded"
    assert not md.exists()
    assert (tmp_project / "features" / "_archived" / "dead-feat.md").exists()


# --- feature_add_milestone ---

def test_feature_add_milestone(mcp_fixture, tmp_project, store):
    mcp, store, _ = mcp_fixture
    data = _active_feature("wip-feat", "sess-1")
    store.write_feature(tmp_project, "wip-feat", data)
    store.register_session(tmp_project, "sess-1", "wip-feat")
    result = json.loads(mcp.call("feature_add_milestone",
                                  project_dir=str(tmp_project),
                                  session_id="sess-1", text="Wired up the pipeline"))
    assert result["status"] == "added"
    feat = store.read_feature(tmp_project, "wip-feat")
    assert len(feat["milestones"]) == 1
    assert feat["milestones"][0]["text"] == "Wired up the pipeline"
    assert "timestamp" in feat["milestones"][0]
