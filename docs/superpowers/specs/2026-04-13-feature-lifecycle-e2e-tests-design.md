# Feature Lifecycle E2E Test Coverage — Design Spec

**Date:** 2026-04-13  
**Repos affected:** `M:\feature-mcp`, `M:\bridgecrew`

---

## Problem

The existing `feature-mcp` test suite has 36 unit/integration tests covering the store utilities, session routing, MCP tool functions (via `FakeMCP`), and REST API routes. None of them test the **full feature lifecycle as a chained sequence**, and none run against the **real FastAPI application** or a **live server**. Two gaps:

1. No test chains `feature_start → feature_add_milestone → feature_complete` and verifies the resulting JSON state and markdown doc together.
2. No test hits `localhost:8765` to verify the server starts, accepts a project, and runs a lifecycle.

---

## Approach

**Approach 1 (chosen): FakeMCP + REST in-process, REST lifecycle endpoints for smoke test**

- In-process: use `FakeMCP` + real `FeatureStore` + FastAPI `TestClient`, chained into full lifecycle sequences. No HTTP overhead on the MCP SSE layer.
- Smoke test: add REST mirrors of lifecycle operations to `rest_api.py` + a project-registration endpoint. Smoke test runs the full lifecycle against the real server via plain HTTP.

MCP SSE is complex to test with a standard HTTP client. The REST layer is what the bot actually uses day-to-day, and the `FakeMCP` pattern already exercises the tool logic correctly.

---

## Part 1: New REST lifecycle endpoints (`feature-mcp/rest_api.py`)

Six new endpoints added to `create_api_router`:

```
POST /api/projects
    body: {"project_dir": "/abs/path"}
    Registers a new project dir with the store at runtime.
    Returns: {"status": "registered", "project_dir": "..."}

POST /api/projects/{path}/features/{name}/start
    body: {"session_id": str, "subdir": str|null, "force": bool = false}
    Calls store methods to start or force-start a feature.
    Returns: {"status": "started"|"conflict", ...}

POST /api/projects/{path}/sessions/{session_id}/resume
    body: {"feature_name": str, "force": bool = false}
    Resumes an existing feature for the given session.
    Returns: {"status": "resumed"|"conflict", ...}

POST /api/projects/{path}/sessions/{session_id}/complete
    body: {"summary": str}
    Completes the active feature for the session, writes markdown doc.
    Returns: {"status": "completed", "feature_name": str}

POST /api/projects/{path}/sessions/{session_id}/discard
    Discards the active feature for the session, archives doc if present.
    Returns: {"status": "discarded", "feature_name": str}

POST /api/projects/{path}/sessions/{session_id}/milestone
    body: {"text": str}
    Adds a milestone to the active feature.
    Returns: {"status": "added"}
```

Each handler calls the same `FeatureStore` methods as the MCP tools — no logic duplication. Conflict detection follows the same pattern as `mcp_tools.py` (check for active session, return conflict or proceed). `POST /api/projects` adds the path to `store._projects`.

---

## Part 2: In-process E2E lifecycle tests (`feature-mcp/tests/test_e2e_lifecycle.py`)

### Fixture: `e2e`

Wraps together:
- `tmp_project` fixture (existing) — temp dir with `.claude/features/`
- `FeatureStore([str(tmp_project)])` — real store instance
- `FakeMCP` with tools registered via `register_tools(mcp, store)` — MCP tool calls
- FastAPI `TestClient` over the REST router — REST verification

All three share the same `store` instance, so MCP tool calls and REST responses see identical state.

### Test groups

**`TestHappyPath`**
- `test_start_creates_feature` — `feature_start` → JSON exists, `store.get_session_feature` returns feature
- `test_milestone_recorded` — `feature_add_milestone` → JSON has 1 milestone with `text` and `timestamp`
- `test_rest_returns_active_feature` — `GET /features` → list contains feature with `status=active`
- `test_complete_writes_markdown` — `feature_complete` → `features/feat.md` exists, contains summary and `# feat` heading
- `test_complete_unregisters_session` — after complete, `store.get_session_feature` returns None
- `test_rest_returns_completed_feature` — `GET /features` → `status=completed`

**`TestConflictResolution`**
- `test_concurrent_start_returns_conflict` — session A starts feat, session B start returns `status=conflict` with `conflicting_session_id`
- `test_force_abandons_old_session` — session B force-starts → `status=started`, session A unregistered, session A's entry in `sessions[]` has `status=abandoned`
- `test_forced_session_active` — after force, session B's `get_session_feature` returns the feature

**`TestResumePath`**
- `test_resume_completed_feature` — start → complete → resume → `status=resumed`, new session registered
- `test_milestone_after_resume` — add milestone to resumed feature → appears in JSON
- `test_complete_after_resume` — complete resumed feature → markdown updated (summary present), `status=completed`

**`TestDiscardPath`**
- `test_discard_removes_json` — start → discard → `.claude/features/feat.json` deleted
- `test_discard_archives_doc` — pre-create `features/feat.md` → discard → file moved to `features/_archived/feat.md`
- `test_rest_returns_empty_after_discard` — `GET /features` → empty list

---

## Part 3: Real-server smoke test (`bridgecrew/tests/e2e/test_feature_mcp.py`)

### Skip condition

A module-level fixture checks `GET http://localhost:8765/api/docs` (or any fast endpoint). If the server isn't reachable, the entire module is skipped with: `"feature-mcp server not running on localhost:8765 — skipping smoke tests"`.

### Fixture: `live_project`

1. Creates a `tmp_path` with `.claude/features/` structure
2. `POST /api/projects` body `{"project_dir": str(tmp_path)}` → registers temp project
3. Yields `(tmp_path, httpx.Client(base_url="http://localhost:8765"))`
4. Teardown: `POST .../sessions/smoke-sess-1/discard` best-effort (ignores errors)

### Test functions

Each lifecycle step is a separate `test_*` function but they share the `live_project` fixture which provides a fresh temp dir per test run. The full lifecycle is covered across:

- `test_live_start_registers_feature` — `POST .../features/smoke-test/start` `{session_id: "smoke-sess-1"}` → 200, `GET /features` contains `smoke-test` with `status=active`
- `test_live_lifecycle_complete` — full sequence in one test: start → milestone → complete → assert `GET /features` `status=completed` and `tmp_path/features/smoke_test.md` contains "Smoke test complete."
- `test_server_rejects_unknown_project` — `POST /api/projects/NONEXISTENT_PATH/features/x/start` → 404

Using a single combined test for the chained lifecycle avoids `pytest-ordering` as a dependency (no new packages needed). The independent connectivity/rejection tests stay separate for clear failure messages.

### Notes

- `httpx.Client` used (sync) to match the existing e2e test style in `bridgecrew/tests/e2e/`
- No new test dependencies — `httpx` is already in `requirements.txt`
- The smoke test does not clean up the temp dir — it's a `tmp_path` which pytest removes automatically

---

## File summary

| File | Change |
|------|--------|
| `feature-mcp/rest_api.py` | +6 endpoints (project register + 5 lifecycle ops) |
| `feature-mcp/tests/test_e2e_lifecycle.py` | New — 15 in-process E2E tests |
| `bridgecrew/tests/e2e/test_feature_mcp.py` | New — 4 smoke tests |
| `bridgecrew/tests/e2e/conftest.py` | Minor — export `skip_no_server` helper or keep self-contained |

No changes to `mcp_tools.py`, `feature_store.py`, or `server.py`.
