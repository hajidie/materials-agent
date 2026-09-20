"""Independent LR/RF engine. No service, Agent or persistence lifecycle is implemented."""

from .contracts import ENGINE_VERSION as __version__
from .contracts import DatasetIdentity, PredictionResult, SplitManifest, TrainingSpec
from .errors import EngineError
from .model_package import load_package, save_package
from .regression import ModelPackage, evaluate_test, predict, train_regression, validate_training_input
from .tabular import analyze_table, read_csv, table_identity, validate_split_binding

__all__ = ["__version__", "DatasetIdentity", "PredictionResult", "SplitManifest", "TrainingSpec",
           "EngineError", "ModelPackage", "analyze_table", "read_csv", "table_identity",
           "validate_split_binding", "train_regression", "predict", "evaluate_test",
           "save_package", "load_package", "validate_training_input"]
