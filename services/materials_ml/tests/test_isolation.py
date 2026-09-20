import json
from pathlib import Path
import subprocess
import sys
import tomllib


def test_package_dependencies_and_interpreter_are_independent():
    project = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = {s.split("==")[0] for s in metadata["project"]["dependencies"]}
    assert dependencies == {"numpy", "pandas", "scipy", "scikit-learn", "joblib", "threadpoolctl"}
    assert sys.version_info[:2] == (3, 11)
    assert sys.prefix != sys.base_prefix or "materialsagent-ml" in sys.prefix
    if sys.prefix != sys.base_prefix:
        config = (Path(sys.prefix) / "pyvenv.cfg").read_text(encoding="utf-8")
        assert "include-system-site-packages = false" in config


def test_training_and_package_roundtrip_never_import_platform_or_network_services():
    program = '''
import builtins, json, tempfile
from pathlib import Path
forbidden = {"flask", "fastapi", "mcp", "materialsagent", "sqlalchemy", "psycopg", "minio", "index", "torch", "tensorflow"}
attempts = []
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split(".")[0] in forbidden:
        attempts.append(name)
        raise ImportError("Forbidden dependency")
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import numpy as np
import pandas as pd
from materials_ml import TrainingSpec, train_regression, predict, save_package, load_package
x = np.arange(30, dtype=float)
data = pd.DataFrame({"x": x, "y": x * 2 + 1})
with tempfile.TemporaryDirectory() as folder:
    model = train_regression(data, TrainingSpec(("x",), "y"))
    save_package(model, Path(folder) / "model")
    restored = load_package(Path(folder) / "model", trusted=True)
    assert len(predict(restored, data[["x"]]).values) == 30
print(json.dumps(attempts))
'''
    result = subprocess.run([sys.executable, "-I", "-c", program], capture_output=True,
                            text=True, timeout=60, check=True)
    assert json.loads(result.stdout) == []
