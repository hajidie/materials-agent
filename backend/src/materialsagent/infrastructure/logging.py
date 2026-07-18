from datetime import datetime, timezone
import json
import logging
import sys
from typing import TextIO


REQUEST_LOGGER_NAME = "materialsagent.request"
REQUEST_FIELDS = (
    "request_id",
    "method",
    "path",
    "status_code",
    "duration_ms",
)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": utc_timestamp(),
            "level": record.levelname,
            "event": getattr(record, "event", record.getMessage()),
        }
        for field_name in REQUEST_FIELDS:
            value = getattr(record, field_name, None)
            if value is not None:
                payload[field_name] = value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(
    level: str,
    stream: TextIO | None = None,
) -> logging.Logger:
    logger = logging.getLogger(REQUEST_LOGGER_NAME)
    logger.handlers.clear()

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger
