from pathlib import Path
import sys


MOCK_RUNTIME_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MOCK_RUNTIME_SRC))
