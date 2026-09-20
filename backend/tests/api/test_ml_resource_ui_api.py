from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient

from materialsagent.api.routes.ml_resources import router
from materialsagent.api.dependencies import get_actor_context
from materialsagent.application.context import ActorContext


def app_with(service=None, enabled=True):
    app = FastAPI()
    app.state.ml_resources = service
    app.state.enable_materials_ml_resources = enabled
    app.state.enable_materials_ml_resource_context = enabled
    app.state.enable_dev_materials_ml_tools = enabled
    app.dependency_overrides[get_actor_context] = lambda: ActorContext(actor_id="actor", user_id=None)
    app.include_router(router)
    return app


def test_capabilities_are_configuration_only_without_remote_io_or_credentials():
    with TestClient(app_with(object())) as http:
        result = http.get("/api/v1/capabilities").json()["data"]
    assert result == {"ml_resources": True, "ml_context": True, "ml_tools": True, "coordination": True, "model_package": False}


def test_local_fence_and_upload_lookup_remain_readable_with_feature_off():
    calls = []
    def state(actor, scope):
        calls.append((actor, scope))
        return {"fence": {"operation_id": "original", "version": 3}}
    def uploads(actor, scope, **kw):
        calls.append((actor, scope, kw))
        return {"items": [], "next_cursor": None}
    with TestClient(app_with(SimpleNamespace(ui_state=state, uploads=uploads), enabled=False)) as http:
        assert http.get("/api/v1/conversations/scope/ml/ui-state").json()["data"]["fence"]["operation_id"] == "original"
        assert http.get("/api/v1/conversations/scope/ml/uploads", headers={"Idempotency-Key": "original-browser-key"}).status_code == 200
    assert calls == [("actor", "scope"), ("actor", "scope", {"limit": 20, "after": None, "key": "original-browser-key"})]


def test_selection_and_source_event_endpoints_are_removed():
    with TestClient(app_with(SimpleNamespace())) as http:
        assert http.get("/api/v1/conversations/scope/ml/resources/ref/sources").status_code == 404
        assert http.post("/api/v1/conversations/scope/ml/resources/ref/select").status_code in {404, 405}


def test_type_filter_and_expected_remote_id_reach_scoped_reference_query():
    calls = []
    def listing(actor, scope, **kw):
        calls.append((actor, scope, kw)); return {"items": [], "next_cursor": None}
    with TestClient(app_with(SimpleNamespace(list=listing))) as http:
        assert http.get("/api/v1/conversations/scope/ml/resources?resource_type=model&resource_id=remote").status_code == 200
        assert http.get("/api/v1/conversations/scope/ml/resources?resource_type=unsupported").status_code == 422
    assert calls == [("actor", "scope", {"limit": 20, "after": None, "resource_type": "model", "resource_id": "remote"})]
