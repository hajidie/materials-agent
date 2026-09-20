from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from .errors import EngineError

ENGINE_VERSION = "0.1.0"
PACKAGE_VERSION = "materials-ml-package-v1"
FINGERPRINT_VERSION = "table-json-v1"
CV_FOLDS = 5


@dataclass(frozen=True)
class TrainingSpec:
    features: tuple[str, ...]
    target: str
    algorithm: str = "LR"
    test_size: float = 0.2
    random_state: int = 42
    units: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.features, (tuple, list)):
            raise EngineError("INVALID_SPEC", "Features must be an explicit column list.")
        features = tuple(self.features)
        if (not features or any(type(c) is not str or not c.strip() for c in features)
                or len(set(features)) != len(features)):
            raise EngineError("INVALID_SPEC", "Feature names must be nonempty and unique.")
        if type(self.target) is not str or not self.target.strip() or self.target in features:
            raise EngineError("INVALID_SPEC", "One separate target column is required.")
        if self.algorithm not in ("LR", "RF"):
            raise EngineError("UNSUPPORTED_ALGORITHM", "Only LR and RF are supported.")
        if type(self.test_size) not in (int, float) or not 0 < self.test_size < 1:
            raise EngineError("INVALID_SPEC", "Test fraction must lie strictly between zero and one.")
        if type(self.random_state) is not int or not 0 <= self.random_state < 2**32:
            raise EngineError("INVALID_SPEC", "Random state must be an unsigned 32-bit integer.")
        if not isinstance(self.units, Mapping) or any(
            key not in (*features, self.target) or type(value) is not str or not value.strip()
            for key, value in self.units.items()
        ):
            raise EngineError("INVALID_SPEC", "Units must be explicit metadata for selected columns.")
        object.__setattr__(self, "features", features)
        object.__setattr__(self, "units", MappingProxyType(dict(self.units)))

    def to_dict(self) -> dict:
        return {"features": list(self.features), "target": self.target,
                "algorithm": self.algorithm, "test_size": self.test_size,
                "random_state": self.random_state, "units": dict(self.units)}


@dataclass(frozen=True)
class DatasetIdentity:
    sha256: str
    row_count: int
    fingerprint_version: str = FINGERPRINT_VERSION

    def __post_init__(self) -> None:
        if (type(self.sha256) is not str or len(self.sha256) != 64
                or any(c not in "0123456789abcdef" for c in self.sha256)
                or type(self.row_count) is not int or self.row_count < 1
                or self.fingerprint_version != FINGERPRINT_VERSION):
            raise EngineError("INVALID_DATA_IDENTITY", "Invalid table identity.")

    def to_dict(self) -> dict:
        return {"sha256": self.sha256, "row_count": self.row_count,
                "fingerprint_version": self.fingerprint_version}


@dataclass(frozen=True)
class CVFold:
    train: tuple[int, ...]
    validation: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "train", tuple(self.train))
        object.__setattr__(self, "validation", tuple(self.validation))

    def to_dict(self) -> dict:
        return {"train": list(self.train), "validation": list(self.validation)}


@dataclass(frozen=True)
class SplitManifest:
    dataset: DatasetIdentity
    train: tuple[int, ...]
    test: tuple[int, ...]
    folds: tuple[CVFold, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "train", tuple(self.train))
        object.__setattr__(self, "test", tuple(self.test))
        object.__setattr__(self, "folds", tuple(self.folds))
        self.validate()

    def validate(self) -> None:
        def indices(values: tuple[int, ...]) -> set[int]:
            if (any(type(v) is not int or not 0 <= v < self.dataset.row_count for v in values)
                    or len(set(values)) != len(values)):
                raise EngineError("INVALID_SPLIT", "Indices must be unique original row positions.")
            return set(values)

        train, test = indices(self.train), indices(self.test)
        if train & test or train | test != set(range(self.dataset.row_count)):
            raise EngineError("INVALID_SPLIT", "Train/test must partition the complete input.")
        if len(train) < CV_FOLDS * 2 or len(test) < 2:
            raise EngineError("INSUFFICIENT_SAMPLES", "Test and every 5-fold validation set require at least two rows.")
        if len(self.folds) != CV_FOLDS:
            raise EngineError("INVALID_SPLIT", "Exactly five CV folds are required.")
        validation_rows: list[int] = []
        for fold in self.folds:
            fold_train, validation = indices(fold.train), indices(fold.validation)
            if fold_train & validation or fold_train | validation != train:
                raise EngineError("INVALID_SPLIT", "Every CV fold must partition only the external train set.")
            if len(validation) < 2:
                raise EngineError("INSUFFICIENT_SAMPLES", "Every R-squared validation fold requires at least two rows.")
            validation_rows.extend(fold.validation)
        if len(validation_rows) != len(train) or set(validation_rows) != train:
            raise EngineError("INVALID_SPLIT", "CV validation folds must cover train exactly once.")

    def to_dict(self) -> dict:
        return {"dataset": self.dataset.to_dict(), "train": list(self.train),
                "test": list(self.test), "folds": [f.to_dict() for f in self.folds]}

    @classmethod
    def from_dict(cls, value: dict) -> SplitManifest:
        try:
            if set(value) != {"dataset", "train", "test", "folds"}:
                raise ValueError()
            return cls(DatasetIdentity(**value["dataset"]), tuple(value["train"]),
                       tuple(value["test"]), tuple(CVFold(**f) for f in value["folds"]))
        except (TypeError, KeyError, ValueError) as exc:
            if isinstance(exc, EngineError):
                raise
            raise EngineError("INVALID_SPLIT", "Malformed split manifest.") from None


@dataclass(frozen=True)
class PredictionResult:
    values: tuple[float, ...]
    row_positions: tuple[int, ...]
    target: str
    unit: str | None
