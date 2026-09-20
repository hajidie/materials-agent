from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
from threading import Event
from types import SimpleNamespace

import pytest
from PIL import Image

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import ApplicationConflictError, ConversationBusyError
from materialsagent.application.asset_service import AssetService
from materialsagent.application.ebsd_assets import upload as upload_image
from materialsagent.domain.models.actor import Actor
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.message import Message
from materialsagent.infrastructure.db.agent import SQLAlchemyAgentStore
from materialsagent.infrastructure.db.ml_resources import MLResourceRepository, lock_conversation
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork

SERVICE = {"service_id": "materials_ml", "binding_version": "1", "endpoint_digest": "b" * 64}
DESCRIPTOR = {"resource_type": "dataset", "resource_id": "remote", "scope_id": "conversation_1",
    "identity_contract_version": "ml-resource-identity-v1", "remote_identity_digest": "a" * 64, "identity": {}}


@pytest.fixture
def resources(migrated_database_engine):
    sessions = create_session_factory(migrated_database_engine)
    stamp = datetime.now(timezone.utc)
    with SQLAlchemyUnitOfWork(sessions) as uow:
        uow.actors.add(Actor.local_anonymous("actor_1"))
        uow.conversations.add(Conversation("conversation_1", "actor_1", None, stamp, stamp))
        uow.commit()
    repo = MLResourceRepository(sessions)
    with repo.transaction("actor_1", "conversation_1", writable=True) as tx:
        tx.bind(SERVICE, "verified-test")
    return repo, sessions


def message():
    return Message.user(message_id="message_1", conversation_id="conversation_1", task_id=None,
        actor_id="actor_1", request_id="request", content_text="synthetic")


@pytest.mark.parametrize("entry", ["message", "new_run", "supplement", "retry", "reference", "binding", "ml_upload", "ebsd_upload",
    "native_dispatch", "managed_dispatch", "mcp_dispatch", "confirmation"])
def test_deleted_fence_serializes_against_each_new_work_entry(resources, entry):
    repo, sessions = resources
    observed, release = Event(), Event()
    store = SQLAlchemyAgentStore(sessions)
    def work():
        # A stale read is not sufficient authorization to commit later.
        with repo.transaction("actor_1", "conversation_1", writable=True):
            pass
        observed.set(); assert release.wait(10)
        if entry in ("new_run", "supplement", "retry"):
            extra = {"run_id": "old", "waiting_version": 0} if entry == "supplement" else {
                "retry_source": SimpleNamespace(agent_run_id="old"), "retry_type": "ANSWER_REGENERATION"} if entry == "retry" else {}
            return store.submit("conversation_1", "actor_1", "synthetic", "request", **extra)
        if entry == "message":
            with SQLAlchemyUnitOfWork(sessions) as uow:
                uow.messages.add(message()); uow.commit()
        elif entry.endswith("dispatch") or entry == "confirmation":
            from materialsagent.infrastructure.db.tool_invocation import SQLAlchemyInvocationRunRepository
            from materialsagent.domain.models.tool_invocation import InvocationStatus
            # Exercise the actual shared SQL dispatch/confirmation CAS gate before any row mutation.
            run = SimpleNamespace(actor_id="actor_1", conversation_id="conversation_1",
                status=InvocationStatus.RUNNING if entry != "confirmation" else InvocationStatus.PENDING_CONFIRMATION)
            with sessions.begin() as session:
                SQLAlchemyInvocationRunRepository(session).update(run, expected_status=InvocationStatus.PENDING)
        elif entry == "ebsd_upload":
            class Storage:
                def put(self, *args):
                    pytest.fail("Fenced upload reached object storage")
            service = AssetService(lambda: SQLAlchemyUnitOfWork(sessions), Storage(), environment="test", bucket="test", storage_namespace="test")
            image = BytesIO(); Image.new("RGB", (128, 128)).save(image, format="PNG")
            upload_image(service, ActorContext("actor_1"), "conversation_1", image.getvalue(), "image")
        else:
            with repo.transaction("actor_1", "conversation_1", writable=True) as tx:
                if entry == "reference":
                    tx.register(SERVICE, DESCRIPTOR, "verified")
                elif entry == "binding":
                    tx.bind({**SERVICE, "service_id": "other"}, "verified")
                else:
                    tx.put("upload", "upload", {}, status="DISPATCHED", operation_key="key")
    with ThreadPoolExecutor() as pool:
        future = pool.submit(work)
        assert observed.wait(10)
        operation = repo.begin_delete("actor_1", "conversation_1", datetime.now(timezone.utc))
        assert operation["status"] == "DELETE_PENDING"
        release.set()
        with pytest.raises(ApplicationConflictError) as failed:
            future.result()
        assert failed.value.code == "CONVERSATION_DELETE_PENDING"
    fresh = MLResourceRepository(sessions)
    with pytest.raises(ApplicationConflictError):
        with fresh.transaction("actor_1", "conversation_1", writable=True):
            pass


def test_admitted_work_blocks_delete_and_late_busy_cannot_clear_new_fence(resources):
    repo, sessions = resources
    with repo.transaction("actor_1", "conversation_1", writable=True) as tx:
        tx.put("upload", "upload", {}, status="DISPATCHED", operation_key="key")
    with pytest.raises(ConversationBusyError):
        repo.begin_delete("actor_1", "conversation_1", datetime.now(timezone.utc))
    with repo.transaction("actor_1", "conversation_1") as tx:
        tx.put("upload", "upload", {}, status="RESOLVED", operation_key="key")
    first = repo.begin_delete("actor_1", "conversation_1", datetime.now(timezone.utc))
    repo.deletion_fact("actor_1", first["operation_id"], {"status": "BUSY"})
    second = repo.begin_delete("actor_1", "conversation_1", datetime.now(timezone.utc))
    assert second["fence_version"] > first["fence_version"]
    repo.deletion_fact("actor_1", first["operation_id"], {"status": "BUSY"})
    with sessions.begin() as session:
        row = lock_conversation(session, "actor_1", "conversation_1")
        assert row.deletion_fence_operation_id == second["operation_id"]


def test_rejected_delete_still_invalidates_earlier_preflight_epoch(resources):
    repo, _ = resources
    with repo.transaction("actor_1", "conversation_1", writable=True) as tx:
        epoch = tx.fence_version
    operation = repo.begin_delete("actor_1", "conversation_1", datetime.now(timezone.utc))
    repo.deletion_fact("actor_1", operation["operation_id"], {"status": "BUSY"})
    with pytest.raises(ApplicationConflictError):
        with repo.transaction("actor_1", "conversation_1", writable=True, expected_version=epoch):
            pytest.fail("Stale upload/registration preflight passed after fence changed")
