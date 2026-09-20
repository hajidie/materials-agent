from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from types import SimpleNamespace
import pytest
from sqlalchemy import select, func
from materialsagent.application.chat_artifacts import ChatArtifacts
from materialsagent.application.errors import ApplicationConflictError, ResourceNotFoundError
from materialsagent.application.ml_resources import MLResources
from materialsagent.infrastructure.db.agent import AgentExecutionRow, AgentRunRow, SQLAlchemyAgentStore
from materialsagent.infrastructure.db.conversation_task import MessageRow
from materialsagent.infrastructure.db.ml_resources import MLResourceRepository, lock_conversation
from backend.tests.api.test_message_orchestration import client_and_conversation


@pytest.fixture
def chat(api_harness):
    client, conversation = client_and_conversation(api_harness)
    with client:
        view = client.post(f"/api/v1/conversations/{conversation}/messages", json={"mode": "NEW_RUN", "content_text": "1000 MPa 转 GPa"},
            headers={"Idempotency-Key": "original"}).json()["data"]["agent_run"]
        sessions = client.app.state.agent_runtime.store.sessions
        with sessions.begin() as session:
            execution = session.scalar(select(AgentExecutionRow))
            execution.document = {**execution.document, "tool_name": "materials_ml_train_tabular_regression"}
            source = "invocation:" + execution.invocation_run_id
        descriptor = {"resource_type": "training_run", "resource_id": "train-private", "scope_id": conversation,
            "identity_contract_version": "ml-resource-identity-v1", "remote_identity_digest": "a" * 64,
            "identity": {"dataset_id": "dataset-private"}, "status": "RUNNING"}
        model = {**descriptor, "resource_type": "model", "resource_id": "model-private", "status": "AVAILABLE",
            "identity": {"dataset_id": "dataset-private", "training_run_id": "train-private"}}
        class Remote:
            service = {"service_id": "materials_ml", "binding_version": "1", "endpoint_digest": "b" * 64}
            state = "RUNNING"
            hook = None
            calls = []
            units = {"屈服强度": "MPa"}
            def descriptor(self, scope, kind, identity, version=None):
                self.calls.append(("descriptor", kind))
                return model if kind == "model" else {**descriptor, "status": self.state}
            def resource(self, scope, kind, identity, action=None):
                self.calls.append(("read", kind))
                if self.hook:
                    self.hook()
                return {"id": identity, "scope_id": scope, "status": "AVAILABLE" if kind == "model" else self.state,
                    "model_id": "model-private", "spec": {"algorithm": "RF", "target": "屈服强度"},
                    "metrics": {"r2": .957, "mae": 7.18}, "units": self.units, "warnings": ["IID_NOT_VERIFIED"]}
        remote = Remote()
        resources = MLResources(MLResourceRepository(sessions), remote)
        with resources.repo.transaction("agent-test", conversation, writable=True) as tx:
            ref = tx.register(remote.service, descriptor, source)
            message = tx.session.get(MessageRow, view["final_answer"]["answer_id"])
            message.structured_content = {**message.structured_content, "artifacts": [{"attachment_id": ref["reference_id"], "kind": "training_run", "name": "训练结果"}]}
        yield ChatArtifacts(sessions, resources), remote, conversation, view, ref


def test_view_is_read_only_even_when_training_completed(chat):
    service, remote, conversation, run, ref = chat
    remote.state = "SUCCEEDED"
    with service.sessions() as session:
        before = deepcopy(session.get(AgentRunRow, run["agent_run_id"]).document)
    with service.resources.repo.transaction("agent-test", conversation) as tx:
        references = tx.rows("reference")
    value = service.view("agent-test", conversation, run["final_answer"]["answer_id"], ref["reference_id"])
    assert "SUCCEEDED" not in str(value["presentation"]) and "model-private" not in str(value["presentation"])
    assert service.messages("agent-test", conversation) == []
    with service.resources.repo.transaction("agent-test", conversation) as tx:
        assert tx.rows("reference") == references and len(references) == 1
    with service.sessions() as session:
        assert session.get(AgentRunRow, run["agent_run_id"]).document == before
    with pytest.raises(ResourceNotFoundError):
        service.view("other-actor", conversation, run["final_answer"]["answer_id"], ref["reference_id"])
    with pytest.raises(ResourceNotFoundError):
        service.view("agent-test", conversation, run["source_message_id"], ref["reference_id"])


