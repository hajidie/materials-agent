from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from sklearn.impute import SimpleImputer
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.pipeline import Pipeline

from materials_ml import (EngineError, TrainingSpec, analyze_table, evaluate_test,
                          predict, read_csv, table_identity, train_regression,
                          validate_split_binding)
from materials_ml.contracts import CVFold, SplitManifest
from materials_ml.regression import make_splits, metrics, parameter_json


def spec(**kwargs):
    return TrainingSpec(("x", "z"), "strength_MPa", **kwargs)


@pytest.mark.parametrize("algorithm", ["LR", "RF"])
def test_training_predictions_metrics_and_complete_actual_parameters(table, algorithm):
    original = table.copy(deep=True)
    result = train_regression(table, spec(algorithm=algorithm))
    pd.testing.assert_frame_equal(table, original)
    for record in (result.report["train"], result.report["test"], *result.report["cv"]["folds"]):
        y, pred = np.array(record["y_true"]), np.array(record["y_pred"])
        assert record["metrics"] == {
            "r2": r2_score(y, pred), "mae": mean_absolute_error(y, pred),
            "mse": mean_squared_error(y, pred), "rmse": np.sqrt(mean_squared_error(y, pred))}
    estimator = result.pipeline.named_steps["estimator"]
    actual = parameter_json(estimator.get_params(deep=True))
    assert result.manifest["estimator_parameters"] == actual
    assert result.manifest["estimator_parameters"]["n_jobs"] == 1
    if algorithm == "LR":
        assert result.report["test"]["metrics"]["r2"] == pytest.approx(1)
        np.testing.assert_allclose(predict(result, table[["x", "z"]]).values,
                                   table.strength_MPa, rtol=1e-10, atol=1e-12)
        assert "positive" in actual  # A non-overridden estimator parameter.
    else:
        assert actual["n_estimators"] == 100 and actual["random_state"] == 42
        assert "bootstrap" in actual and "max_features" in actual
    assert evaluate_test(result, table) == result.report["test"]


def test_r2_is_not_symmetric_and_error_metrics_use_original_scale():
    y, predictions = np.array([0., 10., 20.]), np.array([1., 9., 12.])
    result = metrics(y, predictions)
    assert result["r2"] == pytest.approx(1 - 66 / 200)
    assert result["r2"] != pytest.approx(r2_score(predictions, y))
    assert result["mse"] == 22 and result["mae"] == pytest.approx(10 / 3)
    assert result["rmse"] == pytest.approx(np.sqrt(22))


@pytest.mark.parametrize("algorithm", ["LR", "RF"])
def test_fixed_seed_and_cv_are_reproducible(table, algorithm):
    one = train_regression(table, spec(algorithm=algorithm))
    two = train_regression(table, spec(algorithm=algorithm))
    assert one.splits == two.splits
    assert one.report == two.report
    assert predict(one, table[["x", "z"]]) == predict(two, table[["x", "z"]])


def test_each_cv_imputer_fits_only_its_train_rows_and_final_model_excludes_test(table, monkeypatch):
    splits = make_splits(table, spec())
    table.loc[list(splits.test), "x"] = 1e6  # Would visibly shift a leaked scaler/imputer.
    table.loc[splits.train[0], "z"] = np.nan
    calls = []
    fit = SimpleImputer.fit

    def tracked(self, X, y=None):
        calls.append(X.copy(deep=True))
        return fit(self, X, y)

    monkeypatch.setattr(SimpleImputer, "fit", tracked)
    result = train_regression(table, spec())
    assert len(calls) == 6
    partitions = [f.train for f in result.splits.folds] + [result.splits.train]
    for frame, positions in zip(calls, partitions, strict=True):
        pd.testing.assert_frame_equal(frame, table.iloc[list(positions)][["x", "z"]])
    expected = table.iloc[list(splits.train)][["x", "z"]]
    np.testing.assert_allclose(result.pipeline.named_steps["imputer"].statistics_, expected.median())
    np.testing.assert_allclose(result.pipeline.named_steps["scaler"].mean_, expected.fillna(expected.median()).mean())
    assert result.pipeline.named_steps["scaler"].n_samples_seen_ == len(splits.train)


