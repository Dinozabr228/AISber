import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import AuditEntry

_LOGS_DIR = Path(__file__).resolve().parents[1] / "logs"
_AUDIT_LOG = _LOGS_DIR / "audit.log"
_SECURITY_LOG = _LOGS_DIR / "security.log"


def _ensure_logs_dir() -> None:
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)


def write_audit(entry: AuditEntry) -> None:
    _ensure_logs_dir()
    with _AUDIT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry.model_dump(), ensure_ascii=False) + "\n")


def write_security_event(event_type: str, details: str, severity: str) -> None:
    _ensure_logs_dir()
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "details": details,
        "severity": severity,
    }
    with _SECURITY_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
