"""
state/session_transactions.py

In-session transaction store for immediate visibility after transfer execution.

Transactions are stored here right after executor runs.
When get_transactions is called, these are merged with mock history.

Architecture guarantees:
  • Isolated by user_id.
  • Thread-safe.
  • TTL-based expiry (60 min).
  • Deduplication by operation_id.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SESSION_TX_TTL: float = 3_600.0  # 60 min


@dataclass
class SessionTransaction:
    operation_id: str
    user_id: str
    conversation_id: str
    recipient_name: str
    amount: float
    currency: str
    transaction_type: str   # "expense" | "income"
    execution_status: str   # "accepted"
    created_at: str         # ISO 8601

    _added_at: float = field(default_factory=time.monotonic, repr=False, compare=False)

    def to_history_entry(self) -> dict[str, Any]:
        """Convert to the same shape as mock transaction_history entries."""
        date_str = self.created_at[:10]  # "2026-06-14"
        return {
            "date": date_str,
            "amount": round(self.amount, 2),
            "type": self.transaction_type,
            "recipient": self.recipient_name if self.transaction_type == "expense" else "",
            "sender":   self.recipient_name if self.transaction_type == "income"  else "",
            "_operation_id": self.operation_id,  # dedup sentinel
        }


class SessionTransactionStore:
    """Thread-safe store for in-session transactions."""

    def __init__(self) -> None:
        self._data: dict[str, SessionTransaction] = {}   # operation_id -> tx
        self._user_index: dict[str, set[str]] = {}       # user_id -> set[operation_id]
        self._lock = threading.Lock()

    def add(self, tx: SessionTransaction) -> None:
        """Add or overwrite a transaction (idempotent on operation_id)."""
        with self._lock:
            self._data[tx.operation_id] = tx
            self._user_index.setdefault(tx.user_id, set()).add(tx.operation_id)

    def get_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """Return history-entry dicts for all non-expired session txs of a user."""
        cutoff = time.monotonic() - SESSION_TX_TTL
        with self._lock:
            oids = self._user_index.get(user_id, set())
            return [
                self._data[oid].to_history_entry()
                for oid in oids
                if oid in self._data and self._data[oid]._added_at >= cutoff
            ]

    def cleanup_expired(self) -> int:
        """Remove expired entries. Returns count removed."""
        cutoff = time.monotonic() - SESSION_TX_TTL
        with self._lock:
            expired = [oid for oid, tx in self._data.items() if tx._added_at < cutoff]
            for oid in expired:
                tx = self._data.pop(oid)
                self._user_index.get(tx.user_id, set()).discard(oid)
        return len(expired)


# Module-level singleton used by main.py and executor
session_tx_store = SessionTransactionStore()
