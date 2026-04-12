import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from feature_store import FeatureStore
from mcp_tools import register_tools
from rest_api import create_api_router

MCP_PORT = 8765


def create_app(projects: list[str]) -> FastAPI:
    store = FeatureStore(projects)
    log = store.startup()
    for msg in log:
        print(f"[feature-mcp] {msg}")
    print(f"[feature-mcp] {len(log)} active feature session(s) restored")

    mcp = FastMCP("feature-mcp")
    register_tools(mcp, store)

    app = FastAPI(title="Feature MCP")

    # Mount MCP SSE transport at /mcp
    app.mount("/mcp", mcp.sse_app())

    # Mount REST API
    app.include_router(create_api_router(store), prefix="/api")

    return app


if __name__ == "__main__":
    config_path = Path(__file__).parent / "projects.json"
    projects = json.loads(config_path.read_text())
    app = create_app(projects)
    print(f"[feature-mcp] Starting on http://127.0.0.1:{MCP_PORT}")
    uvicorn.run(app, host="127.0.0.1", port=MCP_PORT)
