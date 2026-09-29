from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path
import sys

from materialsagent.application.tool_registry import ToolRegistry
from materialsagent.application.tools import build_tool_registry
from materialsagent.application.zta35g_tool import build_zta35g_tool_definition
from materialsagent.application.native_tool_protocol import sdk_tools
from materialsagent.application.agent_tools import plain
from jsonschema import Draft202012Validator
from backend.tests.support.heterogeneous_tools import (
    build_ml_training_test_definition,
)


def test_two_definition_registry_keeps_unrelated_contracts_and_hashes() -> None:
    registry = ToolRegistry(
        (
            build_zta35g_tool_definition(),
            build_ml_training_test_definition(),
        )
    )

    snapshot = registry.routing_snapshot()
    entries = {entry.tool_id: entry for entry in snapshot.entries}
    assert set(entries) == {"zta35g_sem_virtual_lab", "ml_training_test"}
    assert entries["ml_training_test"].input_schema["required"] == (
        "dataset",
        "task_type",
        "split_ratio",
    )
    assert entries["ml_training_test"].candidate_input_schema["required"] == (
        "dataset",
        "task_type",
        "split_ratio",
        "target_column",
        "shuffle",
    )
    assert (
        entries["ml_training_test"].schema_hash
        != entries["zta35g_sem_virtual_lab"].schema_hash
    )
    assert set(entries["ml_training_test"].input_schema["properties"]) == {
        "dataset",
        "task_type",
        "split_ratio",
        "target_column",
        "shuffle",
    }


def test_generic_candidate_validation_accepts_ml_training_fields() -> None:
    definition = build_ml_training_test_definition()
    tool = sdk_tools([{"tool_name": definition.tool_id, "description": "ML training",
                      "schema": plain(definition.proposal_schema), "resource_parameters": []}], ask_user=False)[0]
    arguments = {
            "dataset": "dataset_fixture_1",
            "task_type": "regression",
            "split_ratio": 0.8,
            "target_column": "yield_strength",
            "shuffle": True,
    }
    Draft202012Validator(tool.args_schema).validate(arguments)


def test_production_composition_registers_managed_and_safe_standard_tools() -> None:
    registry = build_tool_registry()

    assert {definition.tool_id for definition in registry.list_registered()} == {
        "zta35g_sem_virtual_lab",
        "materials_unit_conversion",
        "ebsd_yield_strength_predictor",
    }


def test_test_support_loads_when_zta_imports_are_forbidden(monkeypatch) -> None:
    support_path = (
        Path(__file__).resolve().parents[1] / "support" / "heterogeneous_tools.py"
    )
    real_import = builtins.__import__

    def import_without_zta(name, globals=None, locals=None, fromlist=(), level=0):
        if "zta35g" in name.lower():
            raise AssertionError("The heterogeneous test support imported ZTA code.")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_zta)
    module_name = "_heterogeneous_tools_import_boundary"
    spec = importlib.util.spec_from_file_location(module_name, support_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)

    spec.loader.exec_module(module)

    assert module.build_ml_training_test_definition().tool_id == "ml_training_test"
