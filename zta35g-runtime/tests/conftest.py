from pathlib import Path
import sys


RUNTIME_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(RUNTIME_SRC))