def test_terminal_publication_is_unique_immutable_and_enters_next_context(chat):
    service, remote, conversation, run, ref = chat
    assert service.observe("agent-test", conversation)["pending"]
    assert service.messages("agent-test", conversation) == []
    with service.sessions() as session:
        before = deepcopy(session.get(AgentRunRow, run["agent_run_id"]).document)
    remote.state = "SUCCEEDED"
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: service.observe("agent-test", conversation), range(3)))
    messages = service.messages("agent-test", conversation)
    assert len(messages) == 1 and "0.957" in messages[0]["text"] and "7.18 MPa" in messages[0]["text"]
    assert "IID_NOT_VERIFIED" not in messages[0]["text"] and "尚未验证" in messages[0]["text"]
    assert service.observe("agent-test", conversation)["messages"] == messages
    with service.sessions() as session:
        assert session.get(AgentRunRow, run["agent_run_id"]).document == before
    next_run, _ = SQLAlchemyAgentStore(service.sessions).submit(conversation, "agent-test", "解释验证结果", "follow-up")
    assert any("0.957" in item["content"] for item in next_run.context)


def test_terminal_publication_respects_delete_fence_after_remote_read(chat):
    service, remote, conversation, run, ref = chat
    remote.state = "FAILED"
    entered, release = Event(), Event()
    def hook():
        entered.set(); assert release.wait(10)
    remote.hook = hook
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(service.observe, "agent-test", conversation)
        assert entered.wait(10)
        with service.sessions.begin() as session:
            owner = lock_conversation(session, "agent-test", conversation)
            owner.deletion_fence_operation_id = "delete-in-flight"
            owner.deletion_fence_version += 1
        release.set()
        future.result()
    assert service.messages("agent-test", conversation) == []


def test_inferred_units_survive_completion_and_read_only_model_view(chat):
    from materialsagent.application.unit_resolution import UnitResolutionPolicy
    from materialsagent.domain.models.semantic_units import UnitAnnotation
    from backend.tests.unit.test_unit_resolution import ANNOTATION
    service, remote, conversation, run, ref = chat
    remote.units = {"strength_MPa": None}
    annotation = UnitResolutionPolicy.resolve(UnitAnnotation(**ANNOTATION))
    with service.sessions.begin() as session:
        execution = session.scalar(select(AgentExecutionRow))
        execution.document = {**execution.document, "unit_annotations": [annotation]}
        old_message = deepcopy(session.get(MessageRow, run["final_answer"]["answer_id"]).content_text)
    remote.state = "SUCCEEDED"
    messages = service.observe("agent-test", conversation)["messages"]
    assert len(messages) == 1 and "模型语义推断" in messages[0]["text"] and "strength_MPa" in messages[0]["text"]
    assert messages[0]["presentation"]["unit_annotations"] == [annotation]
    assert service.observe("agent-test", conversation)["messages"] == messages
    with service.resources.repo.transaction("agent-test", conversation) as tx:
        references = tx.rows("reference")
    for artifact in messages[0]["artifacts"]:
        value = service.view("agent-test", conversation, messages[0]["message_id"], artifact["attachment_id"])
        assert value["presentation"]["unit_annotations"] == [annotation]
    with service.resources.repo.transaction("agent-test", conversation) as tx:
        assert references == tx.rows("reference")
    with service.sessions() as session:
        assert session.get(MessageRow, run["final_answer"]["answer_id"]).content_text == old_message
    assert remote.units == {"strength_MPa": None}
    next_run, _ = SQLAlchemyAgentStore(service.sessions).submit(conversation, "agent-test", "解释单位", "unit-follow-up")
    assert any("模型语义推断" in item["content"] for item in next_run.context)


def test_public_run_has_no_raw_tool_result_or_diagnostics(api_harness):
    client, conversation = client_and_conversation(api_harness)
    with client:
        response = client.post(f"/api/v1/conversations/{conversation}/messages", json={"mode": "NEW_RUN", "content_text": "1000 MPa 转 GPa"}, headers={"Idempotency-Key": "safe"})
        run = response.json()["data"]["agent_run"]
        assert run["observations"][0]["presentation"]["facts"]["value"] == 1
        assert not {"resource_snapshot", "draft", "calls", "steps", "error_code"} & run.keys()
        assert not {"data", "result_summary", "invocation_run_id", "error"} & run["observations"][0].keys()
        assert client.get(f"/api/v1/agent-runs/{run['agent_run_id']}/trace").status_code == 404
        assert client.post(f"/api/v1/conversations/{conversation}/messages", json={"mode": "NEW_RUN", "content_text": "旧格式", "ebsd_asset_id": "old"}, headers={"Idempotency-Key": "old"}).status_code == 422
