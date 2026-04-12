import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_snake(name: str) -> str:
    name = name.lower()
    name = name.replace("&", "and")
    name = re.sub(r"[-\s]+", "_", name)
    name = re.sub(r"[^a-z0-9_]", "", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "unnamed"


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    for attempt in range(3):
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(path)
            return
        except PermissionError:
            if attempt < 2:
                time.sleep(0.1)
            else:
                raise


class FeatureStore:
    def __init__(self, projects: list[str]):
        self._projects = [Path(p) for p in projects]
        # session_id -> (project_dir, feature_name)
        self._sessions: dict[str, tuple[Path, str]] = {}

    def ensure_project_dir(self, project_dir_str: str) -> Path:
        p = Path(project_dir_str)
        if p not in self._projects:
            raise ValueError(f"Unknown project: {project_dir_str}")
        return p

    def _feature_path(self, project_dir: Path, name: str) -> Path:
        return project_dir / ".claude" / "features" / f"{to_snake(name)}.json"

    def _read_file(self, path: Path) -> Optional[dict]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def read_feature(self, project_dir: Path, name: str) -> Optional[dict]:
        return self._read_file(self._feature_path(project_dir, name))

    def write_feature(self, project_dir: Path, name: str, data: dict) -> None:
        _atomic_write(self._feature_path(project_dir, name), data)

    def list_features(self, project_dir: Path) -> list[dict]:
        features_dir = project_dir / ".claude" / "features"
        if not features_dir.exists():
            return []
        results = []
        for p in sorted(features_dir.glob("*.json")):
            data = self._read_file(p)
            if data:
                results.append(data)
        return results

    def register_session(self, project_dir: Path, session_id: str, feature_name: str) -> None:
        self._sessions[session_id] = (project_dir, feature_name)

    def unregister_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def get_session_feature(self, project_dir: Path, session_id: str) -> Optional[dict]:
        entry = self._sessions.get(session_id)
        if entry and entry[0] == project_dir:
            return self.read_feature(project_dir, entry[1])
        return None

    def get_active_session_for_feature(self, project_dir: Path, name: str) -> Optional[str]:
        for sid, (pdir, fname) in self._sessions.items():
            if pdir == project_dir and to_snake(fname) == to_snake(name):
                return sid
        return None

    def startup(self) -> list[str]:
        log: list[str] = []
        self._sessions.clear()
        for project_dir in self._projects:
            features_dir = project_dir / ".claude" / "features"
            if not features_dir.exists():
                continue
            for json_path in sorted(features_dir.glob("*.json")):
                data = self._read_file(json_path)
                if not data or data.get("status") != "active":
                    continue
                feature_name = data.get("name", json_path.stem)
                for sess in data.get("sessions", []):
                    if sess.get("status") == "active":
                        self._sessions[sess["session_id"]] = (project_dir, feature_name)
                        log.append(
                            f"Restored: {project_dir.name}/{feature_name}"
                            f" <- {sess['session_id'][:8]}"
                        )
        return log

    def accumulate_cost(
        self,
        project_dir: Path,
        feature_name: str,
        cost_usd: float,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        data = self.read_feature(project_dir, feature_name)
        if not data:
            return
        data["total_cost_usd"] = data.get("total_cost_usd", 0.0) + cost_usd
        data["total_input_tokens"] = data.get("total_input_tokens", 0) + input_tokens
        data["total_output_tokens"] = data.get("total_output_tokens", 0) + output_tokens
        data["prompt_count"] = data.get("prompt_count", 0) + 1
        self.write_feature(project_dir, feature_name, data)