@pytest.mark.parametrize("rows,test_size", [(1, .2), (12, .2), (40, .025)])
def test_insufficient_partitions_rejected_before_any_fit(table, rows, test_size, monkeypatch):
    fits = []
    monkeypatch.setattr(Pipeline, "fit", lambda *a, **k: fits.append(1))
    with pytest.raises(EngineError) as error:
        train_regression(table.iloc[:rows], spec(test_size=test_size))
    assert error.value.code == "INSUFFICIENT_SAMPLES"
    assert fits == []


@pytest.mark.parametrize("rows,test_size", [(13, .2), (20, .1)])
def test_exact_minimum_partition_sizes(table, rows, test_size):
    result = train_regression(table.iloc[:rows], spec(test_size=test_size))
    assert len(result.splits.test) >= 2
    assert len(result.splits.folds) == 5
    assert min(len(f.validation) for f in result.splits.folds) >= 2
    assert result.manifest["spec"]["test_size"] == test_size
    if rows == 13:
        assert all(len(f.validation) == 2 for f in result.splits.folds)
    else:
        assert len(result.splits.test) == 2


@pytest.mark.parametrize("partition", ["test", "validation"])
def test_constant_r2_partition_rejected_before_fit(table, partition, monkeypatch):
    splits = make_splits(table, spec())
    positions = splits.test if partition == "test" else splits.folds[0].validation
    table.loc[list(positions), "strength_MPa"] = 5
    fits = []
    monkeypatch.setattr(Pipeline, "fit", lambda *a, **k: fits.append(1))
    with pytest.raises(EngineError) as error:
        train_regression(table, spec())
    assert error.value.code == "CONSTANT_TARGET_PARTITION" and fits == []


def test_fold_local_all_missing_feature_rejected_before_fit(table, monkeypatch):
    fold = make_splits(table, spec()).folds[0]
    table.loc[list(fold.train), "z"] = np.nan
    fits = []
    monkeypatch.setattr(Pipeline, "fit", lambda *a, **k: fits.append(1))
    with pytest.raises(EngineError) as error:
        train_regression(table, spec())
    assert error.value.code == "EMPTY_TRAINING_FEATURE" and fits == []


@pytest.mark.parametrize("bad,code", [("missing_target", "INVALID_TARGET"),
    ("infinity", "NON_FINITE_VALUES"), ("text", "NON_NUMERIC_COLUMNS"),
    ("bool", "NON_NUMERIC_COLUMNS"), ("duplicate", "INVALID_COLUMNS"),
    ("empty", "EMPTY_TABLE"), ("all_missing", "EMPTY_TRAINING_FEATURE")])
def test_invalid_inputs_are_diagnosed(table, bad, code):
    if bad == "missing_target":
        table.loc[0, "strength_MPa"] = np.nan
    elif bad == "infinity":
        table.loc[0, "x"] = np.inf
    elif bad == "text":
        table["x"] = "category"
    elif bad == "bool":
        table["x"] = True
    elif bad == "duplicate":
        table.columns = ["x", "x", "strength_MPa"]
    elif bad == "empty":
        table = table.iloc[:0]
    else:
        table["z"] = np.nan
    with pytest.raises(EngineError) as error:
        train_regression(table, spec())
    assert error.value.code == code


def test_diagnostics_do_not_drop_unselected_strings_or_infer_units(table):
    table["batch"] = pd.Categorical(["batch-a"] * len(table))
    table = pd.concat([table, table.iloc[[0]]], ignore_index=True)
    analysis = analyze_table(table)
    assert analysis["non_numeric_columns"] == ["batch"]
    assert analysis["duplicate_row_count"] == 1 and analysis["units"] == {}
    result = train_regression(table, spec())
    assert result.report["input_diagnostics"]["non_numeric_columns"] == ["batch"]
    assert result.manifest["spec"]["units"] == {}
    assert predict(result, table[["x", "z"]]).unit is None
    assert "DUPLICATE_ROWS_PRESENT" in result.report["warnings"]


def test_explicit_optional_units_are_metadata_only(table):
    result = train_regression(table, spec(units={"strength_MPa": "custom-unit", "x": "custom-x"}))
    output = predict(result, table[["z", "x"]])
    assert output.unit == "custom-unit"
    np.testing.assert_allclose(output.values, table.strength_MPa, rtol=1e-10, atol=1e-12)


