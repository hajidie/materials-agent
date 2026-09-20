"""Message-owned viewing and bounded, explicit terminal publication.

Reading an artifact is side-effect free. Publication is a separate command; the
remote service is consulted without a database transaction, then a short locked
transaction checks the deletion fence and the conversation event key.
"""
from sqlalchemy import select
from materialsagent.domain.models.agent import identifier, now
from materialsagent.infrastructure.db.conversation_task import MessageRow
from materialsagent.infrastructure.db.ml_resources import lock_conversation
from .errors import ResourceNotFoundError, ApplicationError
from .result_projection import project_resource
from .ml_resource_context import reads
from .unit_resolution import project_units


def presentation_text(value):
    lines = [value["summary"]]
    if value["facts"].get("model_type"):
        lines.append("模型类型：" + value["facts"]["model_type"])
    if value["metrics"]:
        lines.append("验证结果：" + "；".join(f"{m['label']} = {m['value']:.5g}" + (f" {m['unit']}" if m.get("unit") else "") for m in value["metrics"]))
    lines.extend(value["notes"])
    return "\n".join(lines)


def run_artifacts(session, run):
    """Only already registered execution outputs; no discovery on read/save."""
    from materialsagent.infrastructure.db.ml_resources import references
    sources = ["invocation:" + r.invocation_run_id for r in run.executions if r.invocation_run_id]
    refs = session.scalars(select(references.c.document).where(references.c.conversation_id == run.conversation_id,
        references.c.actor_id == run.actor_id, references.c.document["source"].as_string().in_(sources))).all() if sources else []
    result = [{"attachment_id": r["reference_id"], "kind": r["resource_type"],
        "name": {"training_run": "训练结果", "prediction": "预测结果", "model": "模型评估", "dataset": "数据概况"}[r["resource_type"]]} for r in refs]
    for observation in run.observations:
        for asset in observation.artifacts:
            value = asset if isinstance(asset, dict) else asset.model_dump(mode="json")
            if value.get("asset_id"):
                result.append({"attachment_id": value["asset_id"], "kind": "image", "name": "结果图片"})
    return result


