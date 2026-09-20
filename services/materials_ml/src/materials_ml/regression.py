from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version
import math
import sys

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import KFold, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from .contracts import (CV_FOLDS, ENGINE_VERSION, PACKAGE_VERSION, CVFold, DatasetIdentity,
                        PredictionResult, SplitManifest, TrainingSpec)
from .errors import EngineError
from .tabular import (analyze_table, check_table, numeric_frame, table_identity,
                      validate_split_binding)

DEPENDENCIES = ("numpy", "pandas", "scipy", "scikit-learn", "joblib", "threadpoolctl")


def environment_versions() -> dict:
    return {"python": ".".join(map(str, sys.version_info[:3])),
            **{name: version(name) for name in DEPENDENCIES}}


def parameter_json(value: object) -> object:
    """Lossless JSON representation of this engine's fixed estimator parameters."""
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, dict):
        return {key: parameter_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [parameter_json(item) for item in value]
    if type(value) is float and not math.isfinite(value):
        return {"special_float": "nan" if math.isnan(value) else str(value)}
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise EngineError("INVALID_MODEL_PARAMETERS", "Unsupported estimator parameter value.")


def build_pipeline(spec: TrainingSpec) -> Pipeline:
    steps = [("imputer", SimpleImputer(strategy="median"))]
    if spec.algorithm == "LR":
        steps.extend([("scaler", StandardScaler()),
                      ("estimator", LinearRegression(fit_intercept=True, n_jobs=1))])
    else:
        steps.append(("estimator", RandomForestRegressor(
            n_estimators=100, random_state=spec.random_state, n_jobs=1)))
    return Pipeline(steps)


def preprocessing_parameters(pipeline: Pipeline) -> dict:
    return {name: parameter_json(step.get_params(deep=True))
            for name, step in pipeline.steps[:-1]}


def _require_r2_target(y: np.ndarray) -> None:
    if len(y) < 2:
        raise EngineError("INSUFFICIENT_SAMPLES", "Every R-squared partition requires at least two rows.")
    if not np.isfinite(y).all():
        raise EngineError("INVALID_TARGET", "Target values must be finite and nonmissing.")
    if np.all(y == y[0]):
        raise EngineError("CONSTANT_TARGET_PARTITION", "R-squared is undefined for a constant target partition.")


def metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    _require_r2_target(y_true)
    if y_true.shape != y_pred.shape or not np.isfinite(y_pred).all():
        raise EngineError("INVALID_PREDICTIONS", "Predictions must be finite and match target shape.")
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        mse = float(mean_squared_error(y_true, y_pred))
        result = {"r2": float(r2_score(y_true, y_pred, force_finite=False)),
                  "mae": float(mean_absolute_error(y_true, y_pred)),
                  "mse": mse, "rmse": math.sqrt(mse)}
    if not all(math.isfinite(value) for value in result.values()):
        raise EngineError("NON_FINITE_METRICS", "Evaluation produced an undefined or overflowing metric.")
    return result


def make_splits(table: pd.DataFrame, spec: TrainingSpec) -> SplitManifest:
    return split_positions(table_identity(table), spec)


def split_positions(identity: DatasetIdentity, spec: TrainingSpec) -> SplitManifest:
    """Reconstruct the fixed split from identity and configuration, without data access."""
    try:
        train, test = train_test_split(np.arange(identity.row_count), test_size=spec.test_size,
                                      random_state=spec.random_state, shuffle=True)
    except ValueError:
        raise EngineError("INSUFFICIENT_SAMPLES", "The requested train/test split is not feasible.") from None
    if len(train) < CV_FOLDS * 2 or len(test) < 2:
        raise EngineError("INSUFFICIENT_SAMPLES", "Test and every 5-fold validation set require at least two rows.")
    cv = KFold(n_splits=CV_FOLDS, shuffle=True, random_state=spec.random_state)
    folds = tuple(CVFold(tuple(int(i) for i in train[a]), tuple(int(i) for i in train[b]))
                  for a, b in cv.split(train))
    return SplitManifest(identity, tuple(map(int, train)), tuple(map(int, test)), folds)


@dataclass(frozen=True)
class ModelPackage:
    pipeline: Pipeline
    manifest: dict
    report: dict
    splits: SplitManifest


def _prediction_record(indices: tuple[int, ...], y: np.ndarray, predicted: np.ndarray) -> dict:
    return {"row_positions": list(indices), "y_true": y.tolist(),
            "y_pred": predicted.tolist(), "metrics": metrics(y, predicted)}


def _prepare_training(table: pd.DataFrame, spec: TrainingSpec):
    check_table(table)
    table = table.copy(deep=True)
    analysis = analyze_table(table)
    selected = numeric_frame(table, (*spec.features, spec.target))
    X = selected.loc[:, list(spec.features)]
    y = selected[spec.target].to_numpy()
    if not np.isfinite(y).all():
        raise EngineError("INVALID_TARGET", "Targets cannot be missing or infinite.")
    splits = make_splits(table, spec)
    # Validate all folds before the first fit, including fold-local imputation feasibility.
    fit_partitions = [splits.train, *(f.train for f in splits.folds)]
    score_partitions = [splits.train, splits.test, *(f.validation for f in splits.folds)]
    for indices in score_partitions:
        _require_r2_target(y[list(indices)])
    for indices in fit_partitions:
        if X.iloc[list(indices)].isna().all().any():
            raise EngineError("EMPTY_TRAINING_FEATURE", "A feature is entirely missing in a training partition.")
    return table, analysis, X, y, splits


def validate_training_input(table: pd.DataFrame, spec: TrainingSpec) -> SplitManifest:
    """Run exactly the training preflight without fitting any estimator."""
    return _prepare_training(table, spec)[-1]


def train_regression(table: pd.DataFrame, spec: TrainingSpec) -> ModelPackage:
    """Fit five independent folds, then fit the exported pipeline on external train only."""
    table, analysis, X, y, splits = _prepare_training(table, spec)

    template = build_pipeline(spec)
    cv_records = []
    # Applies to this synchronous computation, not global multiprocessing/GPU configuration.
    with threadpool_limits(limits=1):
        for fold in splits.folds:
            pipeline = clone(template)
            pipeline.fit(X.iloc[list(fold.train)], y[list(fold.train)])
            predictions = pipeline.predict(X.iloc[list(fold.validation)])
            cv_records.append(_prediction_record(fold.validation, y[list(fold.validation)], predictions))
        final = clone(template)
        final.fit(X.iloc[list(splits.train)], y[list(splits.train)])
        train_record = _prediction_record(splits.train, y[list(splits.train)],
                                          final.predict(X.iloc[list(splits.train)]))
        test_record = _prediction_record(splits.test, y[list(splits.test)],
                                         final.predict(X.iloc[list(splits.test)]))
    summary = {key: {"mean": float(np.mean([r["metrics"][key] for r in cv_records])),
                     "std": float(np.std([r["metrics"][key] for r in cv_records], ddof=0))}
               for key in train_record["metrics"]}
    if any(not math.isfinite(v) for entry in summary.values() for v in entry.values()):
        raise EngineError("NON_FINITE_METRICS", "CV aggregation overflowed.")
    warnings = ["IID_NOT_VERIFIED"]
    if analysis["duplicate_row_count"]:
        warnings.append("DUPLICATE_ROWS_PRESENT")
    report = {"dataset": splits.dataset.to_dict(), "train": train_record, "test": test_record,
              "cv": {"folds": cv_records, "summary": summary, "std_ddof": 0,
                     "meaning": "score_variation_not_predictive_uncertainty"},
              "units": dict(spec.units), "warnings": warnings,
              "input_diagnostics": analysis}
    manifest = {"package_version": PACKAGE_VERSION, "engine_version": ENGINE_VERSION,
                "environment": environment_versions(), "dataset": splits.dataset.to_dict(),
                "spec": spec.to_dict(), "cv": {"folds": CV_FOLDS, "shuffle": True},
                "feature_schema": [{"name": c, "dtype": str(table[c].dtype)} for c in spec.features],
                "target_dtype": str(table[spec.target].dtype),
                "estimator_class": type(final.named_steps["estimator"]).__name__,
                "estimator_parameters": parameter_json(final.named_steps["estimator"].get_params(deep=True)),
                "preprocessing": preprocessing_parameters(final),
                "target_transform": "none", "fit_partition": "external_train"}
    return ModelPackage(final, manifest, report, splits)


def predict(package: ModelPackage, table: pd.DataFrame) -> PredictionResult:
    check_table(table)
    spec = TrainingSpec(**package.manifest["spec"])
    if set(table.columns) != set(spec.features):
        raise EngineError("PREDICTION_COLUMNS_MISMATCH", "Prediction requires exactly the saved feature names.")
    X = numeric_frame(table, spec.features)
    with threadpool_limits(limits=1):
        values = package.pipeline.predict(X)
    if values.shape != (len(table),) or not np.isfinite(values).all():
        raise EngineError("INVALID_PREDICTIONS", "Model produced invalid predictions.")
    return PredictionResult(tuple(map(float, values)), tuple(range(len(table))),
                            spec.target, spec.units.get(spec.target))


def evaluate_test(package: ModelPackage, original_table: pd.DataFrame) -> dict:
    """Replay only the saved external test evaluation after checking the full input identity."""
    validate_split_binding(original_table, package.splits)
    spec = TrainingSpec(**package.manifest["spec"])
    indices = package.splits.test
    selected = original_table.iloc[list(indices)]
    predicted = np.asarray(predict(package, selected.loc[:, list(spec.features)]).values)
    y = numeric_frame(selected, (spec.target,))[spec.target].to_numpy()
    return _prediction_record(indices, y, predicted)
