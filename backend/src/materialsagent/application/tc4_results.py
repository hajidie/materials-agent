"""Commit the terminal batch from persisted item facts, preserving the Managed parent."""
from dataclasses import replace
from uuid import uuid4
from materialsagent.application.errors import ApplicationConflictError
from materialsagent.application.tool_execution import normalize_tool_output_summary
from materialsagent.domain.models.agent import now
from materialsagent.domain.models.tc4 import MODEL_VERSION, PREPROCESSING_VERSION, validate_batch
from materialsagent.domain.models.tool_result import ToolResult
from materialsagent.domain.models.result_asset_link import ResultAssetLink


def commit(workflow, actor, receipt):
    output, run_id = receipt.output, receipt.tool_run.tool_run_id
    validate_batch(output.data, output.status)
    with workflow.uow_factory() as uow:
        run = uow.tool_runs.get_owned_for_update(run_id, actor.actor_id)
        task = uow.tasks.get_owned_for_update(run.task_id, actor.actor_id)
        existing = uow.tool_results.get_for_tool_run(run_id)
        if existing is not None:
            from materialsagent.application.agent_tools import plain
            if plain(existing.data) != output.data:
                raise ApplicationConflictError()
            return workflow.load_current_for_task(actor, task_id=task.task_id)
        items = uow.tool_run_items.list_for_tool_run(run_id)
        if (run.current_status != "RUNNING" or task.current_status != "RUNNING"
                or run.output_summary != normalize_tool_output_summary(output)
                or [item.public() for item in items] != output.data["items"]
                or run.normalized_input_snapshot["image_asset_ids"] != [item.input_asset_id for item in items]):
            raise ApplicationConflictError()
        assets = []
        for item in items:
            for role in ("overlay", "mask") if item.status == "SUCCEEDED" else ():
                asset = uow.assets.get_owned(item.artifacts[role], actor.actor_id)
                if (asset is None or asset.current_status != "AVAILABLE" or asset.producer_tool_run_id != run_id
                        or asset.producer_tool_run_item_id != item.item_id or asset.input_asset_id != item.input_asset_id
                        or asset.encoding_rule != "tc4-" + role + "-png-v1"
                        or (asset.width, asset.height) != (item.statistics.width, item.statistics.height)):
                    raise ApplicationConflictError()
                assets.append(asset)
        timestamp = workflow._clock()
        result = ToolResult(result_id=str(uuid4()), task_id=task.task_id, tool_run_id=run_id, actor_id=actor.actor_id,
            status=output.status, requested_outputs=output.requested_outputs, completed_outputs=output.completed_outputs,
            failed_outputs=output.failed_outputs, data=output.data, warnings=list(output.warnings),
            provenance={"input_revision": run.input_revision_no, "input_assets": [
                {"asset_id": item.input_asset_id, "sha256": item.input_sha256, "ordinal": item.ordinal} for item in items],
                "material": "TC4", "model_version": output.model_bundle_id, "preprocessing_version": PREPROCESSING_VERSION,
                "actual_runtime_parameters": output.actual_runtime_parameters}, error=output.error,
            tool_id=run.tool_id, tool_version=run.tool_version, schema_hash=run.schema_hash, created_at=timestamp)
        uow.tool_results.add(result)
        for ordinal, asset in enumerate(assets):
            uow.result_asset_links.add(ResultAssetLink(result.result_id, asset.asset_id, ordinal, timestamp))
        completed_run = run.complete_from_result(completed_outputs=list(output.completed_outputs), failed_outputs=list(output.failed_outputs),
            completed_at=timestamp, error_code=output.error["code"] if output.error else None,
            safe_error_message=output.error["safe_message"] if output.error else None)
        completed_task = replace(task, current_status=result.status, selected_tool_run_id=run_id,
            selected_result_id=result.result_id, updated_at=timestamp, completed_at=timestamp,
            error_code=completed_run.error_code, safe_error_message=completed_run.safe_error_message)
        if uow.tool_runs.update(completed_run, expected_status="RUNNING") is None or uow.tasks.update(completed_task, expected_status="RUNNING") is None:
            raise ApplicationConflictError()
        uow.commit()
    return workflow.load_current_for_task(actor, task_id=task.task_id)
