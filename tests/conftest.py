import pytest
from pathlib import Path
try:
    from feature_store import FeatureStore
except ImportError:
    FeatureStore = None


@pytest.fixture
def tmp_project(tmp_path):
    """A temp directory set up as a project with .claude/features/."""
    features_dir = tmp_path / ".claude" / "features"
    features_dir.mkdir(parents=True)
    (tmp_path / ".claude" / "features.json").write_text("{}")
    return tmp_path


@pytest.fixture
def store(tmp_project):
    if FeatureStore is None:
        pytest.skip("FeatureStore not yet implemented")
    s = FeatureStore([str(tmp_project)])
    return s
