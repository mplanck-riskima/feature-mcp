# mcp_tools.py
import json
from pathlib import Path
from feature_store import FeatureStore, _now_iso, to_snake

SUMMARY_GUIDANCE = (
    "Write a 200-400 word summary covering: what the feature set out to do, "
    "what was actually built, key technical decisions and why, any known gaps "
    "or follow-up work, and notable files changed. This is the primary reference "
    "for future Claude sessions resuming or extending this work."
)


def register_tools(mcp, store: FeatureStore) -> None:

    @mcp.tool()
    def feature_context(project_dir: str, session_id: str) -> str:
        """Get feature context for this session. Call at the start of every Claude session.
        Returns the active feature (if any) and a summary of all project features."""
        try:
            pdir = store.ensure_project_dir(project_dir)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        active = store.get_session_feature(pdir, session_id)
        all_features = store.list_features(pdir)
        return json.dumps({
            "active_feature": active,
            "all_features": [
                {
                    "name": f.get("name"),
                    "status": f.get("status"),
                    "started_at": f.get("started_at"),
                    "completed_at": f.get("completed_at"),
                    "milestone_count": len(f.get("milestones", [])),
                    "session_count": len(f.get("sessions", [])),
                }
                for f in all_features
            ],
        })

    @mcp.tool()
    def feature_list(project_dir: str) -> str:
        """List all features for the project with status, cost, and counts."""
        try:
            pdir = store.ensure_project_dir(project_dir)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        features = store.list_features(pdir)
        return json.dumps([
            {
                "name": f.get("name"),
                "status": f.get("status"),
                "started_at": f.get("started_at"),
                "completed_at": f.get("completed_at"),
                "total_cost_usd": f.get("total_cost_usd", 0.0),
                "milestone_count": len(f.get("milestones", [])),
                "session_count": len(f.get("sessions", [])),
            }
            for f in features
        ])

    @mcp.tool()
    def feature_start(
        project_dir: str, session_id: str, name: str,
        description: str = "", force: bool = False
    ) -> str:
        """Start a new feature for this session. Auto-completes any feature this session
        already has active. If the named feature is active in another session, returns
        status='conflict' — pass force=True only after showing the warning to the user."""
        try:
            pdir = store.ensure_project_dir(project_dir)
        except ValueError as e:
            return json.dumps({"error": str(e)})

        # Auto-complete any existing feature for this session
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
        conflict_sid = store.get_active_session_for_feature(pdir, name)
        if conflict_sid and conflict_sid != session_id:
            if not force:
                return _conflict_response(store, pdir, name, conflict_sid)
            _abandon_session(store, pdir, name, conflict_sid)

        now = _now_iso()
        existing_data = store.read_feature(pdir, name) or {}
        feature_data = {
            "name": name,
            "status": "active",
            "session_id": session_id,
            "description": description or existing_data.get("description", ""),
            "sessions": existing_data.get("sessions", []) + [
                {"session_id": session_id, "session_start": now,
                 "source": "cli", "status": "active"}
            ],
            "milestones": existing_data.get("milestones", []),
            "started_at": existing_data.get("started_at") or now,
            "completed_at": None,
            "total_cost_usd": existing_data.get("total_cost_usd", 0.0),
            "total_input_tokens": existing_data.get("total_input_tokens", 0),
            "total_output_tokens": existing_data.get("total_output_tokens", 0),
            "prompt_count": existing_data.get("prompt_count", 0),
        }
        store.write_feature(pdir, name, feature_data)
        store.register_session(pdir, session_id, name)
        return json.dumps({"status": "started", "feature": feature_data})

    @mcp.tool()
    def feature_resume(
        project_dir: str, session_id: str, feature_name: str, force: bool = False
    ) -> str:
        """Associate this session with an existing feature. If the feature is active in
        another session, returns status='conflict'. Pass force=True only after presenting
        the warning to the user and confirming they want to proceed."""
        try:
            pdir = store.ensure_project_dir(project_dir)
        except ValueError as e:
            return json.dumps({"error": str(e)})

        data = store.read_feature(pdir, feature_name)
        if not data:
            return json.dumps({"error": f"Feature '{feature_name}' not found"})

        conflict_sid = store.get_active_session_for_feature(pdir, feature_name)
        if conflict_sid and conflict_sid != session_id:
            if not force:
                return _conflict_response(store, pdir, feature_name, conflict_sid)
            _abandon_session(store, pdir, feature_name, conflict_sid)

        now = _now_iso()
        data["status"] = "active"
        data["completed_at"] = None
        data["session_id"] = session_id
        data.setdefault("sessions", []).append(
            {"session_id": session_id, "session_start": now,
             "source": "cli", "status": "active"}
        )
        store.write_feature(pdir, feature_name, data)
        store.register_session(pdir, session_id, feature_name)
        return json.dumps({"status": "resumed", "feature": data})

    @mcp.tool()
    def feature_complete(project_dir: str, session_id: str, summary: str) -> str:
        f"""Complete the active feature and write the summary file.

        {SUMMARY_GUIDANCE}"""
        try:
            pdir = store.ensure_project_dir(project_dir)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        data = store.get_session_feature(pdir, session_id)
        if not data:
            return json.dumps({"error": "No active feature for this session"})
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
        md_path.write_text(_render_summary(data, summary), encoding="utf-8")
        return json.dumps({"status": "completed", "summary_path": str(md_path)})

    @mcp.tool()
    def feature_discard(project_dir: str, session_id: str) -> str:
        """Discard the active feature. Moves its summary to features/_archived/ if it exists."""
        try:
            pdir = store.ensure_project_dir(project_dir)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        data = store.get_session_feature(pdir, session_id)
        if not data:
            return json.dumps({"error": "No active feature for this session"})
        name = data["name"]
        data["status"] = "discarded"
        store.write_feature(pdir, name, data)
        store.unregister_session(session_id)
        md_path = pdir / "features" / f"{name}.md"
        if md_path.exists():
            archive = pdir / "features" / "_archived"
            archive.mkdir(exist_ok=True)
            md_path.rename(archive / f"{name}.md")
        return json.dumps({"status": "discarded", "name": name})

    @mcp.tool()
    def feature_add_milestone(project_dir: str, session_id: str, text: str) -> str:
        """Add a timestamped milestone to the active feature. Call when something significant
        is reached mid-session — a working prototype, a key decision, a subsystem completed."""
        try:
            pdir = store.ensure_project_dir(project_dir)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        data = store.get_session_feature(pdir, session_id)
        if not data:
            return json.dumps({"error": "No active feature for this session"})
        milestone = {"timestamp": _now_iso(), "session_id": session_id, "text": text}
        data.setdefault("milestones", []).append(milestone)
        store.write_feature(pdir, data["name"], data)
        return json.dumps({"status": "added", "milestone": milestone})