@pytest.mark.parametrize("bad", ["missing", "extra", "string"])
def test_prediction_schema_is_explicit(table, bad):
    model = train_regression(table, spec())
    frame = table[["x", "z"]].copy()
    if bad == "missing":
        frame = frame[["x"]]
    elif bad == "extra":
        frame["other"] = 1
    else:
        frame["x"] = "12"
    with pytest.raises(EngineError):
        predict(model, frame)


def test_csv_preserves_explicit_schema_and_rejects_ragged_or_duplicate_headers():
    frame = read_csv(b"\xef\xbb\xbfx,z,label\n1,2,3\n4,,6\n")
    assert list(frame.columns) == ["x", "z", "label"] and len(frame) == 2
    assert pd.isna(frame.loc[1, "z"])
    for data in (b"x,x,y\n1,2,3\n", b"x,y\n1\n", b"x,y\n1,2,3\n", b"", b"x,y\n\n"):
        with pytest.raises(EngineError, match="CSV"):
            read_csv(data)


@pytest.mark.parametrize("change", ["value", "row_order", "column_order", "dtype", "row_count", "unused_column"])
def test_full_table_fingerprint_binds_splits(table, change):
    table["note"] = "original"
    result = train_regression(table, spec())
    changed = table.copy()
    if change == "value":
        changed.loc[0, "x"] += 1
    elif change == "row_order":
        changed = changed.iloc[::-1]
    elif change == "column_order":
        changed = changed[list(reversed(changed.columns))]
    elif change == "dtype":
        changed["x"] = changed.x.astype("int64")
    elif change == "row_count":
        changed = changed.iloc[:-1]
    else:
        changed.loc[0, "note"] = "changed"
    with pytest.raises(EngineError) as error:
        evaluate_test(result, changed)
    assert error.value.code == "DATASET_MISMATCH"


def test_split_indices_use_original_positions_not_dataframe_labels(table):
    original_identity = table_identity(table)
    table.index = ["duplicate-label"] * len(table)
    assert table_identity(table) == original_identity
    result = train_regression(table, spec())
    split = result.splits
    validate_split_binding(table, split)
    assert set(split.train).isdisjoint(split.test)
    assert set(split.train) | set(split.test) == set(range(len(table)))
    validation = [i for f in split.folds for i in f.validation]
    assert sorted(validation) == sorted(split.train)
    for fold in split.folds:
        assert set(fold.train).isdisjoint(fold.validation)
        assert set(fold.train) | set(fold.validation) == set(split.train)
    assert evaluate_test(result, table) == result.report["test"]


@pytest.mark.parametrize("change", ["out_of_range", "bool", "overlap", "heldout_in_cv", "repeated_fold", "short_fold"])
def test_invalid_historical_indices_are_rejected(table, change):
    splits = make_splits(table, spec())
    with pytest.raises(EngineError):
        if change == "out_of_range":
            replace(splits, train=(*splits.train[:-1], len(table)))
        elif change == "bool":
            replace(splits, test=(True, *splits.test[1:]))
        elif change == "overlap":
            replace(splits, test=(*splits.test, splits.train[0]))
        elif change == "heldout_in_cv":
            bad = CVFold((*splits.folds[0].train, splits.test[0]), splits.folds[0].validation)
            replace(splits, folds=(bad, *splits.folds[1:]))
        elif change == "repeated_fold":
            replace(splits, folds=(splits.folds[1], *splits.folds[1:]))
        else:
            first = splits.folds[0]
            bad = CVFold((*first.train, *first.validation[1:]), first.validation[:1])
            replace(splits, folds=(bad, *splits.folds[1:]))


def test_split_json_roundtrip(table):
    splits = make_splits(table, spec())
    assert SplitManifest.from_dict(splits.to_dict()) == splits


@pytest.mark.parametrize("kwargs", [{"algorithm": "XGB"}, {"test_size": 0},
    {"random_state": True}, {"units": {"unknown": "MPa"}}])
def test_fixed_engine_contract_rejects_unsupported_spec(kwargs):
    with pytest.raises(EngineError):
        spec(**kwargs)


def test_category_type_metadata_participates_in_full_table_identity(table):
    first = table.assign(batch=pd.Categorical(["A"] * len(table), categories=["A", "B"]))
    second = first.copy()
    second["batch"] = second["batch"].cat.set_categories(["A", "B", "C"])
    assert table_identity(first) != table_identity(second)
    third = first.copy()
    third["batch"] = third["batch"].cat.as_ordered()
    assert table_identity(first) != table_identity(third)
