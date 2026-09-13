"""Input and provenance contracts; the Managed commit protocol remains unchanged."""
from materialsagent.application.errors import ApplicationConflictError
from materialsagent.application.zta35g_input import PARAMETER_FIELDS
from materialsagent.domain.models.managed_contracts import EBSD_TOOL_ID


def sem_source(unit_of_work, task, tool_run, revision):
    normalized_input = revision.normalized_input
    execution_process_parameters = tool_run.execution_input.get(
        "process_parameters"
    )
    expected_requested_outputs = list(tool_run.requested_outputs)
    if (
        normalized_input.get("material") != "ZTA35G"
        or normalized_input.get("requested_outputs")
        != expected_requested_outputs
        or tool_run.execution_input.get("requested_outputs")
        != expected_requested_outputs
        or not isinstance(execution_process_parameters, dict)
    ):
        raise ApplicationConflictError(task_id=task.task_id)
    expected_units = {
        "solution_temperature": "°C",
        "solution_time": "h",
        "aging_temperature": "°C",
        "aging_time": "h",
    }
    normalized_process_parameters: dict[str, object] = {}
    for field_name in PARAMETER_FIELDS:
        parameter = normalized_input.get(field_name)
        if (
            not isinstance(parameter, dict)
            or set(parameter) != {"value", "unit"}
            or parameter["unit"] != expected_units[field_name]
            or field_name not in execution_process_parameters
            or parameter["value"]
            != execution_process_parameters[field_name]
        ):
            raise ApplicationConflictError(task_id=task.task_id)
        normalized_process_parameters[field_name] = dict(parameter)
    return normalized_process_parameters


def ebsd_source(unit_of_work, task, tool_run, revision):
    value = revision.normalized_input
    if (set(value) != {"material", "ebsd_asset_id", "requested_outputs"}
            or value["material"] != "Inconel 625" or value["requested_outputs"] != ["yield_strength"]
            or tool_run.requested_outputs != ["yield_strength"]
            or tool_run.execution_input.get("requested_outputs") != ["yield_strength"]
            or tool_run.execution_input.get("process_parameters") != {}
            or tool_run.execution_input.get("input_assets") != {"ebsd_asset_id": value["ebsd_asset_id"]}):
        raise ApplicationConflictError(task_id=task.task_id)
    asset = unit_of_work.assets.get_owned(value["ebsd_asset_id"], task.actor_id)
    if (asset is None or asset.conversation_id != task.conversation_id or asset.asset_type != "ebsd_image"
            or asset.source_type != "UPLOADED" or asset.current_status != "AVAILABLE"):
        raise ApplicationConflictError(task_id=task.task_id)
    return {"input_asset": {"asset_id": asset.asset_id, "sha256": asset.sha256},
        "material": "Inconel 625", "model_version": tool_run.model_bundle_id,
        "preprocessing_version": "rgb-tensor-resize128-v1"}


def source_for(unit_of_work, task, tool_run, revision):
    contract = ebsd_source if tool_run.tool_id == EBSD_TOOL_ID else sem_source
    return contract(unit_of_work, task, tool_run, revision)


def provenance_for(tool_id, revision, parameters, actual):
    source = parameters if tool_id == EBSD_TOOL_ID else {"normalized_process_parameters": parameters}
    return {"input_revision": revision, **source, "actual_runtime_parameters": dict(actual)}