# --- Module-level helpers ---

def _conflict_response(store: FeatureStore, project_dir: Path, name: str, conflict_sid: str) -> str:
    data = store.read_feature(project_dir, name) or {}
    last_active = next(
        (s for s in reversed(data.get("sessions", [])) if s.get("status") == "active"),
        None,
    )
    return json.dumps({
        "status": "conflict",
        "warning": (
            f"Feature '{name}' is currently active in another session. "
            "Resuming here may cause context loss."
        ),
        "conflicting_session_id": conflict_sid,
        "last_active_at": last_active.get("session_start") if last_active else None,
        "recommendation": (
            "Resume the existing session and complete it there first. "
            "Only pass force=True after showing this warning to the user "
            "and confirming they want to proceed."
        ),
    })


def _abandon_session(store: FeatureStore, project_dir: Path, feature_name: str, session_id: str) -> None:
    data = store.read_feature(project_dir, feature_name)
    if data:
        for s in data.get("sessions", []):
            if s.get("session_id") == session_id:
                s["status"] = "abandoned"
                s["abandoned_at"] = _now_iso()
        store.write_feature(project_dir, feature_name, data)
    store.unregister_session(session_id)


def _render_summary(data: dict, summary: str) -> str:
    name = data.get("name", "unknown")
    started = (data.get("started_at") or "")[:10]
    completed = (data.get("completed_at") or "")[:10]
    cost = data.get("total_cost_usd", 0.0)
    milestones = data.get("milestones", [])
    lines = [
        f"# {name}", "",
        f"**Started:** {started}  ",
        f"**Completed:** {completed}  ",
        f"**Cost:** ${cost:.4f}", "",
        "## Summary", "",
        summary.strip(),
    ]
    if milestones:
        lines += ["", "## Milestones", ""]
        for m in milestones:
            ts = (m.get("timestamp") or "")[:16].replace("T", " ")
            lines.append(f"- **{ts}** — {m['text']}")
    return "\n".join(lines) + "\n"
