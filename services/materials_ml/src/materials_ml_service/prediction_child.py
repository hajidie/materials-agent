"""Fixed, credential-free prediction entrypoint. No service/HTTP/storage imports."""
import json
from pathlib import Path
import sys


def main():
    if sys.stdin.buffer.readline() != b"GO\n":
        return 2
    from materials_ml import load_package, predict, read_csv, table_identity
    folder = Path(sys.argv[1])
    try:
        request = json.loads((folder / "request.json").read_bytes())
        table = read_csv((folder / "input.csv").read_bytes())
        if table_identity(table).to_dict() != request["input_identity"]:
            return 1
        package = load_package(folder / "model", trusted=True)
        result = predict(package, table)
        output = {"format": "predictions-v1", "model_id": request["model_id"],
                  "model_manifest_sha256": request["model_manifest_sha256"],
                  "input_dataset_id": request["input_dataset_id"], "input_identity": request["input_identity"],
                  "row_positions": list(result.row_positions), "values": list(result.values),
                  "target": result.target, "target_unit": result.unit}
        (folder / "predictions.json").write_text(json.dumps(output, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
