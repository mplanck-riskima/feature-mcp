# rest_api.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from urllib.parse import unquote
from feature_store import FeatureStore


class CostPayload(BaseModel):
    cost_usd: float
    input_tokens: int
    output_tokens: int


def create_api_router(store: FeatureStore) -> APIRouter:
    router = APIRouter()

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

    return router
