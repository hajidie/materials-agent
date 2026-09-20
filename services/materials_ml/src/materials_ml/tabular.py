from __future__ import annotations

import csv
import hashlib
import io
import json
import math

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_complex_dtype, is_numeric_dtype

from .contracts import DatasetIdentity, FINGERPRINT_VERSION, SplitManifest
from .errors import EngineError


def check_table(table: pd.DataFrame) -> None:
    if not isinstance(table, pd.DataFrame) or table.empty:
        raise EngineError("EMPTY_TABLE", "A nonempty DataFrame is required.")
    if (not table.columns.is_unique
            or any(type(c) is not str or not c.strip() for c in table.columns)):
        raise EngineError("INVALID_COLUMNS", "Column names must be nonempty unique strings.")


def read_csv(payload: bytes) -> pd.DataFrame:
    """Parse UTF-8 comma-separated bytes; reject headers pandas would silently rename."""
    try:
        if not isinstance(payload, bytes):
            raise ValueError()
        text = payload.decode("utf-8-sig")
        rows = csv.reader(io.StringIO(text), strict=True)
        header = next(rows)
        if (not header or any(not c.strip() for c in header)
                or len(set(header)) != len(header)
                or any(len(row) != len(header) for row in rows)):
            raise ValueError()
        table = pd.read_csv(io.StringIO(text), skip_blank_lines=False)
        check_table(table)
        return table
    except (UnicodeError, ValueError, TypeError, StopIteration, csv.Error, pd.errors.ParserError):
        raise EngineError("INVALID_CSV", "CSV requires a valid UTF-8 header and rectangular rows.") from None


def _cell(value: object) -> object:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if type(value) is float:
        if math.isnan(value):
            return None
        if not math.isfinite(value):
            raise EngineError("NON_FINITE_VALUES", "Infinite values are not supported.")
    if type(value) not in (str, int, float, bool):
        raise EngineError("UNSUPPORTED_CELL_TYPE", "Only scalar numbers, text, booleans and missing values are supported.")
    return value


def table_identity(table: pd.DataFrame) -> DatasetIdentity:
    """Hash full content/schema/order, deliberately excluding DataFrame index labels."""
    check_table(table)
    digest = hashlib.sha256()

    def update(value: object) -> None:
        digest.update(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")

    def dtype_identity(dtype: object) -> object:
        if isinstance(dtype, pd.CategoricalDtype):
            return {"name": "category", "ordered": dtype.ordered,
                    "categories_dtype": str(dtype.categories.dtype),
                    "categories": [_cell(v) for v in dtype.categories]}
        return str(dtype)

    update({"version": FINGERPRINT_VERSION, "columns": list(table.columns),
            "dtypes": [dtype_identity(d) for d in table.dtypes], "row_count": len(table)})
    for row in table.itertuples(index=False, name=None):
        update([_cell(value) for value in row])
    return DatasetIdentity(digest.hexdigest(), len(table))


def numeric_column(column: pd.Series) -> bool:
    return (is_numeric_dtype(column.dtype) and not is_bool_dtype(column.dtype)
            and not is_complex_dtype(column.dtype))


def numeric_frame(table: pd.DataFrame, columns: tuple[str, ...]) -> pd.DataFrame:
    if not set(columns) <= set(table.columns):
        raise EngineError("MISSING_COLUMNS", "Required columns are absent.")
    if any(not numeric_column(table[c]) for c in columns):
        raise EngineError("NON_NUMERIC_COLUMNS", "Selected columns must be numeric; no implicit encoding is performed.")
    try:
        result = table.loc[:, list(columns)].astype("float64")
    except (TypeError, ValueError, OverflowError):
        raise EngineError("INVALID_NUMERIC_VALUES", "Values cannot be represented as float64.") from None
    if np.isinf(result.to_numpy()).any():
        raise EngineError("NON_FINITE_VALUES", "Infinite values are not supported.")
    return result


def analyze_table(table: pd.DataFrame) -> dict:
    check_table(table)
    identity = table_identity(table)
    columns = []
    for name in table.columns:
        column = table[name]
        numeric = numeric_column(column)
        columns.append({"name": name, "dtype": str(column.dtype), "numeric": numeric,
                        "missing_count": int(column.isna().sum()),
                        "constant": column.nunique(dropna=True) <= 1})
    return {"dataset": identity.to_dict(), "columns": columns,
            "non_numeric_columns": [c["name"] for c in columns if not c["numeric"]],
            "duplicate_row_count": int(table.duplicated().sum()),
            "assumptions": ["IID_NOT_VERIFIED"], "units": {}}


def validate_split_binding(table: pd.DataFrame, splits: SplitManifest) -> None:
    splits.validate()
    if table_identity(table) != splits.dataset:
        raise EngineError("DATASET_MISMATCH", "Historical row positions belong to different input content or row count.")
