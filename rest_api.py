# rest_api.py
import json as _json
import os
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from urllib.parse import unquote

from feature_store import FeatureStore, _now_iso
from mcp_tools import _conflict_response, _abandon_session, _render_summary


class CostPayload(BaseModel):
    cost_usd: float
    input_tokens: int
    output_tokens: int


class RegisterProjectPayload(BaseModel):
    project_dir: str


class StartFeaturePayload(BaseModel):
    session_id: str
    force: bool = False


class ResumeFeaturePayload(BaseModel):
    feature_name: str
    force: bool = False


class CompleteFeaturePayload(BaseModel):
    summary: str


class MilestonePayload(BaseModel):
    text: str


def create_api_router(store: FeatureStore) -> APIRouter:
    router = APIRouter()

    # ── Existing endpoints ─────────────────────────────────────────────────

    @router.get("/projects/{encoded_path:path}/features")
    def get_features(encoded_path: str):
        project_dir_str = unquote(encoded_path)
        try:
            pdir = store.ensure_project_dir(project_dir_str)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"Unknown project: {project_dir_str}")
        return store.list_features(pdir)

    @router.post("/projects/{encoded_path:path}/sessions/{session_id}/cost")
    def post_cost(encoded_path: str, session_id: str, body: CostPayload):
        project_dir_str = unquote(encoded_path)
        try:
            pdir = store.ensure_project_dir(project_dir_str)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"Unknown project: {project_dir_str}")
        data = store.get_session_feature(pdir, session_id)
        if not data:
            return {"status": "no_active_feature"}
        store.accumulate_cost(
            pdir, data["name"],
            cost_usd=body.cost_usd,
            input_tokens=body.input_tokens,
            output_tokens=body.output_tokens,
        )
        return {"status": "ok"}

    @router.post("/admin/restart")
    def post_restart():
        os._exit(42)

    # ── Project registration ───────────────────────────────────────────────

    @router.post("/projects")
    def register_project(body: RegisterProjectPayload):
        p = Path(body.project_dir)
        if p not in store._projects:
            store._projects.append(p)
        (p / ".claude" / "features").mkdir(parents=True, exist_ok=True)
        return {"status": "registered", "project_dir": str(p)}

    # ── Lifecycle endpoints ────────────────────────────────────────────────

    @router.post("/projects/{encoded_path:path}/features/{feature_name}/start")
    def post_start_feature(encoded_path: str, feature_name: str, body: StartFeaturePayload):
        project_dir_str = unquote(encoded_path)
        try:
            pdir = store.ensure_project_dir(project_dir_str)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"Unknown project: {project_dir_str}")

        session_id = body.session_id

        # Auto-complete any feature already active for this session
        existing = store.get_session_feature(pdir, session_id)
        if existing:
            existing["status"] = "completed"
            existing["completed_at"] = _now_iso()
            for s in existing.get("sessions", []):
                if s.get("session_id") == session_id:
                    s["status"] = "completed"
            store.write_feature(pdir, existing["name"], existing)
            store.unregister_session(session_id)

        # Conflict check
        conflict_sid = store.get_active_session_for_feature(pdir, feature_name)
        if conflict_sid and conflict_sid != session_id:
            if not body.force:
                return _json.loads(_conflict_response(store, pdir, feature_name, conflict_sid))
            _abandon_session(store, pdir, feature_name, conflict_sid)

        now = _now_iso()
        existing_data = store.read_feature(pdir, feature_name) or {}
        feature_data = {
            "name": feature_name,
            "status": "active",
            "session_id": session_id,
            "description": existing_data.get("description", ""),
            "sessions": existing_data.get("sessions", []) + [
                {"session_id": session_id, "session_start": now,
                 "source": "rest", "status": "active"}
            ],
            "milestones": existing_data.get("milestones", []),
            "started_at": existing_data.get("started_at") or now,
            "completed_at": None,
            "total_cost_usd": existing_data.get("total_cost_usd", 0.0),
            "total_input_tokens": existing_data.get("total_input_tokens", 0),
            "total_output_tokens": existing_data.get("total_output_tokens", 0),
            "prompt_count": existing_data.get("prompt_count", 0),
        }
        store.write_feature(pdir, feature_name, feature_data)
        store.register_session(pdir, session_id, feature_name)
        return {"status": "started", "feature_name": feature_name}

    @router.post("/projects/{encoded_path:path}/sessions/{session_id}/resume")
    def post_resume_feature(encoded_path: str, session_id: str, body: ResumeFeaturePayload):
        project_dir_str = unquote(encoded_path)
        try:
            pdir = store.ensure_project_dir(project_dir_str)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"Unknown project: {project_dir_str}")

        data = store.read_feature(pdir, body.feature_name)
        if not data:
            return {"error": f"Feature '{body.feature_name}' not found"}

        conflict_sid = store.get_active_session_for_feature(pdir, body.feature_name)
        if conflict_sid and conflict_sid != session_id:
            if not body.force:
                return _json.loads(_conflict_response(store, pdir, body.feature_name, conflict_sid))
            _abandon_session(store, pdir, body.feature_name, conflict_sid)

        now = _now_iso()
        data["status"] = "active"
        data["completed_at"] = None
        data["session_id"] = session_id
        data.setdefault("sessions", []).append(
            {"session_id": session_id, "session_start": now,
             "source": "rest", "status": "active"}
        )
        store.write_feature(pdir, body.feature_name, data)
        store.register_session(pdir, session_id, body.feature_name)
        return {"status": "resumed", "feature_name": body.feature_name}

    @router.post("/projects/{encoded_path:path}/sessions/{session_id}/complete")
    def post_complete_feature(encoded_path: str, session_id: str, body: CompleteFeaturePayload):
        project_dir_str = unquote(encoded_path)
        try:
            pdir = store.ensure_project_dir(project_dir_str)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"Unknown project: {project_dir_str}")

        data = store.get_session_feature(pdir, session_id)
        if not data:
            return {"error": "No active feature for this session"}

        name = data["name"]
        now = _now_iso()
        data["status"] = "completed"
        data["completed_at"] = now
        for s in data.get("sessions", []):
            if s.get("session_id") == session_id:
                s["status"] = "completed"
        store.write_feature(pdir, name, data)
        store.unregister_session(session_id)

        md_path = pdir / "features" / f"{name}.md"
        md_path.parent.mkdir(exist_ok=True)
        md_path.write_text(_render_summary(data, body.summary), encoding="utf-8")
        return {"status": "completed", "feature_name": name}

    @router.post("/projects/{encoded_path:path}/sessions/{session_id}/discard")
    def post_discard_feature(encoded_path: str, session_id: str):
        project_dir_str = unquote(encoded_path)
        try:
            pdir = store.ensure_project_dir(project_dir_str)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"Unknown project: {project_dir_str}")

        data = store.get_session_feature(pdir, session_id)
        if not data:
            return {"error": "No active feature for this session"}

        name = data["name"]
        store.unregister_session(session_id)

        # Archive markdown if present
        md_path = pdir / "features" / f"{name}.md"
        if md_path.exists():
            archive = pdir / "features" / "_archived"
            archive.mkdir(exist_ok=True)
            md_path.rename(archive / f"{name}.md")

        # Delete JSON entirely (clean removal; use MCP feature_discard for soft-delete)
        json_path = store._feature_path(pdir, name)
        json_path.unlink(missing_ok=True)

        return {"status": "discarded", "feature_name": name}

    @router.post("/projects/{encoded_path:path}/sessions/{session_id}/milestone")
    def post_milestone(encoded_path: str, session_id: str, body: MilestonePayload):
        project_dir_str = unquote(encoded_path)
        try:
            pdir = store.ensure_project_dir(project_dir_str)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"Unknown project: {project_dir_str}")

        data = store.get_session_feature(pdir, session_id)
        if not data:
            return {"error": "No active feature for this session"}

        milestone = {"timestamp": _now_iso(), "session_id": session_id, "text": body.text}
        data.setdefault("milestones", []).append(milestone)
        store.write_feature(pdir, data["name"], data)
        return {"status": "added", "milestone": milestone}

    return router
