from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory

import joblib
import numpy as np
from sklearn.pipeline import Pipeline
from sklearn.utils.validation import check_is_fitted

from .contracts import CV_FOLDS, ENGINE_VERSION, PACKAGE_VERSION, SplitManifest, TrainingSpec
from .errors import EngineError
from .regression import (ModelPackage, build_pipeline, environment_versions, metrics,
                         parameter_json, preprocessing_parameters, split_positions)

_FILES = {"pipeline.joblib": "application/octet-stream", "evaluation.json": "application/json",
          "splits.json": "application/json"}
_MANIFEST_KEYS = {"package_version", "engine_version", "environment", "dataset", "spec", "cv",
                  "feature_schema", "target_dtype", "estimator_class", "estimator_parameters",
                  "preprocessing", "target_transform", "fit_partition"}
_MAX_JSON_BYTES = 32 * 1024 * 1024
_MAX_MODEL_BYTES = 256 * 1024 * 1024


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _parse_json(payload: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key.")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("Nonfinite JSON number.")

    if len(payload) > _MAX_JSON_BYTES:
        raise ValueError("Metadata exceeds the package limit.")
    result = json.loads(payload, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(result, dict):
        raise ValueError("Metadata must be an object.")
    return result


def _read(path: Path, limit: int) -> bytes:
    if path.is_symlink():
        raise EngineError("INVALID_PACKAGE", "Package members cannot be symbolic links.")
    with path.open("rb") as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise EngineError("INVALID_PACKAGE", "Package member exceeds the supported size.")
    return payload


def _check_metadata(manifest: dict, report: dict, splits: SplitManifest) -> TrainingSpec:
    if set(manifest) != _MANIFEST_KEYS:
        raise EngineError("INVALID_PACKAGE", "Unexpected model manifest fields.")
    if manifest["package_version"] != PACKAGE_VERSION or manifest["engine_version"] != ENGINE_VERSION:
        raise EngineError("UNSUPPORTED_PACKAGE_VERSION", "Unsupported package or Engine version.")
    if manifest["environment"] != environment_versions():
        raise EngineError("INCOMPATIBLE_ENVIRONMENT", "Package requires its recorded Python and dependency versions.")
    spec = TrainingSpec(**manifest["spec"])
    splits.validate()
    if splits != split_positions(splits.dataset, spec):
        raise EngineError("INVALID_SPLIT", "Saved positions disagree with the declared seed and split/CV configuration.")
    if (manifest["dataset"] != splits.dataset.to_dict() or report["dataset"] != manifest["dataset"]
            or report["input_diagnostics"]["dataset"] != manifest["dataset"]):
        raise EngineError("DATASET_MISMATCH", "Package metadata and indices refer to different input data.")
    expected = build_pipeline(spec)
    if (manifest["cv"] != {"folds": CV_FOLDS, "shuffle": True}
            or manifest["target_transform"] != "none" or manifest["fit_partition"] != "external_train"
            or manifest["estimator_class"] != type(expected.named_steps["estimator"]).__name__
            or manifest["estimator_parameters"] != parameter_json(expected.named_steps["estimator"].get_params(deep=True))
            or manifest["preprocessing"] != preprocessing_parameters(expected)):
        raise EngineError("INVALID_PACKAGE", "Model configuration is outside the fixed LR/RF contract.")
    if (not isinstance(manifest["feature_schema"], list)
            or [c["name"] for c in manifest["feature_schema"]] != list(spec.features)
            or any(set(c) != {"name", "dtype"} or type(c["dtype"]) is not str for c in manifest["feature_schema"])
            or type(manifest["target_dtype"]) is not str or report["units"] != dict(spec.units)):
        raise EngineError("INVALID_PACKAGE", "Saved feature or unit metadata is inconsistent.")
    cv = report["cv"]
    if (len(cv["folds"]) != CV_FOLDS or cv["std_ddof"] != 0
            or cv["meaning"] != "score_variation_not_predictive_uncertainty"):
        raise EngineError("INVALID_PACKAGE", "Invalid CV report.")
    records = [report["train"], report["test"], *cv["folds"]]
    partitions = [splits.train, splits.test, *(f.validation for f in splits.folds)]
    for record, positions in zip(records, partitions, strict=True):
        if (record["row_positions"] != list(positions) or len(record["y_true"]) != len(positions)
                or len(record["y_pred"]) != len(positions)):
            raise EngineError("INVALID_PACKAGE", "Evaluation rows do not match the bound split.")
        actual = metrics(np.asarray(record["y_true"], dtype=float), np.asarray(record["y_pred"], dtype=float))
        if actual != record["metrics"]:
            raise EngineError("INVALID_PACKAGE", "Metrics do not match the recorded predictions.")
    summary = {key: {"mean": float(np.mean([r["metrics"][key] for r in cv["folds"]])),
                     "std": float(np.std([r["metrics"][key] for r in cv["folds"]], ddof=0))}
               for key in records[0]["metrics"]}
    if summary != cv["summary"] or not all(math.isfinite(v) for s in summary.values() for v in s.values()):
        raise EngineError("INVALID_PACKAGE", "CV summary is inconsistent.")
    return spec


def _check_pipeline(package: ModelPackage, spec: TrainingSpec) -> None:
    expected = build_pipeline(spec)
    actual = package.pipeline
    if (type(actual) is not Pipeline or [n for n, _ in actual.steps] != [n for n, _ in expected.steps]
            or any(type(a) is not type(b) for (_, a), (_, b) in zip(actual.steps, expected.steps, strict=True))):
        raise EngineError("INVALID_PACKAGE", "Unexpected Pipeline type or steps.")
    check_is_fitted(actual)
    if (list(actual.feature_names_in_) != list(spec.features)
            or parameter_json(actual.named_steps["estimator"].get_params(deep=True)) != package.manifest["estimator_parameters"]
            or preprocessing_parameters(actual) != package.manifest["preprocessing"]):
        raise EngineError("INVALID_PACKAGE", "Pipeline disagrees with its saved input or parameter contract.")


def save_package(package: ModelPackage, destination: str | Path) -> Path:
    """Publish a new local directory, never overwrite an existing package."""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise EngineError("PACKAGE_ALREADY_EXISTS", "Package destination already exists.")
    try:
        spec = _check_metadata(package.manifest, package.report, package.splits)
        _check_pipeline(package, spec)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Only this newly-created temporary directory is cleaned on failure.
        with TemporaryDirectory(prefix=".ml-package-", dir=destination.parent) as temp:
            staging = Path(temp)
            joblib.dump(package.pipeline, staging / "pipeline.joblib", compress=3)
            (staging / "evaluation.json").write_bytes(_json_bytes(package.report))
            (staging / "splits.json").write_bytes(_json_bytes(package.splits.to_dict()))
            files = {}
            for name, media_type in _FILES.items():
                payload = _read(staging / name, _MAX_MODEL_BYTES if name.endswith("joblib") else _MAX_JSON_BYTES)
                files[name] = {"sha256": hashlib.sha256(payload).hexdigest(),
                               "size_bytes": len(payload), "media_type": media_type}
            manifest = {**package.manifest, "files": files}
            (staging / "manifest.json").write_bytes(_json_bytes(manifest))
            # Validate exactly the bytes being published before making the directory visible.
            load_package(staging, trusted=True)
            staging.rename(destination)
        return destination
    except EngineError:
        raise
    except Exception:
        raise EngineError("PACKAGE_SAVE_FAILED", "Model package could not be published.") from None


def load_package(source: str | Path, *, trusted: bool = False) -> ModelPackage:
    """Load only caller-trusted Engine output. Hashes detect corruption, not forgery."""
    if trusted is not True:
        raise EngineError("UNTRUSTED_PACKAGE", "Only trusted Engine-generated packages may be deserialized.")
    try:
        source = Path(source)
        if source.is_symlink() or not source.is_dir():
            raise EngineError("INVALID_PACKAGE", "A package directory is required.")
        if {p.name for p in source.iterdir()} != {*_FILES, "manifest.json"}:
            raise EngineError("INVALID_PACKAGE", "Package members are missing or unexpected.")
        manifest = _parse_json(_read(source / "manifest.json", _MAX_JSON_BYTES))
        if set(manifest) != _MANIFEST_KEYS | {"files"} or set(manifest["files"]) != set(_FILES):
            raise EngineError("INVALID_PACKAGE", "Invalid manifest or member list.")
        payloads = {}
        for name, media_type in _FILES.items():
            payload = _read(source / name, _MAX_MODEL_BYTES if name.endswith("joblib") else _MAX_JSON_BYTES)
            if manifest["files"][name] != {"sha256": hashlib.sha256(payload).hexdigest(),
                                           "size_bytes": len(payload), "media_type": media_type}:
                raise EngineError("PACKAGE_INTEGRITY_ERROR", "Package content does not match its integrity record.")
            payloads[name] = payload
        report = _parse_json(payloads["evaluation.json"])
        splits = SplitManifest.from_dict(_parse_json(payloads["splits.json"]))
        metadata = {key: value for key, value in manifest.items() if key != "files"}
        spec = _check_metadata(metadata, report, splits)
        # Deserialize the already-verified bytes, not a path which could change after hashing.
        pipeline = joblib.load(io.BytesIO(payloads["pipeline.joblib"]))
        package = ModelPackage(pipeline, metadata, report, splits)
        _check_pipeline(package, spec)
        return package
    except EngineError:
        raise
    except Exception:
        raise EngineError("INVALID_PACKAGE", "Model package is malformed or unreadable.") from None
