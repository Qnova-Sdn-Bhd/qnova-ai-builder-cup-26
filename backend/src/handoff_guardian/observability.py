"""Safe JSON application events for Cloud Logging; SDK loggers remain untouched."""

import json
import logging
import re
import sys

LOGGER_NAME = "handoff_guardian"
FIELDS = frozenset(
    {
        "request_id",
        "status",
        "http_status",
        "latency_ms",
        "code",
        "exception_type",
        "job_id",
        "project_id",
        "target_id",
        "kind",
        "outcome",
        "attempt_count",
        "dispatch_generation",
        "fencing_token",
        "enqueued",
        "expired_jobs",
        "failed_jobs",
    }
)
SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


def configure_logging():
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(getattr(handler, "_handoff_guardian_json", False) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._handoff_guardian_json = True
        logger.addHandler(handler)
    return logger


def emit(event: str, **fields):
    """Accept only bounded scalar metadata; never bodies, headers, messages or exceptions."""
    record = {"severity": "INFO", "event": event if SAFE_LABEL.fullmatch(event) else "invalid_event"}
    for key, value in fields.items():
        if key not in FIELDS:
            continue
        if isinstance(value, int) and not isinstance(value, bool):
            record[key] = value
        elif isinstance(value, str) and (SAFE_CODE if key == "code" else SAFE_LABEL).fullmatch(value):
            record[key] = value
    configure_logging().info(json.dumps(record, sort_keys=True, separators=(",", ":")))
