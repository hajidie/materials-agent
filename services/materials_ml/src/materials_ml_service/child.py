"""Child startup gate deliberately precedes Engine imports and all training work."""
import json
from pathlib import Path
import sys


def main():
    if sys.stdin.buffer.readline() != b"GO\n":
        return 2
    folder = Path(sys.argv[1])
    from materials_ml import TrainingSpec, train_regression, read_csv, table_identity, save_package, EngineError
    try:
        request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
        table = read_csv((folder / "input.csv").read_bytes())
        if table_identity(table).to_dict() != request["dataset_identity"]:
            raise EngineError("DATASET_MISMATCH", "Dataset changed")
        package = train_regression(table, TrainingSpec(**request["engine_spec"]))
        save_package(package, folder / "model")
        result = {"ok": True}
    except EngineError as error:
        result = {"ok": False, "code": error.code}
    except Exception:
        result = {"ok": False, "code": "TRAINING_FAILED"}
    (folder / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
