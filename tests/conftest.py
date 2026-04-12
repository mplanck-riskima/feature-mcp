import pytest
from pathlib import Path
from feature_store import FeatureStore


@pytest.fixture
def tmp_project(tmp_path):
    features_dir = tmp_path / ".claude" / "features"
    features_dir.mkdir(parents=True)
    (tmp_path / ".claude" / "features.json").write_text("{}")
    return tmp_path


@pytest.fixture
def store(tmp_project):
    return FeatureStore([str(tmp_project)])
