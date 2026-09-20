import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
import pytest
from sklearn.pipeline import Pipeline

from materials_ml import (EngineError, TrainingSpec, evaluate_test, load_package,
                          predict, save_package, train_regression)


@pytest.fixture
def model(table):
    return train_regression(table, TrainingSpec(("x", "z"), "strength_MPa"))


@pytest.mark.parametrize("algorithm", ["LR", "RF"])
@pytest.mark.parametrize("units", [{}, {"strength_MPa": "explicit-only", "x": "unit-x"}])
def test_roundtrip_model_report_indices_units_and_prediction(table, tmp_path, algorithm, units, monkeypatch):
    model = train_regression(table, TrainingSpec(("x", "z"), "strength_MPa", algorithm=algorithm, units=units))
    before = predict(model, table[["x", "z"]])
    location = save_package(model, tmp_path / "model")
    manifest = json.loads((location / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["estimator_parameters"] == model.pipeline.named_steps["estimator"].get_params(deep=True)
    assert manifest["dataset"] == model.splits.dataset.to_dict()
    assert set(manifest["files"]) == {"pipeline.joblib", "splits.json", "evaluation.json"}
    restored = load_package(location, trusted=True)
    assert restored.manifest == model.manifest and restored.report == model.report
    assert restored.splits == model.splits
    monkeypatch.setattr(Pipeline, "fit", lambda *a, **k: pytest.fail("Prediction must never fit"))
    after = predict(restored, table[["z", "x"]])
    np.testing.assert_allclose(after.values, before.values, rtol=1e-10, atol=1e-12)
    assert after.unit == units.get("strength_MPa")
    assert evaluate_test(restored, table) == model.report["test"]
    # Predictions on a different table do not require the original training digest.
    assert len(predict(restored, table[["x", "z"]].iloc[:3] + 1).values) == 3


def rewrite_json(location, filename, mutate, *, update_ledger=True):
    target = location / filename
    value = json.loads(target.read_text(encoding="utf-8"))
    mutate(value)
    payload = json.dumps(value, allow_nan=False).encode("utf-8")
    target.write_bytes(payload)
    if filename != "manifest.json" and update_ledger:
        manifest_path = location / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][filename]["size_bytes"] = len(payload)
        manifest["files"][filename]["sha256"] = hashlib.sha256(payload).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("corruption", ["model", "missing", "hash", "size", "environment", "version",
    "data_hash", "data_count", "indices", "cv_rows", "metric", "summary", "parameters", "feature_schema",
    "seed", "test_size", "extra_file"])
def test_corruption_rejected_before_deserialization(model, tmp_path, monkeypatch, corruption):
    location = save_package(model, tmp_path / "model")
    if corruption == "model":
        (location / "pipeline.joblib").write_bytes(b"invalid model")
    elif corruption == "missing":
        (location / "splits.json").unlink()
    elif corruption == "hash":
        rewrite_json(location, "manifest.json", lambda m: m["files"]["pipeline.joblib"].update(sha256="0" * 64))
    elif corruption == "size":
        rewrite_json(location, "manifest.json", lambda m: m["files"]["pipeline.joblib"].update(size_bytes=0))
    elif corruption == "environment":
        rewrite_json(location, "manifest.json", lambda m: m["environment"].update({"scikit-learn": "unsupported"}))
    elif corruption == "version":
        rewrite_json(location, "manifest.json", lambda m: m.update(package_version="future"))
    elif corruption == "data_hash":
        rewrite_json(location, "splits.json", lambda m: m["dataset"].update(sha256="0" * 64))
    elif corruption == "data_count":
        rewrite_json(location, "splits.json", lambda m: m["dataset"].update(row_count=999))
    elif corruption == "indices":
        rewrite_json(location, "splits.json", lambda m: m["test"].append(m["train"][0]))
    elif corruption == "cv_rows":
        rewrite_json(location, "evaluation.json", lambda m: m["cv"]["folds"][0]["row_positions"].reverse())
    elif corruption == "metric":
        rewrite_json(location, "evaluation.json", lambda m: m["test"]["metrics"].update(r2=-999))
    elif corruption == "summary":
        rewrite_json(location, "evaluation.json", lambda m: m["cv"]["summary"]["r2"].update(mean=-999))
    elif corruption == "parameters":
        rewrite_json(location, "manifest.json", lambda m: m["estimator_parameters"].pop("positive"))
    elif corruption == "feature_schema":
        rewrite_json(location, "manifest.json", lambda m: m["feature_schema"].reverse())
    elif corruption == "seed":
        rewrite_json(location, "manifest.json", lambda m: m["spec"].update(random_state=123))
    elif corruption == "test_size":
        rewrite_json(location, "manifest.json", lambda m: m["spec"].update(test_size=0.4))
    else:
        (location / "extra.txt").write_text("unexpected", encoding="utf-8")
    loads = []
    monkeypatch.setattr(joblib, "load", lambda *a, **k: loads.append(1))
    with pytest.raises(EngineError):
        load_package(location, trusted=True)
    assert loads == []


def test_load_requires_explicit_trusted_origin(model, tmp_path, monkeypatch):
    location = save_package(model, tmp_path / "model")
    monkeypatch.setattr(joblib, "load", lambda *a, **k: pytest.fail("Untrusted data reached pickle"))
    with pytest.raises(EngineError) as error:
        load_package(location)
    assert error.value.code == "UNTRUSTED_PACKAGE"


def test_verified_bytes_are_deserialized_not_a_mutable_path(model, tmp_path, monkeypatch):
    location = save_package(model, tmp_path / "model")
    load = joblib.load

    def replace_after_verification(stream):
        assert not isinstance(stream, (Path, str))
        (location / "pipeline.joblib").write_bytes(b"changed after verification")
        return load(stream)

    monkeypatch.setattr(joblib, "load", replace_after_verification)
    assert load_package(location, trusted=True).manifest == model.manifest


def test_failed_save_never_publishes_partial_package(model, tmp_path, monkeypatch):
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    def failed_dump(model, path, **kwargs):
        path.write_bytes(b"partial")
        raise OSError("simulated write failure")

    monkeypatch.setattr(joblib, "dump", failed_dump)
    with pytest.raises(EngineError) as error:
        save_package(model, tmp_path / "model")
    assert error.value.code == "PACKAGE_SAVE_FAILED"
    assert list(tmp_path.iterdir()) == [unrelated]


def test_save_never_overwrites_existing_data(model, tmp_path):
    location = save_package(model, tmp_path / "model")
    before = {p.name: p.read_bytes() for p in location.iterdir()}
    with pytest.raises(EngineError) as error:
        save_package(model, location)
    assert error.value.code == "PACKAGE_ALREADY_EXISTS"
    assert before == {p.name: p.read_bytes() for p in location.iterdir()}


def test_mutated_estimator_does_not_publish_misleading_manifest(model, tmp_path):
    model.pipeline.named_steps["estimator"].set_params(positive=True)
    with pytest.raises(EngineError):
        save_package(model, tmp_path / "model")
    assert not (tmp_path / "model").exists()
