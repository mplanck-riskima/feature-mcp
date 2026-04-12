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
