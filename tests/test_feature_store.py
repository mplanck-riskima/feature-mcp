# tests/test_feature_store.py
import json
import pytest
from pathlib import Path
from feature_store import to_snake, _atomic_write


def test_to_snake_basic():
    assert to_snake("my-feature") == "my_feature"

def test_to_snake_ampersand():
    assert to_snake("Bugs & Fixes") == "bugs_and_fixes"

def test_to_snake_mixed():
    assert to_snake("Star-trek-personas") == "star_trek_personas"

def test_to_snake_empty():
    assert to_snake("") == "unnamed"

def test_to_snake_special_chars():
    assert to_snake("feat!@#$") == "feat"

def test_atomic_write_creates_file(tmp_path):
    path = tmp_path / "test.json"
    _atomic_write(path, {"key": "value"})
    assert json.loads(path.read_text()) == {"key": "value"}

def test_atomic_write_no_tmp_left(tmp_path):
    path = tmp_path / "test.json"
    _atomic_write(path, {"key": "value"})
    assert not (tmp_path / "test.tmp").exists()


# --- Task 3: File I/O tests ---

def test_write_and_read_feature(store, tmp_project):
    data = {"name": "my-feature", "status": "active", "sessions": []}
    store.write_feature(tmp_project, "my-feature", data)
    result = store.read_feature(tmp_project, "my-feature")
    assert result["name"] == "my-feature"
    assert result["status"] == "active"

def test_read_missing_feature_returns_none(store, tmp_project):
    assert store.read_feature(tmp_project, "nonexistent") is None

def test_list_features_returns_all(store, tmp_project):
    store.write_feature(tmp_project, "feat-a", {"name": "feat-a", "status": "active"})
    store.write_feature(tmp_project, "feat-b", {"name": "feat-b", "status": "completed"})
    features = store.list_features(tmp_project)
    names = [f["name"] for f in features]
    assert "feat-a" in names
    assert "feat-b" in names

def test_feature_path_uses_snake_case(store, tmp_project):
    store.write_feature(tmp_project, "My Feature", {"name": "My Feature", "status": "active"})
    path = tmp_project / ".claude" / "features" / "my_feature.json"
    assert path.exists()

def test_ensure_project_dir_rejects_unknown(store):
    with pytest.raises(ValueError, match="Unknown project"):
        store.ensure_project_dir("/nonexistent/path")

# --- Task 4: Session routing tests ---

def test_register_and_get_session_feature(store, tmp_project):
    store.write_feature(tmp_project, "feat-a", {"name": "feat-a", "status": "active", "sessions": []})
    store.register_session(tmp_project, "sess-1", "feat-a")
    result = store.get_session_feature(tmp_project, "sess-1")
    assert result["name"] == "feat-a"

def test_unregister_session(store, tmp_project):
    store.write_feature(tmp_project, "feat-a", {"name": "feat-a", "status": "active", "sessions": []})
    store.register_session(tmp_project, "sess-1", "feat-a")
    store.unregister_session("sess-1")
    assert store.get_session_feature(tmp_project, "sess-1") is None

def test_get_active_session_for_feature(store, tmp_project):
    store.write_feature(tmp_project, "feat-a", {"name": "feat-a", "status": "active", "sessions": []})
    store.register_session(tmp_project, "sess-1", "feat-a")
    assert store.get_active_session_for_feature(tmp_project, "feat-a") == "sess-1"

def test_startup_rebuilds_routing(tmp_project):
    from feature_store import FeatureStore, _now_iso
    now = _now_iso()
    data = {
        "name": "feat-a",
        "status": "active",
        "session_id": "sess-rebuilt",
        "sessions": [{"session_id": "sess-rebuilt", "session_start": now, "source": "cli", "status": "active"}],
        "milestones": [], "started_at": now, "completed_at": None,
        "total_cost_usd": 0.0, "total_input_tokens": 0, "total_output_tokens": 0,
    }
    store2 = FeatureStore([str(tmp_project)])
    store2.write_feature(tmp_project, "feat-a", data)
    log = store2.startup()
    assert store2.get_session_feature(tmp_project, "sess-rebuilt") is not None
    assert any("feat-a" in msg for msg in log)

def test_accumulate_cost(store, tmp_project):
    store.write_feature(tmp_project, "feat-a", {
        "name": "feat-a", "status": "active",
        "total_cost_usd": 1.0, "total_input_tokens": 100,
        "total_output_tokens": 50, "prompt_count": 2,
    })
    store.accumulate_cost(tmp_project, "feat-a", cost_usd=0.5, input_tokens=200, output_tokens=30)
    data = store.read_feature(tmp_project, "feat-a")
    assert data["total_cost_usd"] == pytest.approx(1.5)
    assert data["total_input_tokens"] == 300
    assert data["prompt_count"] == 3
