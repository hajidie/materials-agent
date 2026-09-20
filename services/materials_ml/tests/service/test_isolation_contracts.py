import subprocess
import sys
import os


def test_worker_import_has_only_http_and_process_dependencies():
    program = """
import sys
import materials_ml_service.worker
forbidden = {'sqlalchemy', 'psycopg', 'minio', 'materialsagent', 'materials_ml', 'fastapi'}
assert not forbidden.intersection(name.split('.')[0] for name in sys.modules)
"""
    subprocess.run([sys.executable, "-I", "-c", program], check=True, capture_output=True)


def test_worker_cli_rejects_inherited_service_credentials():
    env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "PATH"}}
    env.update(ML_DATABASE_URL="must-not-be-used", ML_WORKER_TOKEN="x" * 32)
    result = subprocess.run([sys.executable, "-I", "-m", "materials_ml_service.worker", "--once"],
                            env=env, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert b"ML_WORKER_STOPPED" in result.stderr and b"must-not-be-used" not in result.stderr


def test_prediction_child_import_does_not_load_service_infrastructure():
    program = """
import sys
import materials_ml_service.prediction_child
assert not {'sqlalchemy', 'psycopg', 'minio', 'fastapi', 'mcp', 'materials_ml', 'materialsagent'}.intersection(
    name.split('.')[0] for name in sys.modules)
"""
    subprocess.run([sys.executable, "-I", "-c", program], check=True, capture_output=True)


def test_built_wheel_contains_incremental_migrations(tmp_path):
    from pathlib import Path
    import zipfile
    project = Path(__file__).resolve().parents[2]
    subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(tmp_path), str(project)],
                   check=True, capture_output=True, timeout=60)
    with zipfile.ZipFile(next(tmp_path.glob('*.whl'))) as wheel:
        for migration in ('0001_ml_resources.py', '0002_predictions.py'):
            assert 'materials_ml_service/migrations/versions/' + migration in wheel.namelist()
