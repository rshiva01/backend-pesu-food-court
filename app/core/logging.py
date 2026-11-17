import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

# Never log secrets: OTPs, tokens, passwords, cookies. Redact by key name and
# by obvious value patterns before anything is written.
_SENSITIVE_KEYS = re.compile(r"otp|code|token|password|secret|cookie|authorization|totp", re.I)
_SENSITIVE_VALUES = re.compile(r"(?i)(bearer\s+)[\w.\-]+|eyJ[\w-]+\.[\w-]+\.[\w-]+")


def _redact(value: Any, key: str = "") -> Any:
    if key and _SENSITIVE_KEYS.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return _SENSITIVE_VALUES.sub("[redacted]", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "at": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": _redact(record.getMessage()),
        }
        extra = getattr(record, "ctx", None)
        if isinstance(extra, dict):
            payload["ctx"] = _redact(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # uvicorn's access log prints full query strings; keep it but route via JSON
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