class ChatArtifacts:
    def __init__(self, sessions, resources):
        self.sessions, self.resources = sessions, resources

    def unit_annotations(self, actor, conversation, identity):
        """Read the frozen execution provenance, never create a semantic binding."""
        from materialsagent.infrastructure.db.agent import AgentExecutionRow, AgentRunRow
        ref = self.resources.reference(actor, conversation, identity)
        if ref["resource_type"] == "model" and ref["source"].startswith("lineage:"):
            ref = self.resources.reference(actor, conversation, ref["source"][len("lineage:"):])
        if ref["resource_type"] not in ("training_run", "model", "prediction"):
            return []
        with self.sessions() as session:
            rows = session.execute(select(AgentExecutionRow.invocation_run_id, AgentExecutionRow.document)
                .join(AgentRunRow, AgentExecutionRow.agent_run_id == AgentRunRow.agent_run_id)
                .where(AgentRunRow.actor_id == actor, AgentRunRow.conversation_id == conversation)).all()
            for invocation, document in rows:
                source = "invocation:" + invocation
                if ref["source"] == source or ref["source"].startswith(source + ":reconcile:"):
                    return document.get("unit_annotations", [])
        return []

    def messages(self, actor, conversation):
        with self.sessions() as session:
            lock_conversation(session, actor, conversation)
            rows = session.scalars(select(MessageRow).where(MessageRow.conversation_id == conversation,
                MessageRow.event_key.is_not(None)).order_by(MessageRow.created_at.desc(), MessageRow.message_id.desc()).limit(100)).all()
            return [self.message(row) for row in reversed(rows)]

    @staticmethod
    def message(row):
        content = row.structured_content or {}
        return {"message_id": row.message_id, "text": row.content_text, "created_at": row.created_at.isoformat(),
            "presentation": content.get("presentation"), "artifacts": content.get("artifacts", [])}

    def artifact(self, actor, conversation, message, identity):
        with self.sessions() as session:
            lock_conversation(session, actor, conversation)
            row = session.get(MessageRow, message)
            if row is None or row.actor_id != actor or row.conversation_id != conversation:
                raise ResourceNotFoundError()
            content = row.structured_content or {}
            target = next((a for a in [*content.get("attachments", []), *content.get("artifacts", [])]
                if a["attachment_id"] == identity), None)
            if target is None:
                raise ResourceNotFoundError()
            return dict(target)

    def view(self, actor, conversation, message, identity):
        target = self.artifact(actor, conversation, message, identity)
        if target["kind"] in ("image", "ebsd_image"):
            return {**target, "image_url": f"/api/v1/assets/{identity}/content", "downloads": [
                {"name": "下载图片", "url": f"/api/v1/conversations/{conversation}/messages/{message}/artifacts/{identity}/download/image"}]}
        with reads(10):
            value = self.resources.resource(actor, conversation, identity)
        # No lineage registration, selection, completion observation or model call.
        members = {"dataset": ["dataset.csv"], "prediction": ["predictions.csv"], "model": [], "training_run": []}[target["kind"]]
        projection = project_resource(value, kind=target["kind"])
        projection = project_units(projection, self.unit_annotations(actor, conversation, identity))
        if target["kind"] == "prediction" and value.get("status") == "SUCCEEDED":
            prediction = self.prediction(actor, conversation, identity)
            projection["facts"]["prediction_preview"] = [{"row": index + 1, "value": amount} for index, amount in enumerate(prediction["values"][:50])]
            if len(prediction["values"]) > 50:
                projection["notes"].append("详情仅展示前 50 行，完整结果可下载。")
        return {**target,
            "downloads": [{"name": "下载 CSV", "url": f"/api/v1/conversations/{conversation}/messages/{message}/artifacts/{identity}/download/{member}"} for member in members],
            "presentation": projection}

    def prediction(self, actor, conversation, identity):
        import json
        from .result_projection import number
        from .errors import DependencyUnavailableError
        spool, _ = self.resources.download(actor, conversation, identity, "predictions.json")
        try:
            result = json.loads(spool.read(2 * 1024**2 + 1))
            if not isinstance(result.get("values"), list) or not all(number(v) for v in result["values"]):
                raise ValueError()
            return {"values": result["values"], "target": result["target"], "unit": result.get("target_unit")}
        except (ValueError, KeyError, TypeError):
            raise DependencyUnavailableError(code="INVALID_PREDICTION_RESULT") from None
        finally:
            spool.close()

    def observe(self, actor, conversation, cursor=None):
        if self.resources is None or self.resources.client is None:
            return {"pending": False, "messages": self.messages(actor, conversation)}
        from materialsagent.infrastructure.db.ml_resources import references
        from materialsagent.infrastructure.db.agent import AgentExecutionRow, AgentRunRow
        with self.sessions() as session:
            owner = lock_conversation(session, actor, conversation, writable=True)
            epoch = owner.deletion_fence_version
            # Only tasks created by a confirmed Invocation in this conversation.
            created = session.execute(select(AgentExecutionRow.invocation_run_id, AgentExecutionRow.document)
                .join(AgentRunRow, AgentExecutionRow.agent_run_id == AgentRunRow.agent_run_id)
                .where(AgentRunRow.actor_id == actor, AgentRunRow.conversation_id == conversation)).all()
            sources = ["invocation:" + identity for identity, doc in created if doc["tool_name"] == "materials_ml_train_tabular_regression"
                or (doc["tool_name"] == "materials_ml_predict_with_model" and doc["status"] == "OUTCOME_UNKNOWN")]
            published = set(session.scalars(select(MessageRow.event_key).where(MessageRow.conversation_id == conversation,
                MessageRow.event_key.is_not(None))))
            refs = session.scalars(select(references.c.document).where(references.c.actor_id == actor,
                references.c.conversation_id == conversation, references.c.resource_type.in_(("training_run", "prediction")))
                .order_by(references.c.id)).all() if sources else []
            refs = [r for r in refs if any(r["source"] == source or r["source"].startswith(source + ":reconcile:") for source in sources)
                and "terminal:" + r["reference_id"] not in published]
            offset = next((i + 1 for i, r in enumerate(refs) if r["reference_id"] == cursor), 0) % max(1, len(refs))
            refs = refs[offset:] + refs[:offset]
        pending = len(refs) > 4
        for ref in refs[:4]:
            try:
                with reads(10):
                    value = self.resources.resource(actor, conversation, ref["reference_id"])
                    if value.get("status") not in ("SUCCEEDED", "FAILED", "CANCELLED"):
                        pending = True
                        continue
                    presentation = project_resource(value, kind=ref["resource_type"])
                    artifacts = [{"attachment_id": ref["reference_id"], "kind": ref["resource_type"], "name": presentation["title"]}]
                    if ref["resource_type"] == "training_run" and value["status"] == "SUCCEEDED":
                        model = self.resources.discover_model(actor, conversation, ref["reference_id"])
                        details = self.resources.resource(actor, conversation, model["reference_id"])
                        presentation = project_resource(details, kind="model")
                        presentation["summary"] = "模型训练完成。"
                        artifacts.append({"attachment_id": model["reference_id"], "kind": "model", "name": "模型评估"})
                presentation = project_units(presentation, self.unit_annotations(actor, conversation, ref["reference_id"]))
                with self.sessions.begin() as session:
                    owner = lock_conversation(session, actor, conversation, writable=True, expected_version=epoch)
                    key = "terminal:" + ref["reference_id"]
                    if session.scalar(select(MessageRow.message_id).where(MessageRow.conversation_id == conversation, MessageRow.event_key == key)):
                        continue
                    stamp = now()
                    session.add(MessageRow(message_id=identifier(), event_key=key, conversation_id=conversation, actor_id=actor,
                        task_id=None, request_id=identifier(), role="ASSISTANT", generation_source="AGENT",
                        content_text=presentation_text(presentation), structured_content={"contract": "chat-v1",
                            "presentation": presentation, "artifacts": artifacts,
                            "source": {"reference_id": ref["reference_id"], "state": value["status"]}},
                        llm_call_id=None, created_at=stamp))
                    owner.updated_at = stamp
            except ApplicationError:
                pending = True  # Unknown is not permission to re-dispatch.
        return {"pending": pending, "next_cursor": refs[min(4, len(refs)) - 1]["reference_id"] if refs else None,
                "messages": self.messages(actor, conversation)}
