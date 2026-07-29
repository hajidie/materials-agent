from typing import Dict, FrozenSet, Tuple


RUNTIME_CONTRACT_VERSION = "1.0"
TOOL_ID = "zta35g_sem_virtual_lab"
TOOL_VERSION = "0.1.0"
SCHEMA_VERSION = "1.0"
MODEL_BUNDLE_ID = "zta35g-sem-original-bundle"

HOST = "127.0.0.1"
DEFAULT_PORT = 8100
TOKEN_HEADER = "X-ZTA35G-Runtime-Token"

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ID_CHARS = 256
MAX_SAFE_MESSAGE_CHARS = 256
MAX_SAFE_DETAILS = 10
MAX_SAFE_DETAIL_KEY_CHARS = 64
MAX_WARNINGS = 20
MAX_DIAGNOSTICS = 10

IMG_SIZE = 512
NUM_SAMPLES = 1
GUIDE_SCALE = 2.0
TIMESTEPS = 1000

SUPPORTED_OUTPUTS = frozenset(
    ("sem_image", "mechanical_properties")
)  # type: FrozenSet[str]
PROCESS_PARAMETER_ORDER = (
    "solution_temperature",
    "solution_time",
    "aging_temperature",
    "aging_time",
)  # type: Tuple[str, ...]
PROCESS_PARAMETER_RANGES = {
    "solution_temperature": (900.0, 1100.0, True),
    "solution_time": (1.0, 5.0, False),
    "aging_temperature": (670.0, 790.0, True),
    "aging_time": (1.0, 5.0, False),
}  # type: Dict[str, Tuple[float, float, bool]]

RUNTIME_ERROR_CODES = frozenset(
    (
        "INVALID_RUNTIME_REQUEST",
        "UNSUPPORTED_TOOL",
        "SCHEMA_VERSION_MISMATCH",
        "TOOL_VERSION_MISMATCH",
        "RUNTIME_NOT_READY",
        "RUNTIME_BUSY",
        "MODEL_LOAD_FAILED",
        "SEM_GENERATION_FAILED",
        "MECHANICAL_PROPERTY_PREDICTION_FAILED",
        "INVALID_MODEL_OUTPUT",
        "INTERNAL_RUNTIME_ERROR",
    )
)  # type: FrozenSet[str]

DIAGNOSTIC_STEPS = frozenset(
    (
        "model_loading",
        "sem_generation",
        "mechanical_property_prediction",
    )
)  # type: FrozenSet[str]
