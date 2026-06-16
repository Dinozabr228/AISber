"""
state/conversation_context.py

Session-scoped conversation memory for follow-up request resolution.

Architecture guarantees:
  • Gemini remains stateless — context is re-injected as a structured
    prefix on every request, never stored in the LLM.
  • Memory is isolated per (user_id, conversation_id).
  • All data expires automatically (TTL-based).
  • Thread-safe: all public methods hold the internal lock.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

CONTEXT_TTL: float = 1_800.0        # 30 min inactivity → reset memory
CONTEXT_HARD_TTL: float = 3_600.0   # 60 min → remove entry entirely


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


# ---------------------------------------------------------------------------
# ConversationContext — single conversation's memory
# ---------------------------------------------------------------------------

@dataclass
class ConversationContext:
    """Per-conversation memory. Never passed to Gemini directly."""
    conversation_id: str
    session_id: str
    user_id: str

    # Intent/filter state — updated after every successful action
    last_intent: str = ""
    last_filters: dict[str, Any] = field(default_factory=dict)
    last_result: dict[str, Any] | None = None

    # Timestamps
    created_at: str = field(default_factory=_now_iso)
    expires_at: str = field(default_factory=lambda: _expires_iso(CONTEXT_HARD_TTL))
    last_active: float = field(default_factory=time.monotonic)

    def is_inactive(self) -> bool:
        """True when inactivity TTL exceeded — memory should be reset."""
        return time.monotonic() - self.last_active > CONTEXT_TTL

    def is_hard_expired(self) -> bool:
        """True when hard TTL exceeded — entry should be removed."""
        return time.monotonic() - self.last_active > CONTEXT_HARD_TTL

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def reset_memory(self) -> None:
        """Clear conversational memory on inactivity TTL. Metadata preserved."""
        self.last_intent = ""
        self.last_filters = {}
        self.last_result = None

    def build_filter_hint(self) -> str:
        """Return a compact string describing active filters for the context hint."""
        if not self.last_filters:
            return ""
        parts = []
        if self.last_filters.get("filter"):
            parts.append(f"filter={self.last_filters['filter']}")
        if self.last_filters.get("period"):
            parts.append(f"period={self.last_filters['period']}")
        if self.last_filters.get("date_from"):
            parts.append(f"date_from={self.last_filters['date_from']}")
        if self.last_filters.get("date_to"):
            parts.append(f"date_to={self.last_filters['date_to']}")
        if self.last_filters.get("report_subtype"):
            parts.append(f"subtype={self.last_filters['report_subtype']}")
        if self.last_filters.get("section"):
            parts.append(f"section={self.last_filters['section']}")
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# ConversationContextStore — thread-safe in-memory store
# ---------------------------------------------------------------------------

class ConversationContextStore:
    """Thread-safe store for ConversationContext objects."""

    def __init__(self) -> None:
        self._data: dict[str, ConversationContext] = {}
        self._lock = threading.Lock()

    # ── Public API ───────────────────────────────────────────────────────────

    def get_or_create(
        self,
        conversation_id: str,
        user_id: str,
        session_id: str = "",
    ) -> ConversationContext:
        """Return existing context or create a new one.

        Raises PermissionError if conversation_id belongs to a different user.
        Resets memory (not entry) on inactivity TTL.
        """
        with self._lock:
            ctx = self._data.get(conversation_id)
            if ctx is None:
                ctx = ConversationContext(
                    conversation_id=conversation_id,
                    session_id=session_id,
                    user_id=user_id,
                )
                self._data[conversation_id] = ctx
                return ctx

            if ctx.user_id != user_id:
                raise PermissionError(
                    "conversation_id не принадлежит данному пользователю."
                )
            if ctx.is_inactive():
                ctx.reset_memory()
            ctx.touch()
            return ctx

    def update(
        self,
        conversation_id: str,
        *,
        intent: str = "",
        filters: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Update intent, filters, and/or result for a conversation.

        Filters are MERGED: existing keys are preserved unless overridden.
        Pass an empty dict {} to explicitly clear all filters.
        """
        with self._lock:
            ctx = self._data.get(conversation_id)
            if ctx is None:
                return
            if intent:
                ctx.last_intent = intent
            if filters is not None:
                if filters == {}:
                    ctx.last_filters = {}
                else:
                    merged = dict(ctx.last_filters)
                    merged.update({k: v for k, v in filters.items() if v is not None})
                    ctx.last_filters = merged
            if result is not None:
                # Store only a summary to avoid large memory consumption
                ctx.last_result = _summarise_result(result)
            ctx.touch()

    def reset(self, conversation_id: str) -> None:
        """Reset memory for a new chat (clear last_intent/filters/result)."""
        with self._lock:
            ctx = self._data.get(conversation_id)
            if ctx:
                ctx.reset_memory()

    def get(self, conversation_id: str) -> ConversationContext | None:
        with self._lock:
            return self._data.get(conversation_id)

    def delete(self, conversation_id: str) -> None:
        with self._lock:
            self._data.pop(conversation_id, None)

    def cleanup_expired(self) -> int:
        """Remove hard-expired entries. Returns count removed."""
        with self._lock:
            to_remove = [
                cid for cid, ctx in self._data.items()
                if ctx.is_hard_expired()
            ]
            for cid in to_remove:
                del self._data[cid]
        return len(to_remove)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _summarise_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep only a lightweight summary of action_result in memory."""
    if not isinstance(result, dict):
        return {}
    summary: dict[str, Any] = {}
    # Transactions: remember count and type breakdown
    if "transactions" in result:
        txs = result["transactions"]
        summary["tx_count"] = len(txs)
        summary["tx_types"] = list({t.get("type") for t in txs if t.get("type")})
    # Report: remember period and totals
    if "income" in result:
        summary["income"] = result.get("income")
        summary["expenses"] = result.get("expenses")
        summary["period"] = result.get("period")
    return summary


# Module-level singleton used by main.py
context_store = ConversationContextStore()
