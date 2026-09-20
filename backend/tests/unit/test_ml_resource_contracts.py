from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import subprocess
import sys

import pytest

from materialsagent.domain.models.ml_resource import descriptor_matches

ROOT = Path(__file__).resolve().parents[3]


def migration():
    spec = spec_from_file_location("p5_migration", ROOT / "backend/alembic/versions/0018_ml_resources.py")
    module = module_from_spec(spec); spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("missing", [None, "binding", "scope", "actor", "agent", "dispatch", "identity", "normalizer"])
def test_historical_scope_requires_complete_approved_evidence(missing):
    binding = {"server_id": "materials_ml", "binding_version": "1", "endpoint_digest": "a" * 64}
    row = {"tool_id": "approved", "binding_snapshot": binding, "dispatch_started_at": "original",
        "actor_id": "owner", "agent_actor": "owner", "message_actor": "owner",
        "conversation_id": "scope", "agent_conversation": "scope", "message_conversation": "scope",
        "status": "SUCCEEDED", "result_data": {"resource": {"scope_id": "scope", "id": "remote"}}}
    if missing == "binding": row["binding_snapshot"] = {**binding, "endpoint_digest": "b" * 64}
    if missing == "scope": row["result_data"]["resource"]["scope_id"] = "other"
    if missing == "actor": row["message_actor"] = "other"
    if missing == "agent": row["agent_conversation"] = "other"
    if missing == "dispatch": row["dispatch_started_at"] = None
    if missing == "identity": row["result_data"] = {}
    if missing == "normalizer":
        row.update(status="OUTCOME_UNKNOWN", remote_operation={"request_digest": "original", "operation": "training.submit", "digest_version": "v1"},
            remote_receipt={"lookup_status": "FOUND", "resource": {"id": "remote", "scope_id": "scope"},
                "request_digest": "changed", "operation": "training.submit", "digest_version": "v1"})
    actual = migration().historical_binding(row, {"approved": binding})
    assert bool(actual) == (missing is None)


def test_reference_uses_remote_digest_without_reinterpreting_audit_fields():
    ref = {"scope_id": "scope", "resource_type": "dataset", "resource_id": "data",
        "identity_contract_version": "ml-resource-identity-v1", "remote_identity_digest": "a" * 64,
        "identity": {"old_audit": True}}
    descriptor = {**ref, "identity": {"new_audit": True}, "status": "DELETED"}
    assert descriptor_matches(ref, descriptor)  # identity equality says nothing about availability
    assert not descriptor_matches(ref, {**descriptor, "remote_identity_digest": "b" * 64})
    assert not descriptor_matches(ref, {**descriptor, "identity_contract_version": "v2"})


def test_resource_transport_imports_without_sdk_or_engine():
    code = f"""import sys
sys.path.insert(0, {str(ROOT / 'backend/src')!r})
sys.modules['mcp'] = None
import materialsagent.infrastructure.tool_clients.ml_resource_client
assert not any(name in sys.modules for name in ('sklearn','joblib','materials_ml','materials_ml_service'))
"""
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("fault", ["redirect", "size", "digest", "media", "json"])
def test_resource_transport_rejects_redirect_and_untrusted_download(monkeypatch, fault):
    import httpx
    from hashlib import sha256
    from materialsagent.infrastructure.tool_clients.ml_resource_client import MLResourceClient
    from materialsagent.application.errors import DependencyUnavailableError
    original = httpx.Client
    visited = []
    def respond(request):
        visited.append(str(request.url))
        if fault == "redirect":
            return httpx.Response(307, headers={"location": "http://127.0.0.1:1/forbidden"})
        if fault == "json":
            return httpx.Response(200, json=[])
        return httpx.Response(200, content=b"abc" if fault != "size" else b"abcd",
            headers={"content-type": "text/plain" if fault == "media" else "application/json"})
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    client = MLResourceClient("http://127.0.0.1:8200/mcp", "synthetic-resource", "1")
    artifact = {"size_bytes": 3, "sha256": "0" * 64 if fault == "digest" else sha256(b"abc").hexdigest(),
        "media_type": "application/json"}
    with pytest.raises(DependencyUnavailableError):
        client.request("owned", "fixed/content", artifact=None if fault == "json" else artifact)
    assert visited == ["http://127.0.0.1:8200/api/v1/scopes/owned/fixed/content"]
