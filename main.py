"""
SberBusiness AI Agent — main application entry point.

Zero-Trust pipeline order for /chat (enforced, no step may be skipped):
  1. Prompt firewall       — block injections before touching any data
  2. Privacy filter        — sanitize message before Gemini ever sees it
  3. Gemini call           — stateless intent extraction (clean text only)
  4. Response validation   — strict schema check on everything Gemini returns
  5. Action whitelist      — reject anything not in the approved action set
  6. Risk scoring          — server-side, never trusted from client
  7. Audit log             — written before execution; immutable append-only
  8. Execution gate        — confirmation required per risk level
  9. Action executor       — mock data only, no external calls
"""
import asyncio
import collections
import json
import mimetypes
import os
import pathlib
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from agent.gemini import call_gemini
from state.conversation_context import context_store
from state.session_transactions import SessionTransaction, session_tx_store
from data.mock import get_counterparty, get_user
from executor.actions import ActionExecutor
from models import (
    AuditEntry, ChatRequest, ChatResponse,
    ConfirmRequest, ConfirmResponse,
    DraftItem, FeedbackRequest,
    NotificationItem, RecipientDetailsRequest, RecipientDetailsResponse,
    RenameConversationRequest,
    SessionResponse,
    _USER_ID_RE as _MODELS_USER_ID_RE,
)
from privacy.filters import filter_user_request
from security.audit import write_audit, write_security_event
from security.prompt_firewall import check_request
from security.risk_scoring import calculate_risk
from security.whitelist import validate_action

load_dotenv()

_executor = ActionExecutor()
_API_KEY = os.getenv("API_KEY", "").strip()

# ---------------------------------------------------------------------------
# Confirmation token store.
# Each entry: {user_id, action, parameters, expires_at, risk_level, draft_id}.
# TTL = 15 minutes; tokens are single-use and consumed atomically.
# ---------------------------------------------------------------------------
_CONFIRMATION_TTL = 900.0
_pending_confirmations: dict[str, dict[str, Any]] = {}
_MAX_PENDING_PER_USER = 3


def _store_confirmation(
    token: str,
    user_id: str,
    action: str,
    parameters: dict,
    risk_level: str = "UNKNOWN",
    draft_id: str | None = None,
    conversation_id: str = "",
) -> None:
    _pending_confirmations[token] = {
        "user_id": user_id,
        "action": action,
        "parameters": parameters,
        "risk_level": risk_level,
        "draft_id": draft_id,
        "conversation_id": conversation_id,
        "expires_at": time.monotonic() + _CONFIRMATION_TTL,
    }


def _pop_confirmation(token: str) -> dict[str, Any] | None:
    entry = _pending_confirmations.pop(token, None)
    if entry is None:
        return None
    if time.monotonic() > entry["expires_at"]:
        return None
    return entry


def _find_duplicate_transfer(user_id: str, action: str, parameters: dict) -> str | None:
    """Return existing token if an identical pending transfer exists for this user."""
    if action != "initiate_transfer":
        return None
    req_amount = parameters.get("amount")
    req_recipient = (parameters.get("recipient") or "").strip().lower()
    now = time.monotonic()
    for token, entry in _pending_confirmations.items():
        if (
            entry["user_id"] == user_id
            and entry["action"] == action
            and entry["parameters"].get("amount") == req_amount
            and (entry["parameters"].get("recipient") or "").strip().lower() == req_recipient
            and now < entry["expires_at"]
        ):
            return token
    return None


def _count_pending_for_user(user_id: str) -> int:
    now = time.monotonic()
    return sum(
        1 for e in _pending_confirmations.values()
        if e["user_id"] == user_id and now < e["expires_at"]
    )


# ---------------------------------------------------------------------------
# Draft store — history of all draft operations
# ---------------------------------------------------------------------------
_DRAFT_TTL = 86_400.0         # 24h for confirmed drafts
_DRAFT_DETAILS_TTL = 1_800.0  # 30min for requires_recipient_details drafts
_drafts: dict[str, dict[str, Any]] = {}


def _create_draft(
    user_id: str,
    action: str,
    parameters: dict,
    risk_level: str,
    status: str = "pending",
    ttl: float | None = None,
    conversation_id: str = "",
) -> str:
    """Create a draft record. status may be 'pending' or 'requires_recipient_details'."""
    draft_id = str(uuid.uuid4())
    now_mono = time.monotonic()
    now_iso = _now_iso()
    effective_ttl = ttl if ttl is not None else (
        _DRAFT_DETAILS_TTL if status == "requires_recipient_details" else _DRAFT_TTL
    )
    expires_iso = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + effective_ttl,
        tz=timezone.utc,
    ).isoformat()
    _drafts[draft_id] = {
        "draft_id": draft_id,
        "user_id": user_id,
        "action": action,
        "parameters": parameters,
        "status": status,
        "created_at": now_iso,
        "expires_at": expires_iso,
        "expires_mono": now_mono + effective_ttl,
        "risk_level": risk_level,
        "conversation_id": conversation_id,
    }
    return draft_id


def _find_pending_recipient_details_draft(
    user_id: str, action: str, parameters: dict
) -> str | None:
    """Return existing draft_id if a non-expired requires_recipient_details draft exists."""
    if action != "initiate_transfer":
        return None
    req_amount = parameters.get("amount")
    req_recipient = (parameters.get("recipient") or "").strip().lower()
    now = time.monotonic()
    for did, draft in _drafts.items():
        if (
            draft["user_id"] == user_id
            and draft["action"] == action
            and draft["status"] == "requires_recipient_details"
            and draft["parameters"].get("amount") == req_amount
            and (draft["parameters"].get("recipient") or "").strip().lower() == req_recipient
            and now < draft.get("expires_mono", 0)
        ):
            return did
    return None


# ---------------------------------------------------------------------------
# Draft state machine — CRITICAL-01
# All valid (current_status, next_status) transitions. Any transition not in
# this set is either a logic bug or an attempted manipulation — both are logged.
# ---------------------------------------------------------------------------
_VALID_DRAFT_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    # New counterparty flow
    ("requires_recipient_details", "pending"),     # details provided + server-validated
    ("requires_recipient_details", "expired"),     # TTL expired before details submitted
    ("requires_recipient_details", "cancelled"),   # cancelled before details (future use)
    # Standard banking confirmation flow
    ("pending", "confirmed"),                       # user confirmed; executor succeeded
    ("pending", "cancelled"),                       # user explicitly cancelled
    ("pending", "expired"),                         # TTL expired while awaiting confirmation
})

# Per-draft threading locks — prevent race conditions on concurrent detail submissions.
# Key: draft_id  Value: threading.Lock
_draft_locks: dict[str, threading.Lock] = {}
_draft_locks_guard = threading.Lock()


def _get_draft_lock(draft_id: str) -> threading.Lock:
    """Return (creating if needed) the per-draft Lock for concurrent-access protection."""
    with _draft_locks_guard:
        if draft_id not in _draft_locks:
            _draft_locks[draft_id] = threading.Lock()
        return _draft_locks[draft_id]


def _assert_valid_transition(draft_id: str, current_status: str, target_status: str) -> None:
    """Raise HTTP 409 if the transition is not in _VALID_DRAFT_TRANSITIONS.
    Used at API endpoints where an invalid transition must be surfaced to the caller."""
    if (current_status, target_status) not in _VALID_DRAFT_TRANSITIONS:
        write_security_event(
            "INVALID_DRAFT_TRANSITION",
            f"draft_id={draft_id[:36]} {current_status} → {target_status}",
            "HIGH",
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Недопустимый переход состояния черновика: "
                f"{current_status} → {target_status}."
            ),
        )


def _update_draft_status(draft_id: str | None, status: str) -> None:
    """Update draft status after validating the transition.

    Logs a security event but does NOT raise on invalid transitions — callers such
    as the background cleanup must not propagate exceptions.  API endpoints that need
    strict enforcement call _assert_valid_transition() before this function.
    """
    if not draft_id or draft_id not in _drafts:
        return
    current = _drafts[draft_id].get("status", "unknown")
    if (current, status) not in _VALID_DRAFT_TRANSITIONS:
        write_security_event(
            "INVALID_DRAFT_TRANSITION",
            f"draft_id={draft_id[:36]} {current} → {status}",
            "HIGH",
        )
        return  # Do not apply an invalid transition — log is the audit evidence
    _drafts[draft_id]["status"] = status


# ---------------------------------------------------------------------------
# Notification store
# ---------------------------------------------------------------------------
_notifications: dict[str, list[dict[str, Any]]] = {}


def _add_notification(user_id: str, text: str, level: str = "info") -> None:
    _notifications.setdefault(user_id, []).append({
        "id": str(uuid.uuid4()),
        "text": text,
        "level": level,
        "created_at": _now_iso(),
        "read": False,
    })


# ---------------------------------------------------------------------------
# Conversation context store — structured context, NO raw financial data
# ---------------------------------------------------------------------------
_CONTEXT_TTL = 1_800.0  # 30 minutes inactivity
_MAX_CONTEXT_DEPTH = 10
_conversation_contexts: dict[str, dict[str, Any]] = {}


def _get_or_create_context(
    conversation_id: str,
    user_id: str,
    session_id: str = "",
) -> dict[str, Any]:
    ctx_obj = context_store.get_or_create(conversation_id, user_id, session_id)

    entry = _conversation_contexts.get(conversation_id)
    if entry is None:
        now_iso = datetime.now(timezone.utc).isoformat()
        entry = {
            "conversation_id": conversation_id,
            "session_id": session_id,
            "user_id": user_id,
            "history": [],
            "last_action": None,
            "last_intent": "",
            "last_filters": {},
            "last_result": None,
            "last_parameters": {},
            "last_active": time.monotonic(),
            "title": None,
            "title_locked": False,
            "created_at": now_iso,
            "updated_at": now_iso,
            "expires_at": ctx_obj.expires_at,
            "messages": [],
        }
        _conversation_contexts[conversation_id] = entry
        return entry
    if entry["user_id"] != user_id:
        raise PermissionError("conversation_id не принадлежит данному пользователю.")
    if time.monotonic() - entry["last_active"] > _CONTEXT_TTL:
        entry["history"] = []
        entry["last_action"] = None
        entry["last_intent"] = ""
        entry["last_filters"] = {}
        entry["last_result"] = None
        entry["last_parameters"] = {}
    entry["last_active"] = time.monotonic()
    return entry


def _safe_context_params(action: str, parameters: dict) -> dict[str, str]:
    """Return only non-sensitive parameters safe to store in conversation history.

    Intentionally excludes: amounts, account numbers, recipient names, PII.
    Only metadata useful for follow-up intent resolution is kept.
    """
    safe: dict[str, str] = {}
    if action == "get_transactions":
        if parameters.get("filter"):
            safe["filter"] = str(parameters["filter"])[:50]
        if parameters.get("period"):
            safe["period"] = str(parameters["period"])[:50]
        if parameters.get("date_from"):
            safe["date_from"] = str(parameters["date_from"])[:10]
        if parameters.get("date_to"):
            safe["date_to"] = str(parameters["date_to"])[:10]
    elif action == "navigate":
        if parameters.get("section"):
            safe["section"] = str(parameters["section"])[:50]
    elif action == "create_report":
        if parameters.get("report_type"):
            safe["report_type"] = str(parameters["report_type"])[:50]
        if parameters.get("period"):
            safe["period"] = str(parameters["period"])[:50]
        if parameters.get("date_from"):
            safe["date_from"] = str(parameters["date_from"])[:10]
        if parameters.get("date_to"):
            safe["date_to"] = str(parameters["date_to"])[:10]
    # initiate_transfer: amount and recipient are sensitive — not stored
    # get_tariffs, get_requisites, get_counterparties, get_favorites: no params
    return safe


_TOPIC_STRIP_RE = re.compile(r'[\[\]{}]')

# ---------------------------------------------------------------------------
# Conversation history helpers
# ---------------------------------------------------------------------------

_ACTION_TITLES: dict[str, str] = {
    "get_transactions":   "Последние операции",
    "initiate_transfer":  "Перевод средств",
    "navigate":           "Навигация по банку",
    "get_tariffs":        "Тарифы",
    "get_requisites":     "Реквизиты компании",
    "get_counterparties": "Контрагенты",
    "get_favorites":      "Избранные получатели",
}


def _generate_title(first_message: str, action: str | None, intent: str, parameters: dict | None = None) -> str:
    if action == "create_report":
        if parameters and parameters.get("report_subtype") == "analysis":
            return "Анализ расходов"
        return "Финансовый отчёт"
    if action and action in _ACTION_TITLES:
        return _ACTION_TITLES[action]
    cleaned = first_message.strip()
    return (cleaned[:47] + "...") if len(cleaned) > 50 else (cleaned or "Диалог")


def _store_message(conversation_id: str, role: str, text: str) -> None:
    entry = _conversation_contexts.get(conversation_id)
    if entry is None:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    entry["messages"].append({"role": role, "text": text[:2000], "timestamp": now_iso})
    entry["updated_at"] = now_iso


def _set_conversation_title(conversation_id: str, title: str, locked: bool = False) -> None:
    entry = _conversation_contexts.get(conversation_id)
    if entry is None or entry.get("title_locked"):
        return
    entry["title"] = title[:50]
    if locked:
        entry["title_locked"] = True


def _update_context(
    conversation_id: str,
    action: str | None,
    parameters: dict,
    intent: str = "",
    assistant_topic: str = "",
    result: dict[str, Any] | None = None,
) -> None:
    entry = _conversation_contexts.get(conversation_id)
    if entry is None:
        return
    entry["last_action"] = action
    entry["last_parameters"] = parameters
    entry["last_active"] = time.monotonic()

    if intent:
        entry["last_intent"] = intent
    if action == "get_transactions":
        new_filters: dict[str, Any] = {}
        if parameters.get("filter"):
            new_filters["filter"] = parameters["filter"]
        if parameters.get("period"):
            new_filters["period"] = parameters["period"]
        if parameters.get("date_from"):
            new_filters["date_from"] = parameters["date_from"]
        if parameters.get("date_to"):
            new_filters["date_to"] = parameters["date_to"]
        if new_filters:
            merged = dict(entry.get("last_filters") or {})
            merged.update(new_filters)
            entry["last_filters"] = merged
    elif action in ("create_report",):
        new_filters = {}
        if parameters.get("report_subtype"):
            new_filters["report_subtype"] = parameters["report_subtype"]
        if parameters.get("period"):
            new_filters["period"] = parameters["period"]
        if parameters.get("date_from"):
            new_filters["date_from"] = parameters["date_from"]
        if parameters.get("date_to"):
            new_filters["date_to"] = parameters["date_to"]
        if new_filters:
            entry["last_filters"] = new_filters
    elif action not in ("get_transactions", "create_report"):
        # Different action type — preserve filters only for same-action follow-ups
        pass
    if result is not None:
        entry["last_result"] = result

    # Sync extended fields to ConversationContextStore
    safe_filters = _safe_context_params(action, parameters) if action else {}
    context_store.update(
        conversation_id,
        intent=intent,
        filters=safe_filters if safe_filters else None,
        result=result,
    )

    if action:
        turn: dict[str, str] = {"action": action}
        if intent:
            turn["intent"] = intent[:100]
        turn.update(_safe_context_params(action, parameters))
        entry["history"].append(turn)
    elif assistant_topic:
        clean = _TOPIC_STRIP_RE.sub('', assistant_topic).strip()[:100]
        if clean:
            entry["history"].append({"topic": clean})
    entry["history"] = entry["history"][-_MAX_CONTEXT_DEPTH:]


# Filter fields that carry over across follow-up turns per action type
_FOLLOW_UP_FIELDS: dict[str, list[str]] = {
    "get_transactions": ["filter", "period", "date_from", "date_to"],
    "create_report":    ["report_type", "period", "report_subtype", "date_from", "date_to"],
}

# Parameter values that mean "not specified" — don't override stored context
_EMPTY_VALUES: frozenset[str] = frozenset({"", "all", "none", "any"})


def _apply_follow_up_context(
    conversation_id: str,
    action: str | None,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Merge stored context filters into parameters for follow-up requests.

    Stored filters fill gaps only — Gemini's explicit non-empty values win.
    Applied only when the action matches last_intent (same action type follow-up).
    """
    if action not in _FOLLOW_UP_FIELDS:
        return parameters

    ctx_obj = context_store.get(conversation_id)
    if not ctx_obj or not ctx_obj.last_filters:
        return parameters

    # Only merge when it's a continuation of the same action
    if ctx_obj.last_intent and ctx_obj.last_intent != action:
        return parameters

    relevant_fields = _FOLLOW_UP_FIELDS[action]
    merged = dict(parameters)
    for field_name in relevant_fields:
        stored_val = ctx_obj.last_filters.get(field_name)
        current_val = parameters.get(field_name, "")
        if stored_val and str(current_val).lower() in _EMPTY_VALUES:
            merged[field_name] = stored_val
    return merged


def _build_context_hint(ctx: dict[str, Any]) -> str:
    """Build a multi-turn context hint from conversation history.

    Banking turns:  "get_transactions(filter=incoming, period=last_month) → ..."
    Assistant turns: "[Кредит — это форма финансовых отношений...] → ..."
    Passed as a prefix to the Gemini prompt so it can resolve follow-up questions.
    Gemini remains stateless — context is re-injected every request.
    """
    history = ctx.get("history", [])
    recent = history[-3:]
    turn_strs: list[str] = []
    for turn in recent:
        if "action" in turn:
            action = turn["action"]
            extras = [f"{k}={turn[k]}" for k in ("filter", "period", "date_from", "date_to", "section", "report_type", "report_subtype") if k in turn]
            turn_strs.append(f"{action}({', '.join(extras)})" if extras else action)
        elif "topic" in turn:
            turn_strs.append(f"[{turn['topic']}]")

    # append active filters so Gemini can resolve follow-up questions
    ctx_obj = context_store.get(ctx.get("conversation_id", ""))
    if ctx_obj:
        filter_hint = ctx_obj.build_filter_hint()
        if filter_hint and turn_strs:
            turn_strs[-1] = f"{turn_strs[-1]} [active: {filter_hint}]"

    if not turn_strs:
        return ""
    return " → ".join(turn_strs)


# ---------------------------------------------------------------------------
# Session store — session_token → user_id
# ---------------------------------------------------------------------------
_SESSION_TTL = 3_600.0  # 1 hour
_sessions: dict[str, dict[str, Any]] = {}
_DEMO_USER_IDS = ["user_001", "user_002", "user_003"]


def _create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "user_id": user_id,
        "expires_at": time.monotonic() + _SESSION_TTL,
    }
    return token


def _resolve_session(token: str) -> str | None:
    entry = _sessions.get(token)
    if entry is None:
        return None
    if time.monotonic() > entry["expires_at"]:
        _sessions.pop(token, None)
        return None
    entry["expires_at"] = time.monotonic() + _SESSION_TTL  # rolling
    return entry["user_id"]


# ---------------------------------------------------------------------------
# Metrics counters
# ---------------------------------------------------------------------------
_metrics: dict[str, Any] = {
    "requests_total": 0,
    "requests_blocked_firewall": 0,
    "requests_confirmed": 0,
    "requests_cancelled": 0,
    "gemini_fallback_activations": 0,
    "response_times_ms": [],
    "active_pending_confirmations": 0,
}


def _record_response_time(ms: float) -> None:
    _metrics["response_times_ms"].append(ms)
    if len(_metrics["response_times_ms"]) > 500:
        _metrics["response_times_ms"] = _metrics["response_times_ms"][-500:]


# ---------------------------------------------------------------------------
# Rate limiting — sliding window, 60 requests per 60 seconds per IP
# ---------------------------------------------------------------------------
_RATE_LIMIT_WINDOW = 60.0
_RATE_LIMIT_MAX    = 60
_rate_limit_store: dict[str, collections.deque] = {}


async def _check_rate_limit(request: Request) -> None:
    client_ip: str = (request.client.host if request.client else "unknown")
    now = time.monotonic()
    if client_ip not in _rate_limit_store:
        _rate_limit_store[client_ip] = collections.deque()
    timestamps = _rate_limit_store[client_ip]
    while timestamps and now - timestamps[0] > _RATE_LIMIT_WINDOW:
        timestamps.popleft()
    if len(timestamps) >= _RATE_LIMIT_MAX:
        try:
            write_security_event(
                event_type="RATE_LIMIT_EXCEEDED",
                details=f"ip={client_ip} count={len(timestamps)} window={_RATE_LIMIT_WINDOW}s",
                severity="MEDIUM",
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=429,
            detail="Слишком много запросов. Пожалуйста, подождите.",
        )
    timestamps.append(now)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="SberBusiness AI Agent",
    description="Интеллектуальный банковский ассистент для бизнеса",
    version="2.0.0",
)

_CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost http://127.0.0.1").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key"],
)

app.mount("/icon", StaticFiles(directory="icon"), name="icon")
app.mount("/fonts", StaticFiles(directory="fonts"), name="fonts")

_SBBOL_DIR = pathlib.Path("C:/сбербизнес")
_SBBOL_ASSETS = _SBBOL_DIR / "СберБизнес — интернет-банк_files"


@app.get("/sbbol-assets/{path:path}")
async def sbbol_asset(path: str) -> FileResponse:
    file_path = _SBBOL_ASSETS / path
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    name = file_path.name
    if name.endswith(".js") or ".js." in name:
        media_type = "application/javascript; charset=utf-8"
    elif name.endswith(".css"):
        media_type = "text/css; charset=utf-8"
    else:
        media_type, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(str(file_path), media_type=media_type)


_DEMO_HTML_CACHE: str | None = None


_DEMO_INTERACTIVE_JS = r"""
<script>
document.addEventListener('DOMContentLoaded', function() {

  var S = '.mainContentWrapper';
  var dashboardHTML = document.querySelector(S) ? document.querySelector(S).innerHTML : '';

  var pages = {
    moneyAndEvents: null,
    payments: '<div style="padding:32px"><h2 style="margin:0 0 24px;font-size:22px;font-weight:600;color:#1F2937">Расчёты</h2>'
      + '<div style="display:flex;gap:12px;margin-bottom:24px">'
      + '<button onclick="this.style.borderBottom=\'2px solid #107F8C\';this.style.color=\'#107F8C\'" style="padding:8px 16px;background:none;border:none;border-bottom:2px solid transparent;font-size:14px;cursor:pointer;color:#6B7280">Все</button>'
      + '<button onclick="this.style.borderBottom=\'2px solid #107F8C\'" style="padding:8px 16px;background:none;border:none;border-bottom:2px solid transparent;font-size:14px;cursor:pointer;color:#6B7280">Входящие</button>'
      + '<button onclick="this.style.borderBottom=\'2px solid #107F8C\'" style="padding:8px 16px;background:none;border:none;border-bottom:2px solid transparent;font-size:14px;cursor:pointer;color:#6B7280">Исходящие</button></div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:14px"><thead><tr style="border-bottom:2px solid #E5E7EB;text-align:left;color:#6B7280">'
      + '<th style="padding:12px">Дата</th><th style="padding:12px">Получатель</th><th style="padding:12px">Назначение</th><th style="padding:12px;text-align:right">Сумма</th><th style="padding:12px">Статус</th></tr></thead>'
      + '<tbody>'
      + '<tr style="border-bottom:1px solid #F3F4F6;cursor:pointer" onmouseover="this.style.background=\'#F9FAFB\'" onmouseout="this.style.background=\'\'"><td style="padding:12px">10.06.2026</td><td>ООО «ПримаТех»</td><td>Оплата по договору №127</td><td style="text-align:right;color:#DC2626">-12 500.00 BYN</td><td><span style="background:#DEF7EC;color:#03543F;padding:2px 8px;border-radius:10px;font-size:12px">Исполнен</span></td></tr>'
      + '<tr style="border-bottom:1px solid #F3F4F6;cursor:pointer" onmouseover="this.style.background=\'#F9FAFB\'" onmouseout="this.style.background=\'\'"><td style="padding:12px">09.06.2026</td><td>ИП Сидоров А.В.</td><td>Возврат аванса</td><td style="text-align:right;color:#059669">+3 200.00 BYN</td><td><span style="background:#DEF7EC;color:#03543F;padding:2px 8px;border-radius:10px;font-size:12px">Исполнен</span></td></tr>'
      + '<tr style="border-bottom:1px solid #F3F4F6;cursor:pointer" onmouseover="this.style.background=\'#F9FAFB\'" onmouseout="this.style.background=\'\'"><td style="padding:12px">08.06.2026</td><td>ЕРИП</td><td>Коммунальные услуги</td><td style="text-align:right;color:#DC2626">-847.30 BYN</td><td><span style="background:#DEF7EC;color:#03543F;padding:2px 8px;border-radius:10px;font-size:12px">Исполнен</span></td></tr>'
      + '<tr style="border-bottom:1px solid #F3F4F6;cursor:pointer" onmouseover="this.style.background=\'#F9FAFB\'" onmouseout="this.style.background=\'\'"><td style="padding:12px">07.06.2026</td><td>ООО «Агрокомплекс Нива»</td><td>Оплата поставки зерна</td><td style="text-align:right;color:#DC2626">-45 000.00 BYN</td><td><span style="background:#FEF3C7;color:#92400E;padding:2px 8px;border-radius:10px;font-size:12px">В обработке</span></td></tr>'
      + '</tbody></table>'
      + '<div style="margin-top:24px;text-align:center"><button style="padding:10px 32px;background:#107F8C;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:14px" onmouseover="this.style.background=\'#0E6B76\'" onmouseout="this.style.background=\'#107F8C\'">Создать платёжное поручение</button></div></div>',

    statement: '<div style="padding:32px"><h2 style="margin:0 0 24px;font-size:22px;font-weight:600;color:#1F2937">Выписка</h2>'
      + '<div style="display:flex;gap:16px;margin-bottom:24px;align-items:center">'
      + '<div style="flex:1"><label style="font-size:12px;color:#6B7280;display:block;margin-bottom:4px">Счёт</label><select style="width:100%;padding:8px 12px;border:1px solid #D1D5DB;border-radius:6px;font-size:14px"><option>BY51 BPSB 3012 2222 2222 2933 2222 (BYN)</option><option>BY69 BPSB 3012 3333 3333 3933 3333 (BYN)</option></select></div>'
      + '<div><label style="font-size:12px;color:#6B7280;display:block;margin-bottom:4px">С</label><input type="date" value="2026-06-01" style="padding:8px 12px;border:1px solid #D1D5DB;border-radius:6px;font-size:14px"></div>'
      + '<div><label style="font-size:12px;color:#6B7280;display:block;margin-bottom:4px">По</label><input type="date" value="2026-06-10" style="padding:8px 12px;border:1px solid #D1D5DB;border-radius:6px;font-size:14px"></div>'
      + '<button style="margin-top:18px;padding:10px 24px;background:#107F8C;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:14px">Сформировать</button></div>'
      + '<div style="background:#F9FAFB;border-radius:12px;padding:24px;text-align:center;color:#6B7280"><p style="font-size:48px;margin:0">📄</p><p style="margin:8px 0 0">Выберите параметры и нажмите «Сформировать»</p></div></div>',

    salary: '<div style="padding:32px"><h2 style="margin:0 0 24px;font-size:22px;font-weight:600;color:#1F2937">Зарплатный проект</h2>'
      + '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:24px">'
      + '<div style="background:#F0FDFA;border-radius:12px;padding:20px"><div style="font-size:12px;color:#6B7280">Сотрудников</div><div style="font-size:28px;font-weight:600;color:#107F8C;margin-top:4px">24</div></div>'
      + '<div style="background:#FEF3C7;border-radius:12px;padding:20px"><div style="font-size:12px;color:#6B7280">К выплате</div><div style="font-size:28px;font-weight:600;color:#92400E;margin-top:4px">67 840 BYN</div></div>'
      + '<div style="background:#F0FDFA;border-radius:12px;padding:20px"><div style="font-size:12px;color:#6B7280">Следующая выплата</div><div style="font-size:28px;font-weight:600;color:#107F8C;margin-top:4px">15.06</div></div></div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:14px"><thead><tr style="border-bottom:2px solid #E5E7EB;text-align:left;color:#6B7280"><th style="padding:12px">ФИО</th><th style="padding:12px">Должность</th><th style="padding:12px;text-align:right">Оклад</th><th style="padding:12px">Карта</th></tr></thead>'
      + '<tbody>'
      + '<tr style="border-bottom:1px solid #F3F4F6;cursor:pointer" onmouseover="this.style.background=\'#F9FAFB\'" onmouseout="this.style.background=\'\'"><td style="padding:12px">Иванов И.С.</td><td>Руководитель проекта</td><td style="text-align:right">4 200.00 BYN</td><td>**** 4521</td></tr>'
      + '<tr style="border-bottom:1px solid #F3F4F6;cursor:pointer" onmouseover="this.style.background=\'#F9FAFB\'" onmouseout="this.style.background=\'\'"><td style="padding:12px">Петрова А.М.</td><td>Бухгалтер</td><td style="text-align:right">3 100.00 BYN</td><td>**** 7833</td></tr>'
      + '<tr style="border-bottom:1px solid #F3F4F6;cursor:pointer" onmouseover="this.style.background=\'#F9FAFB\'" onmouseout="this.style.background=\'\'"><td style="padding:12px">Козлов Д.А.</td><td>Инженер</td><td style="text-align:right">2 850.00 BYN</td><td>**** 1209</td></tr>'
      + '</tbody></table></div>',

    productsAndServices: '<div style="padding:32px"><h2 style="margin:0 0 24px;font-size:22px;font-weight:600;color:#1F2937">Продукты и услуги</h2>'
      + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">'
      + '<div style="border:1px solid #E5E7EB;border-radius:12px;padding:24px;cursor:pointer;transition:box-shadow .2s" onmouseover="this.style.boxShadow=\'0 4px 12px rgba(0,0,0,0.08)\'" onmouseout="this.style.boxShadow=\'\'"><div style="font-size:18px;font-weight:600;margin-bottom:8px">Кредиты для бизнеса</div><div style="color:#6B7280;font-size:14px;margin-bottom:12px">от 8.5% годовых, до 500 000 BYN</div><button style="padding:8px 20px;background:#107F8C;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:13px">Подробнее</button></div>'
      + '<div style="border:1px solid #E5E7EB;border-radius:12px;padding:24px;cursor:pointer;transition:box-shadow .2s" onmouseover="this.style.boxShadow=\'0 4px 12px rgba(0,0,0,0.08)\'" onmouseout="this.style.boxShadow=\'\'"><div style="font-size:18px;font-weight:600;margin-bottom:8px">Депозиты</div><div style="color:#6B7280;font-size:14px;margin-bottom:12px">до 12.5% годовых, от 1 000 BYN</div><button style="padding:8px 20px;background:#107F8C;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:13px">Открыть</button></div>'
      + '<div style="border:1px solid #E5E7EB;border-radius:12px;padding:24px;cursor:pointer;transition:box-shadow .2s" onmouseover="this.style.boxShadow=\'0 4px 12px rgba(0,0,0,0.08)\'" onmouseout="this.style.boxShadow=\'\'"><div style="font-size:18px;font-weight:600;margin-bottom:8px">Корпоративные карты</div><div style="color:#6B7280;font-size:14px;margin-bottom:12px">Visa Business, обслуживание от 5 BYN/мес</div><button style="padding:8px 20px;background:#107F8C;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:13px">Заказать</button></div>'
      + '<div style="border:1px solid #E5E7EB;border-radius:12px;padding:24px;cursor:pointer;transition:box-shadow .2s" onmouseover="this.style.boxShadow=\'0 4px 12px rgba(0,0,0,0.08)\'" onmouseout="this.style.boxShadow=\'\'"><div style="font-size:18px;font-weight:600;margin-bottom:8px">Эквайринг</div><div style="color:#6B7280;font-size:14px;margin-bottom:12px">Приём платежей картами, от 1.5%</div><button style="padding:8px 20px;background:#107F8C;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:13px">Подключить</button></div>'
      + '</div></div>',

    'partner-services': '<div style="padding:32px"><h2 style="margin:0 0 24px;font-size:22px;font-weight:600;color:#1F2937">Сервисы</h2>'
      + '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px">'
      + '<div style="border:1px solid #E5E7EB;border-radius:12px;padding:20px;text-align:center;cursor:pointer" onmouseover="this.style.background=\'#F9FAFB\'" onmouseout="this.style.background=\'\'"><div style="font-size:32px;margin-bottom:8px">📊</div><div style="font-weight:600">Проверка контрагента</div><div style="color:#6B7280;font-size:13px;margin-top:4px">Быстрая проверка по УНП</div></div>'
      + '<div style="border:1px solid #E5E7EB;border-radius:12px;padding:20px;text-align:center;cursor:pointer" onmouseover="this.style.background=\'#F9FAFB\'" onmouseout="this.style.background=\'\'"><div style="font-size:32px;margin-bottom:8px">📝</div><div style="font-weight:600">Электронный документооборот</div><div style="color:#6B7280;font-size:13px;margin-top:4px">Обмен документами с контрагентами</div></div>'
      + '<div style="border:1px solid #E5E7EB;border-radius:12px;padding:20px;text-align:center;cursor:pointer" onmouseover="this.style.background=\'#F9FAFB\'" onmouseout="this.style.background=\'\'"><div style="font-size:32px;margin-bottom:8px">🔐</div><div style="font-weight:600">ЭЦП</div><div style="color:#6B7280;font-size:13px;margin-top:4px">Электронная цифровая подпись</div></div>'
      + '</div></div>',

    other: '<div style="padding:32px"><h2 style="margin:0 0 24px;font-size:22px;font-weight:600;color:#1F2937">Прочее</h2>'
      + '<div style="display:flex;flex-direction:column;gap:12px">'
      + '<div style="border:1px solid #E5E7EB;border-radius:8px;padding:16px;cursor:pointer;display:flex;justify-content:space-between;align-items:center" onmouseover="this.style.background=\'#F9FAFB\'" onmouseout="this.style.background=\'\'"><span>Письма в банк</span><span style="color:#6B7280">3 непрочитанных</span></div>'
      + '<div style="border:1px solid #E5E7EB;border-radius:8px;padding:16px;cursor:pointer;display:flex;justify-content:space-between;align-items:center" onmouseover="this.style.background=\'#F9FAFB\'" onmouseout="this.style.background=\'\'"><span>Справки и документы</span><span style="color:#6B7280">Заказать</span></div>'
      + '<div style="border:1px solid #E5E7EB;border-radius:8px;padding:16px;cursor:pointer;display:flex;justify-content:space-between;align-items:center" onmouseover="this.style.background=\'#F9FAFB\'" onmouseout="this.style.background=\'\'"><span>Валютный контроль</span><span style="color:#6B7280">2 документа</span></div>'
      + '<div style="border:1px solid #E5E7EB;border-radius:8px;padding:16px;cursor:pointer;display:flex;justify-content:space-between;align-items:center" onmouseover="this.style.background=\'#F9FAFB\'" onmouseout="this.style.background=\'\'"><span>Шаблоны документов</span><span style="color:#6B7280">12 шаблонов</span></div>'
      + '</div></div>',

    'user-account': '<div style="padding:32px"><h2 style="margin:0 0 24px;font-size:22px;font-weight:600;color:#1F2937">Настройки</h2>'
      + '<div style="max-width:600px">'
      + '<div style="border-bottom:1px solid #F3F4F6;padding:16px 0;display:flex;justify-content:space-between"><span style="color:#6B7280">Организация</span><span style="font-weight:500">DEMO ЮРИДИЧЕСКОЕ ЛИЦО</span></div>'
      + '<div style="border-bottom:1px solid #F3F4F6;padding:16px 0;display:flex;justify-content:space-between"><span style="color:#6B7280">УНП</span><span style="font-weight:500">192******</span></div>'
      + '<div style="border-bottom:1px solid #F3F4F6;padding:16px 0;display:flex;justify-content:space-between"><span style="color:#6B7280">Тариф</span><span style="font-weight:500;color:#107F8C">Бизнес</span></div>'
      + '<div style="border-bottom:1px solid #F3F4F6;padding:16px 0;display:flex;justify-content:space-between"><span style="color:#6B7280">Уведомления</span><span style="font-weight:500">SMS + Email</span></div>'
      + '<div style="border-bottom:1px solid #F3F4F6;padding:16px 0;display:flex;justify-content:space-between"><span style="color:#6B7280">Двухфакторная аутентификация</span><span style="font-weight:500;color:#059669">Включена</span></div>'
      + '<div style="margin-top:24px"><button style="padding:10px 24px;background:#107F8C;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:14px;margin-right:12px">Сохранить</button>'
      + '<button style="padding:10px 24px;background:none;border:1px solid #D1D5DB;border-radius:8px;cursor:pointer;font-size:14px;color:#6B7280">Отмена</button></div></div></div>'
  };

  /* ---- Sidebar menu ---- */
  var menuItems = document.querySelectorAll('li[class*="leftMenuItem-wrapper"]');
  var activeClass = 'leftMenuItem-active-GGgw';
  (function() {
    var m = document.querySelector('[class*="leftMenuItem-active"]');
    if (m) { var match = m.className.match(/leftMenuItem-active-\w+/); if (match) activeClass = match[0]; }
  })();

  menuItems.forEach(function(li) {
    li.style.cursor = 'pointer';
    li.addEventListener('click', function() {
      menuItems.forEach(function(el) {
        el.className = el.className.replace(/leftMenuItem-active-\w+/g, '').replace(/\s+/g, ' ').trim();
      });
      li.className = li.className + ' ' + activeClass;

      var nameEl = li.querySelector('[data-name]');
      var key = nameEl ? nameEl.getAttribute('data-name') : null;
      var content = document.querySelector(S);
      if (content && key) {
        if (pages[key] === null) { content.innerHTML = dashboardHTML; }
        else if (pages[key]) { content.innerHTML = pages[key]; }
        content.scrollTop = 0;
      }
    });
  });

  /* ---- Account rows ---- */
  document.querySelectorAll('div[class*="accountsTableItem-container"]').forEach(function(row) {
    row.style.cursor = 'pointer';
    row.addEventListener('click', function() {
      document.querySelectorAll('div[class*="accountsTableItem-container"]').forEach(function(r) { r.style.background = ''; });
      row.style.background = 'rgba(16,127,140,0.07)';
    });
  });

  /* ---- Toast helper ---- */
  function _toast(text) {
    var c = document.getElementById('_demoToasts');
    if (!c) {
      c = document.createElement('div'); c.id = '_demoToasts';
      c.style.cssText = 'position:fixed;top:20px;left:50%;transform:translateX(-50%);z-index:999999;display:flex;flex-direction:column;gap:8px;pointer-events:none;';
      document.body.appendChild(c);
    }
    var t = document.createElement('div');
    t.style.cssText = 'background:rgba(0,0,0,.82);color:#fff;padding:10px 20px;border-radius:24px;font-size:14px;white-space:nowrap;box-shadow:0 4px 16px rgba(0,0,0,.15);animation:_ti .3s ease both;';
    t.textContent = text; c.appendChild(t);
    setTimeout(function() { t.style.animation = '_to .3s ease both'; setTimeout(function() { t.remove(); }, 300); }, 2500);
  }
  var _ts = document.createElement('style');
  _ts.textContent = '@keyframes _ti{from{opacity:0;transform:translateY(-16px)}to{opacity:1;transform:none}}@keyframes _to{to{opacity:0;transform:translateY(-16px)}}';
  document.head.appendChild(_ts);

  /* ---- Modal helper ---- */
  function _modal(title, body) {
    var overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:200000;display:flex;align-items:center;justify-content:center;animation:_ti .2s ease both;';
    var box = document.createElement('div');
    box.style.cssText = 'background:#fff;border-radius:16px;padding:32px;max-width:520px;width:90%;box-shadow:0 20px 60px rgba(0,0,0,.15);position:relative;';
    box.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px"><h3 style="margin:0;font-size:20px;color:#1F2937">' + title + '</h3>'
      + '<button style="background:none;border:none;font-size:24px;cursor:pointer;color:#6B7280;padding:0 4px" onclick="this.closest(\'div[style*=fixed]\').remove()">&times;</button></div>'
      + '<div style="color:#4B5563;font-size:14px;line-height:1.6">' + body + '</div>'
      + '<div style="margin-top:24px;text-align:right"><button onclick="this.closest(\'div[style*=fixed]\').remove()" style="padding:10px 28px;background:#107F8C;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:14px">Закрыть</button></div>';
    overlay.appendChild(box);
    overlay.addEventListener('click', function(e) { if (e.target === overlay) overlay.remove(); });
    document.body.appendChild(overlay);
  }

  /* ---- Named button actions ---- */
  var btnActions = {
    'mainMenuToggle': function() {
      var sb = document.querySelector('[data-name="mainMenuContainer"]');
      if (sb) sb.style.display = sb.style.display === 'none' ? '' : 'none';
    },
    'mainHeaderMessagesButton': function() {
      _modal('Сообщения', '<div style="display:flex;flex-direction:column;gap:12px">'
        + '<div style="padding:12px;border:1px solid #E5E7EB;border-radius:8px;background:#F0FDFA"><strong>Банк:</strong> Ваша заявка на овердрафт одобрена. Лимит: 50 000 BYN.</div>'
        + '<div style="padding:12px;border:1px solid #E5E7EB;border-radius:8px"><strong>Система:</strong> Плановое обслуживание 15.06.2026, 02:00-04:00.</div>'
        + '<div style="padding:12px;border:1px solid #E5E7EB;border-radius:8px"><strong>Поддержка:</strong> Ваш запрос №4521 обработан.</div>'
        + '</div>');
    },
    'userProfile.currCustomer': function() {
      _modal('Профиль организации', '<div style="display:flex;flex-direction:column;gap:10px">'
        + '<div style="display:flex;justify-content:space-between"><span style="color:#6B7280">Организация:</span><strong>DEMO ЮРИДИЧЕСКОЕ ЛИЦО</strong></div>'
        + '<div style="display:flex;justify-content:space-between"><span style="color:#6B7280">УНП:</span><strong>192******</strong></div>'
        + '<div style="display:flex;justify-content:space-between"><span style="color:#6B7280">Пользователь:</span><strong>Администратор</strong></div>'
        + '<div style="display:flex;justify-content:space-between"><span style="color:#6B7280">Последний вход:</span><strong>10.06.2026, 19:45</strong></div>'
        + '<div style="margin-top:8px;padding-top:12px;border-top:1px solid #E5E7EB"><a href="#" onclick="event.preventDefault();this.closest(\'div[style*=fixed]\').remove();_toast(\'Выход из системы (демо)\')" style="color:#DC2626;text-decoration:none">Выйти из системы</a></div>'
        + '</div>');
    },
    'subHeader.onboardingButton': function() { _toast('Онбординг: добро пожаловать в СберБизнес!'); },
    'dashboard.createDoc': function() {
      _modal('Создать документ', '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">'
        + '<div style="border:1px solid #E5E7EB;border-radius:8px;padding:16px;cursor:pointer;text-align:center" onclick="_toast(\'Открываю форму платёжного поручения\');this.closest(\'div[style*=fixed]\').remove()"><div style="font-size:24px;margin-bottom:4px">📄</div>Платёжное поручение</div>'
        + '<div style="border:1px solid #E5E7EB;border-radius:8px;padding:16px;cursor:pointer;text-align:center" onclick="_toast(\'Открываю форму заявления\');this.closest(\'div[style*=fixed]\').remove()"><div style="font-size:24px;margin-bottom:4px">📋</div>Заявление</div>'
        + '<div style="border:1px solid #E5E7EB;border-radius:8px;padding:16px;cursor:pointer;text-align:center" onclick="_toast(\'Открываю письмо в банк\');this.closest(\'div[style*=fixed]\').remove()"><div style="font-size:24px;margin-bottom:4px">✉️</div>Письмо в банк</div>'
        + '<div style="border:1px solid #E5E7EB;border-radius:8px;padding:16px;cursor:pointer;text-align:center" onclick="_toast(\'Открываю валютный перевод\');this.closest(\'div[style*=fixed]\').remove()"><div style="font-size:24px;margin-bottom:4px">💱</div>Валютный перевод</div>'
        + '</div>');
    },
    'dashboard.accountsTable.refresh': function() {
      var b = document.querySelector('[name="dashboard.accountsTable.refresh"]');
      if (b) { b.style.animation = 'spin .6s linear'; setTimeout(function(){ b.style.animation = ''; }, 600); }
      _toast('Остатки обновлены');
    },
    'dashboard.accountsTable.settings': function() {
      _modal('Настройки счетов', '<div style="display:flex;flex-direction:column;gap:12px">'
        + '<label style="display:flex;align-items:center;gap:8px;cursor:pointer"><input type="checkbox" checked> Показывать скрытые счета</label>'
        + '<label style="display:flex;align-items:center;gap:8px;cursor:pointer"><input type="checkbox" checked> Группировать по валюте</label>'
        + '<label style="display:flex;align-items:center;gap:8px;cursor:pointer"><input type="checkbox"> Показывать закрытые счета</label>'
        + '</div>');
    },
    'acceptDictionary.button.showMore': function() { _toast('Загружаю ещё…'); },
    'dashboard.doc-to-sign': function() {
      _modal('Документы на подписании', '<div style="display:flex;flex-direction:column;gap:12px">'
        + '<div style="padding:12px;border:1px solid #E5E7EB;border-radius:8px;display:flex;justify-content:space-between;align-items:center"><div><strong>ПП №1042</strong><br><span style="color:#6B7280;font-size:13px">ООО «ПримаТех» — 12 500.00 BYN</span></div><button onclick="_toast(\'Документ подписан\');this.closest(\'div[style*=fixed]\').remove()" style="padding:6px 16px;background:#107F8C;color:#fff;border:none;border-radius:6px;cursor:pointer">Подписать</button></div>'
        + '<div style="padding:12px;border:1px solid #E5E7EB;border-radius:8px;display:flex;justify-content:space-between;align-items:center"><div><strong>ПП №1041</strong><br><span style="color:#6B7280;font-size:13px">ЕРИП — 847.30 BYN</span></div><button onclick="_toast(\'Документ подписан\');this.closest(\'div[style*=fixed]\').remove()" style="padding:6px 16px;background:#107F8C;color:#fff;border:none;border-radius:6px;cursor:pointer">Подписать</button></div>'
        + '</div>');
    },
    'dashboard.credits': function() {
      _modal('Кредиты', '<div style="text-align:center;padding:16px">'
        + '<div style="font-size:48px;margin-bottom:12px">💰</div>'
        + '<p style="font-size:16px;margin-bottom:8px">Доступный кредитный лимит</p>'
        + '<div style="font-size:32px;font-weight:700;color:#107F8C;margin-bottom:16px">150 000.00 BYN</div>'
        + '<p style="color:#6B7280">Ставка от 8.5% годовых. Без залога до 50 000 BYN.</p>'
        + '</div>');
    },
    'dashboard.corporateCards': function() {
      _modal('Корпоративные карты', '<div style="display:flex;flex-direction:column;gap:12px">'
        + '<div style="background:linear-gradient(135deg,#107F8C,#0E6B76);color:#fff;border-radius:12px;padding:20px"><div style="font-size:12px;opacity:.7">Visa Business</div><div style="font-size:20px;letter-spacing:2px;margin:12px 0">**** **** **** 4521</div><div style="display:flex;justify-content:space-between"><span>DEMO ЮР ЛИЦО</span><span>12/28</span></div></div>'
        + '<div style="background:linear-gradient(135deg,#6B7280,#4B5563);color:#fff;border-radius:12px;padding:20px"><div style="font-size:12px;opacity:.7">Mastercard Business</div><div style="font-size:20px;letter-spacing:2px;margin:12px 0">**** **** **** 7833</div><div style="display:flex;justify-content:space-between"><span>DEMO ЮР ЛИЦО</span><span>09/27</span></div></div>'
        + '</div>');
    },
    'dashboard.correspondent': function() {
      _modal('Корреспонденты', '<div style="display:flex;flex-direction:column;gap:10px">'
        + '<div style="padding:12px;border:1px solid #E5E7EB;border-radius:8px"><strong>ООО «ПримаТех»</strong><br><span style="color:#6B7280;font-size:13px">УНП 191234567 • 5 операций за месяц</span></div>'
        + '<div style="padding:12px;border:1px solid #E5E7EB;border-radius:8px"><strong>ИП Сидоров А.В.</strong><br><span style="color:#6B7280;font-size:13px">УНП 190987654 • 2 операции за месяц</span></div>'
        + '<div style="padding:12px;border:1px solid #E5E7EB;border-radius:8px"><strong>ООО «Агрокомплекс Нива»</strong><br><span style="color:#6B7280;font-size:13px">УНП 193456789 • 1 операция за месяц</span></div>'
        + '</div>');
    },
    'dashboard.employees': function() {
      _modal('Сотрудники', '<div style="display:flex;flex-direction:column;gap:10px">'
        + '<div style="padding:12px;border:1px solid #E5E7EB;border-radius:8px;display:flex;justify-content:space-between"><div><strong>Иванов И.С.</strong><br><span style="color:#6B7280;font-size:13px">Руководитель проекта</span></div><span style="color:#107F8C;font-weight:600">4 200 BYN</span></div>'
        + '<div style="padding:12px;border:1px solid #E5E7EB;border-radius:8px;display:flex;justify-content:space-between"><div><strong>Петрова А.М.</strong><br><span style="color:#6B7280;font-size:13px">Бухгалтер</span></div><span style="color:#107F8C;font-weight:600">3 100 BYN</span></div>'
        + '<div style="padding:12px;border:1px solid #E5E7EB;border-radius:8px;display:flex;justify-content:space-between"><div><strong>Козлов Д.А.</strong><br><span style="color:#6B7280;font-size:13px">Инженер</span></div><span style="color:#107F8C;font-weight:600">2 850 BYN</span></div>'
        + '</div>');
    },
    'dashboard.exchange-rates': function() {
      _modal('Курсы валют НБ РБ на 10.06.2026', '<table style="width:100%;border-collapse:collapse;font-size:14px">'
        + '<thead><tr style="border-bottom:2px solid #E5E7EB;color:#6B7280"><th style="padding:10px;text-align:left">Валюта</th><th style="padding:10px;text-align:right">Покупка</th><th style="padding:10px;text-align:right">Продажа</th><th style="padding:10px;text-align:right">НБ РБ</th></tr></thead>'
        + '<tbody>'
        + '<tr style="border-bottom:1px solid #F3F4F6"><td style="padding:10px">🇺🇸 USD</td><td style="text-align:right;padding:10px">3.2150</td><td style="text-align:right;padding:10px">3.2450</td><td style="text-align:right;padding:10px;color:#107F8C;font-weight:600">3.2305</td></tr>'
        + '<tr style="border-bottom:1px solid #F3F4F6"><td style="padding:10px">🇪🇺 EUR</td><td style="text-align:right;padding:10px">3.4980</td><td style="text-align:right;padding:10px">3.5320</td><td style="text-align:right;padding:10px;color:#107F8C;font-weight:600">3.5148</td></tr>'
        + '<tr style="border-bottom:1px solid #F3F4F6"><td style="padding:10px">🇷🇺 100 RUB</td><td style="text-align:right;padding:10px">3.5800</td><td style="text-align:right;padding:10px">3.6400</td><td style="text-align:right;padding:10px;color:#107F8C;font-weight:600">3.6102</td></tr>'
        + '</tbody></table>');
    }
  };

  /* ---- Buttons ---- */
  document.querySelectorAll('button').forEach(function(btn) {
    if (btn.closest('.launcher, .chat-widget, .sberik-panel, .stage, #widget, .overlay-root')) return;
    btn.style.cursor = 'pointer';
    btn.addEventListener('click', function(e) {
      var name = btn.getAttribute('data-name') || btn.getAttribute('name') || '';
      if (btnActions[name]) { btnActions[name](); return; }
      if (btn.className && btn.className.indexOf('triggerDropdown') !== -1) {
        var acct = btn.closest('div[class*="accountsTableItem-container"]');
        var num = acct ? (acct.querySelector('span') || {}).textContent || '' : '';
        _modal('Операции по счёту', '<div style="display:flex;flex-direction:column;gap:8px">'
          + '<button onclick="_toast(\'Открываю выписку\');this.closest(\'div[style*=fixed]\').remove()" style="display:block;width:100%;text-align:left;padding:12px;border:1px solid #E5E7EB;border-radius:8px;background:#fff;cursor:pointer;font-size:14px">📄 Выписка по счёту</button>'
          + '<button onclick="_toast(\'Открываю реквизиты\');this.closest(\'div[style*=fixed]\').remove()" style="display:block;width:100%;text-align:left;padding:12px;border:1px solid #E5E7EB;border-radius:8px;background:#fff;cursor:pointer;font-size:14px">📋 Реквизиты счёта</button>'
          + '<button onclick="_toast(\'Создаю платёж\');this.closest(\'div[style*=fixed]\').remove()" style="display:block;width:100%;text-align:left;padding:12px;border:1px solid #E5E7EB;border-radius:8px;background:#fff;cursor:pointer;font-size:14px">💸 Создать платёж</button>'
          + '<button onclick="_toast(\'Счёт скрыт\');this.closest(\'div[style*=fixed]\').remove()" style="display:block;width:100%;text-align:left;padding:12px;border:1px solid #E5E7EB;border-radius:8px;background:#fff;cursor:pointer;font-size:14px">👁 Скрыть счёт</button>'
          + '</div>');
        return;
      }
      var txt = (btn.textContent || '').trim();
      if (txt.indexOf('одробнее') !== -1) { _toast('Переход на страницу сервиса'); return; }
      if (txt.indexOf('оздать запрос') !== -1) { _toast('Открываю форму запроса'); return; }
      btn.style.transform = 'scale(0.95)';
      setTimeout(function() { btn.style.transform = ''; }, 150);
      if (txt && txt.length > 1 && txt.length < 40) { _toast(txt); }
    });
  });

  document.querySelectorAll('[data-testid="close-btn"], [name="modal.close"]').forEach(function(btn) {
    btn.style.cursor = 'pointer';
    btn.addEventListener('click', function() {
      var m = btn.closest('[class*="banner"], [class*="Banner"], [class*="modal"], [class*="Modal"], [class*="overlay"], [class*="Overlay"]');
      if (m) { m.style.transition = 'opacity .3s'; m.style.opacity = '0'; setTimeout(function() { m.style.display = 'none'; }, 300); }
    });
  });

  document.querySelectorAll('a[href]').forEach(function(a) {
    if (a.closest('#widget, .overlay-root, .launcher')) return;
    a.addEventListener('click', function(e) {
      var href = a.getAttribute('href') || '';
      if (href.indexOf('http') === 0) { e.preventDefault(); _toast('Внешняя ссылка: ' + (a.textContent||'').trim().substring(0,40)); }
    });
  });

  document.querySelectorAll('[class*="headerPhoneBlock"], [class*="headerPhoneBtn"]').forEach(function(el) {
    el.style.cursor = 'pointer';
    el.addEventListener('click', function() { _toast('Телефон поддержки: 147 (бесплатно по РБ)'); });
  });
  document.querySelectorAll('[class*="notification"], [class*="Notification"]').forEach(function(el) {
    if (el.closest('#widget')) return;
    el.style.cursor = 'pointer';
    el.addEventListener('click', function() {
      _modal('Уведомления', '<div style="display:flex;flex-direction:column;gap:10px">'
        + '<div style="padding:12px;border-left:3px solid #107F8C;background:#F0FDFA;border-radius:0 8px 8px 0">Платёж №1042 на сумму 12 500 BYN исполнен</div>'
        + '<div style="padding:12px;border-left:3px solid #F59E0B;background:#FFFBEB;border-radius:0 8px 8px 0">Срок действия сертификата ЭЦП истекает через 14 дней</div>'
        + '<div style="padding:12px;border-left:3px solid #6B7280;background:#F9FAFB;border-radius:0 8px 8px 0">Новый тариф «Бизнес Плюс» доступен для подключения</div>'
        + '</div>');
    });
  });

  document.querySelectorAll('select, [class*="Dropdown"], [class*="Select"]').forEach(function(el) {
    if (el.closest('#widget')) return;
    el.style.cursor = 'pointer';
  });

  var css = document.createElement('style');
  css.textContent =
    'li[class*="leftMenuItem-wrapper"]:hover{background:rgba(16,127,140,0.05)!important}' +
    'div[class*="accountsTableItem-container"]:hover{background:rgba(16,127,140,0.04)!important}' +
    '[class*="leftMenuItem-active"]{background:rgba(16,127,140,0.1)!important}' +
    'button[name*="dashboard."]:hover,.nav-block_item:hover{background:rgba(16,127,140,0.05)!important}' +
    '@keyframes spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}';
  document.head.appendChild(css);
});
</script>
"""


def _build_demo_html() -> str:
    import re

    html_path = _SBBOL_DIR / "СберБизнес — интернет-банк.html"
    html = html_path.read_text(encoding="utf-8", errors="replace")

    html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<script\b[^>]*/>', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<noscript\b[^>]*>.*?</noscript>', '', html, flags=re.DOTALL | re.IGNORECASE)

    html = html.replace("./СберБизнес — интернет-банк_files/", "/sbbol-assets/")

    visibility_fix = (
        '\n  <style>'
        'body,body[unresolved]{opacity:1!important;transition:none!important;'
        'display:block!important;overflow:auto!important;}'
        'main.main-app,.main-app{position:relative;z-index:1!important;}'
        '.pageWrap{display:flex!important;}'
        '[data-name="mainMenuContainer"]{position:relative!important;flex-shrink:0;}'
        '.mainContentWrapper{flex:1;min-width:0;padding-left:24px;box-sizing:border-box;}'
        '</style>'
    )
    html = html.replace(
        "</head>",
        visibility_fix + '\n  <link rel="stylesheet" href="/main.css">\n</head>',
        1,
    )

    widget_src = pathlib.Path("index.html").read_text(encoding="utf-8")
    body_start = widget_src.find("<body>") + len("<body>")
    body_end = widget_src.rfind("</body>")
    widget_body = widget_src[body_start:body_end].strip()
    widget_body = widget_body.replace('src="main.js"', 'src="/main.js"')
    widget_body = widget_body.replace("src='main.js'", "src='/main.js'")

    html = html.replace(
        "</body>",
        "\n\n<!-- SBERIK WIDGET -->\n" + widget_body + "\n"
        + _DEMO_INTERACTIVE_JS + "\n</body>",
        1,
    )
    return html


@app.get("/demo", response_class=HTMLResponse)
async def demo_page() -> HTMLResponse:
    if not (_SBBOL_DIR / "СберБизнес — интернет-банк.html").exists():
        raise HTTPException(status_code=404, detail="Demo file not found at C:/сбербизнес/")
    return HTMLResponse(_inject_api_key(_build_demo_html()))


@app.get("/login")
async def login_redirect() -> RedirectResponse:
    return RedirectResponse(url="/demo", status_code=302)


@app.post("/remote/token")
async def remote_token() -> JSONResponse:
    return JSONResponse({
        "holder": {
            "access_token": "demo-access-token",
            "refresh_token": "demo-refresh-token",
            "expires_in": 86400,
        },
        "ok": True,
        "authenticated": True,
    })


@app.get("/logout")
@app.post("/logout")
async def sbbol_logout() -> RedirectResponse:
    return RedirectResponse(url="/demo", status_code=302)


@app.get("/static/index.html")
async def sbbol_static_index() -> RedirectResponse:
    return RedirectResponse(url="/demo", status_code=302)


def _inject_api_key(html: str) -> str:
    snippet = f'<script>window.__SBERIK_API_KEY__={json.dumps(_API_KEY)};</script>'
    return html.replace("</head>", snippet + "\n</head>", 1)


@app.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse(url="/demo", status_code=302)


@app.get("/widget")
async def widget() -> HTMLResponse:
    html = pathlib.Path("index.html").read_text(encoding="utf-8")
    return HTMLResponse(_inject_api_key(html))


@app.get("/main.css")
async def css() -> FileResponse:
    return FileResponse("main.css", media_type="text/css")


@app.get("/main.js")
async def js_file() -> FileResponse:
    return FileResponse("main.js", media_type="application/javascript")


# ---------------------------------------------------------------------------
# API key dependency
# ---------------------------------------------------------------------------

async def _verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> None:
    if not _API_KEY:
        raise HTTPException(status_code=500, detail="Сервер не настроен: API_KEY не задан.")
    if not secrets.compare_digest(x_api_key, _API_KEY):
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий API-ключ.")


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    try:
        write_security_event(
            event_type="UNHANDLED_EXCEPTION",
            details=f"{type(exc).__name__} на {request.method} {request.url.path}",
            severity="HIGH",
        )
    except Exception:
        pass
    return JSONResponse(
        status_code=500,
        content={"detail": "Внутренняя ошибка сервера."},
    )


# ---------------------------------------------------------------------------
# Startup: periodic cleanup background task
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _start_cleanup_task() -> None:
    asyncio.create_task(_periodic_cleanup())


async def _periodic_cleanup() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            _cleanup_expired_confirmations_with_audit()
            _cleanup_expired_drafts()
            _cleanup_expired_contexts()
            _cleanup_expired_sessions()
        except Exception:
            pass


def _cleanup_expired_confirmations_with_audit() -> None:
    now = time.monotonic()
    expired = [
        (t, v) for t, v in list(_pending_confirmations.items())
        if v["expires_at"] < now
    ]
    for token, entry in expired:
        _pending_confirmations.pop(token, None)
        try:
            _write_audit(
                user_id=entry["user_id"],
                action=entry["action"],
                parameters=entry["parameters"],
                risk_level=entry.get("risk_level", "UNKNOWN"),
                result="expired_without_action",
            )
        except Exception:
            pass
        # Mark draft as expired
        _update_draft_status(entry.get("draft_id"), "expired")


def _cleanup_expired_drafts() -> None:
    now = time.monotonic()
    expired = [did for did, d in list(_drafts.items()) if d.get("expires_mono", 0) < now]
    for did in expired:
        if _drafts.get(did, {}).get("status") == "pending":
            _drafts[did]["status"] = "expired"
        _drafts.pop(did, None)


def _cleanup_expired_contexts() -> None:
    now = time.monotonic()
    expired = [
        cid for cid, ctx in list(_conversation_contexts.items())
        if now - ctx.get("last_active", 0) > _CONTEXT_TTL * 2
    ]
    for cid in expired:
        _conversation_contexts.pop(cid, None)
    context_store.cleanup_expired()
    session_tx_store.cleanup_expired()


def _cleanup_expired_sessions() -> None:
    now = time.monotonic()
    expired = [t for t, s in list(_sessions.items()) if s["expires_at"] < now]
    for t in expired:
        _sessions.pop(t, None)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_audit(
    *,
    user_id: str,
    action: str,
    parameters: dict[str, Any],
    risk_level: str,
    result: str,
    conversation_id: str | None = None,
    mode: str = "banking",
) -> None:
    entry = AuditEntry(
        timestamp=_now_iso(),
        user_id=user_id,
        action=action,
        parameters=parameters,
        risk_level=risk_level,
        result=result,
        conversation_id=conversation_id,
        mode=mode,
    )
    write_audit(entry)


def _user_profile(user_id: str) -> dict[str, Any]:
    return get_user(user_id) or {}


def _build_transfer_draft(
    parameters: dict[str, Any],
    risk: dict[str, Any],
    user_profile: dict[str, Any],
    draft_id: str | None = None,
) -> dict[str, Any]:
    recipient: str = parameters.get("recipient", "")
    known_recipients: list[str] = user_profile.get("known_recipients", [])

    is_known = recipient in known_recipients
    registry = get_counterparty(recipient) or {}
    has_registry = bool(registry)

    # For new recipients with user-provided details, show those instead of registry
    provided_account = parameters.get("account_number")
    provided_bank    = parameters.get("bank_name")
    provided_currency = parameters.get("currency_recipient")

    if is_known:
        recipient_status = "Существующий контрагент"
        recipient_info: dict[str, Any] = {
            "organization_name": registry.get("organization_name", recipient),
            "bank": provided_bank or registry.get("bank", "—"),
            "account_masked": provided_account or registry.get("account_masked", "—"),
            "last_four": registry.get("last_four", "—"),
            "currency": provided_currency or registry.get("currency", "BYN"),
        }
    elif has_registry:
        # In global registry but not in user's own list — bank details available
        recipient_status = "Новый контрагент"
        recipient_info = {
            "organization_name": registry.get("organization_name", recipient),
            "bank": provided_bank or registry.get("bank", "—"),
            "account_masked": provided_account or registry.get("account_masked", "—"),
            "last_four": registry.get("last_four", "—"),
            "currency": provided_currency or registry.get("currency", "BYN"),
        }
    elif provided_account and provided_bank:
        recipient_status = "Новый контрагент (реквизиты проверены)"
        recipient_info = {
            "organization_name": recipient,
            "bank": provided_bank,
            "account_masked": _mask_account(provided_account),
            "last_four": provided_account[-4:] if len(provided_account) >= 4 else "—",
            "currency": provided_currency or "BYN",
        }
    else:
        recipient_status = "Новый контрагент — требуются реквизиты"
        recipient_info = {
            "organization_name": recipient,
            "bank": "—",
            "account_masked": "—",
            "last_four": "—",
            "currency": "BYN",
        }

    result: dict[str, Any] = {
        "action": "initiate_transfer",
        "amount": parameters.get("amount"),
        "currency": provided_currency or "BYN",
        "recipient": recipient,
        "purpose": parameters.get("purpose"),
        "recipient_info": recipient_info,
        "recipient_status": recipient_status,
        "risk_level": risk.get("level", "UNKNOWN"),
        "risk_reasons": risk.get("reasons", []),
        # include draft_id so frontend can call /recipient-details
        "requires_details": not (is_known or has_registry or (provided_account and provided_bank)),
    }
    if draft_id:
        result["draft_id"] = draft_id
    return result


def _mask_account(account: str) -> str:
    """Show only last 4 digits of account number for display."""
    clean = account.replace(" ", "")
    if len(clean) > 8:
        return "BY** **** **** **** " + clean[-4:]
    return clean[-4:] if len(clean) >= 4 else "—"


def _build_transfer_confirmation_msg(
    gemini_msg: str,
    parameters: dict[str, Any],
    risk: dict[str, Any],
    risk_action: str = "show_warning_then_draft",
) -> str:
    amount = parameters.get("amount", 0)
    recipient = parameters.get("recipient", "")
    risk_reasons: list[str] = risk.get("reasons", [])

    try:
        amount_str = f"{float(amount):,.2f}"
    except (TypeError, ValueError):
        amount_str = "—"

    lines = [gemini_msg, ""]

    if risk_action == "show_draft":
        lines.append("💸 Черновик платёжного поручения готов:")
    elif risk_action == "require_draft_confirmation":
        lines.append("🔴 Обнаружены факторы повышенного риска. Прежде чем продолжить:")
    else:
        lines.append("⚠️ Обратите внимание:")

    if risk_reasons and risk_action != "show_draft":
        for reason in risk_reasons:
            lines.append(f"• {reason}")
        lines.append("")

    lines.append("💸 Черновик платёжного поручения:")
    lines.append(f"   Получатель: {recipient or '—'}")
    lines.append(f"   Сумма: {amount_str} BYN")

    if risk_action == "require_draft_confirmation":
        lines.extend(["", "Нажмите «Создать черновик» для перехода к экрану проверки или «Отменить» для отмены."])
    else:
        lines.extend(["", "Проверьте данные и нажмите «Подтвердить» для выполнения или «Отменить» для отмены."])

    return "\n".join(lines)


def _build_new_recipient_message(parameters: dict[str, Any]) -> str:
    """Message shown when recipient is unknown and details must be provided first."""
    recipient = parameters.get("recipient", "")
    amount = parameters.get("amount", 0)
    try:
        amount_str = f"{float(amount):,.2f}"
    except (TypeError, ValueError):
        amount_str = "—"
    return (
        f"Получатель «{recipient}» не найден в вашем справочнике контрагентов.\n\n"
        f"Для перевода {amount_str} BYN необходимо указать банковские реквизиты:\n"
        "• Номер расчётного счёта (IBAN BY или любой другой формат)\n"
        "• Название банка получателя\n"
        "• Валюта счёта\n\n"
        "Заполните форму ниже. После проверки реквизитов платёжное поручение будет готово к подтверждению."
    )


def _determine_risk_action(risk_level: str, action: str) -> str:
    """Determine UX flow based on risk level."""
    if action != "initiate_transfer":
        return "show_warning_then_draft"
    if risk_level == "LOW":
        return "show_draft"
    elif risk_level == "MEDIUM":
        return "show_warning_then_draft"
    else:
        return "require_draft_confirmation"


def _firewall_check(text: str, user_id: str, endpoint: str) -> None:
    fw = check_request(text)
    if fw["blocked"]:
        write_security_event(
            event_type="PROMPT_INJECTION_BLOCKED",
            details=(
                f"user={user_id} endpoint={endpoint} "
                f"reason={fw['reason']} severity={fw['severity']}"
            ),
            severity=fw["severity"],
        )
        raise HTTPException(
            status_code=400,
            detail="Ваш запрос не может быть обработан. Пожалуйста, переформулируйте.",
        )


def _whitelist_check(action: str | None, user_id: str, endpoint: str) -> None:
    if action is None:
        return
    if not validate_action(action):
        write_security_event(
            event_type="INVALID_ACTION_BLOCKED",
            details=f"user={user_id} endpoint={endpoint} action={action}",
            severity="HIGH",
        )
        raise HTTPException(
            status_code=400,
            detail="Запрос не может быть выполнен. Пожалуйста, переформулируйте.",
        )


# ---------------------------------------------------------------------------
# POST /api/v1/session — create demo session
# ---------------------------------------------------------------------------

@app.post("/api/v1/session", response_model=SessionResponse)
async def create_session(request: Request) -> SessionResponse:
    """Create a demo session. Cycles through demo users round-robin."""
    _metrics["requests_total"] += 1
    # Cycle demo users based on active session count for variety
    idx = len(_sessions) % len(_DEMO_USER_IDS)
    user_id = _DEMO_USER_IDS[idx]
    token = _create_session(user_id)
    return SessionResponse(
        session_token=token,
        user_id=user_id,
        expires_in=int(_SESSION_TTL),
    )


# ---------------------------------------------------------------------------
# POST /api/v1/chat
# ---------------------------------------------------------------------------

@app.post("/api/v1/chat", response_model=ChatResponse, dependencies=[Depends(_verify_api_key), Depends(_check_rate_limit)])
async def chat(request: ChatRequest) -> ChatResponse:
    _metrics["requests_total"] += 1
    t_start = time.monotonic()

    user_id = request.user_id
    raw_message = request.message
    req_mode = request.mode
    # conversation_id: UUID4 string, re-sent by client on every subsequent turn to maintain context.
    conversation_id = request.conversation_id or str(uuid.uuid4())

    # ------------------------------------------------------------------
    # STEP 1 — Prompt firewall
    # ------------------------------------------------------------------
    _firewall_check(raw_message, user_id, "/chat")

    # ------------------------------------------------------------------
    # STEP 2 — Privacy filter
    # ------------------------------------------------------------------
    try:
        privacy_result = filter_user_request(raw_message, {})
        clean_message: str = privacy_result["clean_message"]
    except Exception as exc:
        write_security_event("PRIVACY_FILTER_ERROR", str(exc)[:200], "MEDIUM")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера.")

    # ------------------------------------------------------------------
    # STEP 2b — Conversation context (structured, no financial data)
    # ------------------------------------------------------------------
    context_hint = ""
    _is_first_turn = False
    try:
        ctx = _get_or_create_context(conversation_id, user_id, session_id=conversation_id)
        context_hint = _build_context_hint(ctx)
        _is_first_turn = not bool(ctx.get("messages"))
        _store_message(conversation_id, "user", raw_message)
    except PermissionError:
        write_security_event(
            "CONTEXT_OWNERSHIP_VIOLATION",
            f"user={user_id} conversation_id={conversation_id}",
            "HIGH",
        )
        raise HTTPException(status_code=403, detail="Недействительный идентификатор разговора.")
    except Exception:
        pass

    # ------------------------------------------------------------------
    # STEP 3 + 4 — Gemini call + response validation
    # mode is forwarded so call_gemini can enforce the assistant-mode
    # guarantee at the LLM boundary (action=None, parameters={}).
    # ------------------------------------------------------------------
    try:
        gemini_resp = call_gemini(clean_message, context_hint=context_hint, mode=req_mode)
    except (ValueError, RuntimeError) as exc:
        write_security_event("GEMINI_RESPONSE_INVALID", str(exc)[:300], "HIGH")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера.")
    except Exception as exc:
        write_security_event("GEMINI_ERROR", str(exc)[:300], "HIGH")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера.")

    action: str | None = gemini_resp.action
    parameters: dict[str, Any] = gemini_resp.parameters
    confidence: str = gemini_resp.confidence

    parameters = _apply_follow_up_context(conversation_id, action, parameters)

    if _is_first_turn:
        _set_conversation_title(
            conversation_id,
            _generate_title(raw_message, action, gemini_resp.intent, parameters),
            locked=False,
        )

    # ------------------------------------------------------------------
    # STEP 4b — ASSISTANT MODE GATE
    # Runs BEFORE the whitelist, risk engine, and executor.
    # call_gemini already guarantees action=None in assistant mode, so
    # this branch handles informational responses from BOTH modes.
    #
    # Security invariants enforced here:
    #   • Whitelist is NEVER called in assistant mode.
    #   • Risk engine is NEVER called in assistant mode.
    #   • Executor is NEVER called in assistant mode.
    #   • Response always carries mode="assistant" (backend-authoritative).
    # ------------------------------------------------------------------
    if req_mode == "assistant":
        _update_context(conversation_id, None, {}, assistant_topic=gemini_resp.user_message, result=None)
        _write_audit(
            user_id=user_id,
            action="none",
            parameters={},
            risk_level="LOW",
            result="assistant_informational",
            conversation_id=conversation_id,
            mode="assistant",
        )
        _store_message(conversation_id, "ai", gemini_resp.user_message)
        _record_response_time((time.monotonic() - t_start) * 1000)
        return ChatResponse(
            user_message=gemini_resp.user_message,
            action_result=None,
            requires_confirmation=False,
            confirmation_message=None,
            conversation_id=conversation_id,
            mode="assistant",
        )

    # ------------------------------------------------------------------
    # STEP 5 — Whitelist validation (banking mode only)
    # ------------------------------------------------------------------
    _whitelist_check(action, user_id, "/chat")

    # Banking mode informational response (action=None returned by Gemini)
    if action is None:
        # Transfer intent with missing params → return blank/partial form instead of
        # asking for clarification in text. Covers "сделай перевод", "хочу перевести" etc.
        _is_transfer_intent = gemini_resp.intent in {
            "initiate_transfer", "transfer", "make_transfer", "перевод",
            "payment", "new_transfer", "open_transfer", "платёж", "платеж",
            "перечисление", "перевести", "create_transfer",
        } or "transfer" in gemini_resp.intent.lower() or "payment" in gemini_resp.intent.lower()
        if _is_transfer_intent:
            prefill: dict[str, Any] = {}
            if parameters.get("amount") is not None:
                prefill["amount"] = parameters["amount"]
            if parameters.get("recipient"):
                prefill["recipient"] = parameters["recipient"]
            if parameters.get("purpose"):
                prefill["purpose"] = parameters["purpose"]
            _update_context(conversation_id, None, {}, intent=gemini_resp.intent)
            _write_audit(
                user_id=user_id,
                action="none",
                parameters=parameters,
                risk_level="LOW",
                result="transfer_form_shown",
                conversation_id=conversation_id,
                mode=req_mode,
            )
            _store_message(conversation_id, "ai", gemini_resp.user_message)
            _record_response_time((time.monotonic() - t_start) * 1000)
            return ChatResponse(
                user_message=gemini_resp.user_message,
                action_result=None,
                requires_confirmation=False,
                confirmation_message=None,
                conversation_id=conversation_id,
                mode=req_mode,
                requires_transfer_form=True,
                transfer_prefill=prefill if prefill else None,
            )

        _update_context(conversation_id, None, {}, intent=gemini_resp.intent)
        _write_audit(
            user_id=user_id,
            action="none",
            parameters={},
            risk_level="LOW",
            result="informational_response",
            conversation_id=conversation_id,
            mode=req_mode,
        )
        _store_message(conversation_id, "ai", gemini_resp.user_message)
        _record_response_time((time.monotonic() - t_start) * 1000)
        return ChatResponse(
            user_message=gemini_resp.user_message,
            action_result=None,
            requires_confirmation=False,
            confirmation_message=None,
            conversation_id=conversation_id,
            mode=req_mode,
        )

    # ------------------------------------------------------------------
    # STEP 6 — Risk scoring
    # ------------------------------------------------------------------
    profile = _user_profile(user_id)
    try:
        risk = calculate_risk(action, parameters, profile)
    except Exception as exc:
        write_security_event("RISK_SCORING_ERROR", str(exc)[:200], "MEDIUM")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера.")

    if "score" not in risk:
        write_security_event("RISK_SCORE_MISSING", f"action={action}", "HIGH")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера.")
    risk_level: str = risk["level"]
    risk_score: int = risk["score"]

    # AI low confidence → escalate risk level and score by one step
    if confidence == "low":
        if risk_level == "LOW":
            risk_level = "MEDIUM"
        elif risk_level == "MEDIUM":
            risk_level = "HIGH"
        risk_score = min(risk_score + 20, 100)

    # ------------------------------------------------------------------
    # STEP 7 — Audit log (before execution)
    # ------------------------------------------------------------------
    # INVARIANT: initiate_transfer always requires confirmation regardless of score.
    _requires_confirmation = (risk_score >= 60) or (action == "initiate_transfer")
    if _requires_confirmation:
        confirmation_message = (
            "Проверьте данные перед подтверждением операции."
        )
    else:
        confirmation_message = None
    audit_result = "pending_confirmation" if _requires_confirmation else "executing"

    _write_audit(
        user_id=user_id,
        action=action,
        parameters=parameters,
        risk_level=risk_level,
        result=audit_result,
        conversation_id=conversation_id,
        mode=req_mode,
    )

    # ------------------------------------------------------------------
    # STEP 7b — Block transfers to unknown counterparties
    # Must run BEFORE the execution gate. No confirmation token is issued;
    # instead we return requires_recipient_details=True.
    # Overrides _requires_confirmation — token is issued only after /transfer/recipient-details.
    # ------------------------------------------------------------------
    if action == "initiate_transfer":
        _recipient = parameters.get("recipient", "")
        _profile_recipients: list[str] = profile.get("known_recipients", [])
        _in_registry = bool(get_counterparty(_recipient))
        _is_new_counterparty = (
            bool(_recipient)
            and _recipient not in _profile_recipients
            and not _in_registry
        )
        if _is_new_counterparty:
            # Check for an existing duplicate draft in requires_recipient_details
            existing_draft_id = _find_pending_recipient_details_draft(user_id, action, parameters)
            if existing_draft_id:
                pending_did = existing_draft_id
                _is_dup_rd = True
            else:
                pending_did = _create_draft(
                    user_id, action, parameters, risk_level,
                    status="requires_recipient_details",
                    conversation_id=conversation_id,
                )
                _is_dup_rd = False

            draft_details = _build_transfer_draft(parameters, risk, profile, draft_id=pending_did)
            user_msg = _build_new_recipient_message(parameters)
            if _is_dup_rd:
                user_msg = "⚠️ Черновик для этого получателя уже существует. " + user_msg

            _write_audit(
                user_id=user_id,
                action=action,
                parameters=parameters,
                risk_level=risk_level,
                result="requires_recipient_details",
                conversation_id=conversation_id,
                mode=req_mode,
            )
            _update_context(
                conversation_id, action, parameters,
                intent=gemini_resp.intent,
                result=None,
            )
            _store_message(conversation_id, "ai", user_msg)
            _record_response_time((time.monotonic() - t_start) * 1000)
            return ChatResponse(
                user_message=user_msg,
                action_result=None,
                requires_confirmation=False,
                confirmation_message="Укажите реквизиты получателя для продолжения перевода.",
                requires_recipient_details=True,
                pending_draft_id=pending_did,
                draft_details=draft_details,
                conversation_id=conversation_id,
                mode=req_mode,
            )

    # ------------------------------------------------------------------
    # STEP 8 — Execution gate
    # ------------------------------------------------------------------
    if _requires_confirmation:
        # Check per-user pending limit
        if _count_pending_for_user(user_id) >= _MAX_PENDING_PER_USER:
            raise HTTPException(
                status_code=429,
                detail="Превышено максимальное количество ожидающих подтверждений. Завершите предыдущие операции.",
            )

        # Duplicate transfer detection
        is_duplicate = False
        existing_token = _find_duplicate_transfer(user_id, action, parameters)
        if existing_token:
            is_duplicate = True
            confirmation_token = existing_token
            # Retrieve the draft_id from the existing pending confirmation entry
            draft_id = _pending_confirmations.get(existing_token, {}).get("draft_id")
        else:
            # Create draft record
            draft_id = _create_draft(user_id, action, parameters, risk_level)
            confirmation_token = str(uuid.uuid4())
            _store_confirmation(
                confirmation_token, user_id, action, parameters,
                risk_level=risk_level, draft_id=draft_id,
                conversation_id=conversation_id,
            )

        risk_action = _determine_risk_action(risk_level, action)

        if action == "initiate_transfer":
            draft_details = _build_transfer_draft(parameters, risk, profile, draft_id=draft_id)
            user_msg = _build_transfer_confirmation_msg(
                gemini_resp.user_message, parameters, risk, risk_action
            )
            if is_duplicate:
                user_msg = "⚠️ Похожий перевод уже ожидает подтверждения. " + user_msg
        else:
            draft_details = None
            risk_reasons = risk.get("reasons", [])
            warning_lines = "\n".join(f"• {r}" for r in risk_reasons)
            user_msg = (
                f"{gemini_resp.user_message}\n\n"
                "⚠️ Операция требует подтверждения из-за повышенного уровня риска:\n"
                f"{warning_lines}\n\n"
                "Нажмите «Подтвердить» для выполнения или «Отменить» для отмены."
            )

        _update_context(
            conversation_id, action, parameters,
            intent=gemini_resp.intent,
            result=None,
        )
        _store_message(conversation_id, "ai", user_msg)
        _record_response_time((time.monotonic() - t_start) * 1000)
        return ChatResponse(
            user_message=user_msg,
            action_result=None,
            requires_confirmation=True,
            confirmation_message=confirmation_message,
            confirmation_token=confirmation_token,
            draft_details=draft_details,
            risk_action=risk_action,
            conversation_id=conversation_id,
            mode=req_mode,
            is_duplicate=is_duplicate,
        )

    # Non-transfer action at LOW/MEDIUM risk — execute now.
    _stxs = session_tx_store.get_for_user(user_id) if action == "get_transactions" else None
    try:
        action_result = _executor.execute(action, parameters, user_id, session_txs=_stxs)
    except ValueError as exc:
        _write_audit(
            user_id=user_id, action=action, parameters=parameters,
            risk_level=risk_level, result=f"error: {str(exc)[:200]}",
            conversation_id=conversation_id,
        )
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        _write_audit(
            user_id=user_id, action=action, parameters=parameters,
            risk_level=risk_level, result="internal_error",
            conversation_id=conversation_id,
        )
        write_security_event("EXECUTOR_ERROR", str(exc)[:200], "MEDIUM")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера.")

    _update_context(
        conversation_id, action, parameters,
        intent=gemini_resp.intent,
        result=action_result if isinstance(action_result, dict) else None,
    )
    _store_message(conversation_id, "ai", gemini_resp.user_message)
    _record_response_time((time.monotonic() - t_start) * 1000)

    return ChatResponse(
        user_message=gemini_resp.user_message,
        action_result=action_result,
        requires_confirmation=False,
        confirmation_message=None,
        conversation_id=conversation_id,
        mode=req_mode,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/conversations — list conversations for a user
# GET /api/v1/conversations/{conv_id}/messages — get messages in a conversation
# PATCH /api/v1/conversations/{conv_id}/title — rename a conversation
# ---------------------------------------------------------------------------

@app.get("/api/v1/conversations", dependencies=[Depends(_verify_api_key)])
async def get_conversations(user_id: str = Query(..., min_length=1, max_length=64)) -> JSONResponse:
    if not _MODELS_USER_ID_RE.match(user_id.strip()):
        raise HTTPException(status_code=400, detail="Недопустимый user_id.")
    uid = user_id.strip()
    convs = []
    for conv_id, entry in list(_conversation_contexts.items()):
        if entry.get("user_id") != uid:
            continue
        if not entry.get("messages"):
            continue
        convs.append({
            "conversation_id": conv_id,
            "title": entry.get("title") or "Диалог",
            "created_at": entry.get("created_at", ""),
            "updated_at": entry.get("updated_at", ""),
            "message_count": len(entry.get("messages", [])),
        })
    convs.sort(key=lambda c: c["updated_at"], reverse=True)
    return JSONResponse({"conversations": convs})


@app.get("/api/v1/conversations/{conv_id}/messages", dependencies=[Depends(_verify_api_key)])
async def get_conversation_messages(
    conv_id: str,
    user_id: str = Query(..., min_length=1, max_length=64),
) -> JSONResponse:
    if not _MODELS_USER_ID_RE.match(user_id.strip()):
        raise HTTPException(status_code=400, detail="Недопустимый user_id.")
    uid = user_id.strip()
    entry = _conversation_contexts.get(conv_id)
    if entry is None or entry.get("user_id") != uid:
        raise HTTPException(status_code=404, detail="Разговор не найден.")
    return JSONResponse({
        "conversation_id": conv_id,
        "title": entry.get("title") or "Диалог",
        "created_at": entry.get("created_at", ""),
        "updated_at": entry.get("updated_at", ""),
        "messages": entry.get("messages", []),
    })


@app.delete("/api/v1/conversations/{conv_id}", dependencies=[Depends(_verify_api_key)])
async def delete_conversation(
    conv_id: str,
    user_id: str = Query(..., min_length=1, max_length=64),
) -> JSONResponse:
    if not _MODELS_USER_ID_RE.match(user_id.strip()):
        raise HTTPException(status_code=400, detail="Недопустимый user_id.")
    uid = user_id.strip()
    entry = _conversation_contexts.get(conv_id)
    if entry is None or entry.get("user_id") != uid:
        raise HTTPException(status_code=404, detail="Разговор не найден.")
    _conversation_contexts.pop(conv_id, None)
    context_store.delete(conv_id)
    return JSONResponse({"deleted": conv_id})


@app.patch("/api/v1/conversations/{conv_id}/title", dependencies=[Depends(_verify_api_key)])
async def rename_conversation(conv_id: str, request: RenameConversationRequest) -> JSONResponse:
    entry = _conversation_contexts.get(conv_id)
    if entry is None or entry.get("user_id") != request.user_id:
        raise HTTPException(status_code=404, detail="Разговор не найден.")
    new_title = request.title.strip()[:50]
    if not new_title:
        raise HTTPException(status_code=400, detail="Название не может быть пустым.")
    entry["title"] = new_title
    entry["title_locked"] = True
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    return JSONResponse({"conversation_id": conv_id, "title": new_title})


# ---------------------------------------------------------------------------
# POST /api/v1/confirm
# ---------------------------------------------------------------------------

@app.post("/api/v1/confirm", response_model=ConfirmResponse, dependencies=[Depends(_verify_api_key), Depends(_check_rate_limit)])
async def confirm(request: ConfirmRequest) -> ConfirmResponse:
    user_id = request.user_id
    token = request.confirmation_token
    confirmed = request.confirmed

    pending = _pop_confirmation(token)
    if pending is None:
        write_security_event(
            "CONFIRM_INVALID_TOKEN",
            f"user={user_id} token={token[:36]}",
            "HIGH",
        )
        raise HTTPException(status_code=404, detail="Токен подтверждения недействителен или истёк.")
    if pending["user_id"] != user_id:
        write_security_event(
            "CONFIRM_TOKEN_MISMATCH",
            f"claimed_user={user_id} token_owner={pending['user_id']}",
            "HIGH",
        )
        raise HTTPException(status_code=403, detail="Токен не принадлежит данному пользователю.")

    action: str = pending["action"]
    parameters: dict[str, Any] = pending["parameters"]
    draft_id: str | None = pending.get("draft_id")

    if not confirmed:
        _update_draft_status(draft_id, "cancelled")
        _write_audit(
            user_id=user_id,
            action=action,
            parameters=parameters,
            risk_level="UNKNOWN",
            result="cancelled_by_user",
        )
        _metrics["requests_cancelled"] += 1
        return ConfirmResponse(result=None, message="Операция отменена.")

    _whitelist_check(action, user_id, "/confirm")

    profile = _user_profile(user_id)
    try:
        risk = calculate_risk(action, parameters, profile)
    except Exception as exc:
        write_security_event("RISK_SCORING_ERROR", str(exc)[:200], "MEDIUM")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера.")

    risk_level: str = risk["level"]
    risk_score_at_confirm: int = risk.get("score", 0)
    stored_risk_level: str = pending.get("risk_level", "UNKNOWN")
    if risk_score_at_confirm >= 60 and stored_risk_level == "LOW":
        write_security_event(
            "CONFIRM_RISK_ESCALATED",
            f"user={user_id} action={action} score={risk_score_at_confirm} was={stored_risk_level}",
            "HIGH",
        )

    _write_audit(
        user_id=user_id,
        action=action,
        parameters=parameters,
        risk_level=risk_level,
        result="confirmed_executing",
    )

    try:
        action_result = _executor.execute(action, parameters, user_id)
    except ValueError as exc:
        _update_draft_status(draft_id, "cancelled")
        _write_audit(
            user_id=user_id, action=action, parameters=parameters,
            risk_level=risk_level, result=f"error: {str(exc)[:200]}",
        )
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        _update_draft_status(draft_id, "cancelled")
        _write_audit(
            user_id=user_id, action=action, parameters=parameters,
            risk_level=risk_level, result="internal_error",
        )
        write_security_event("EXECUTOR_ERROR_CONFIRM", str(exc)[:200], "MEDIUM")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера.")

    _update_draft_status(draft_id, "confirmed")
    _metrics["requests_confirmed"] += 1

    # store confirmed transfer in session for immediate visibility in history
    if action == "initiate_transfer" and isinstance(action_result, dict):
        _conv_id = pending.get("conversation_id", "")
        session_tx_store.add(SessionTransaction(
            operation_id=action_result.get("transfer_id", str(uuid.uuid4())),
            user_id=user_id,
            conversation_id=_conv_id,
            recipient_name=str(parameters.get("recipient", "")),
            amount=float(parameters.get("amount", 0)),
            currency="BYN",
            transaction_type="expense",
            execution_status="accepted",
            created_at=action_result.get("created_at", datetime.now(timezone.utc).isoformat()),
        ))

    return ConfirmResponse(result=action_result, message="Операция выполнена успешно.")


# ---------------------------------------------------------------------------
# POST /api/v1/transfer/recipient-details
# Accepts bank details for a draft in "requires_recipient_details" status.
# Validates, updates draft, creates confirmation token.
# ---------------------------------------------------------------------------

@app.post(
    "/api/v1/transfer/recipient-details",
    response_model=RecipientDetailsResponse,
    dependencies=[Depends(_verify_api_key), Depends(_check_rate_limit)],
)
async def add_recipient_details(request: RecipientDetailsRequest) -> RecipientDetailsResponse:
    user_id  = request.user_id
    draft_id = request.draft_id

    # ------------------------------------------------------------------
    # Lookup and ownership check
    # ------------------------------------------------------------------
    draft = _drafts.get(draft_id)
    if draft is None or time.monotonic() > draft.get("expires_mono", 0):
        write_security_event(
            "RECIPIENT_DETAILS_INVALID_DRAFT",
            f"user={user_id} draft_id={draft_id[:36]}",
            "MEDIUM",
        )
        raise HTTPException(
            status_code=404,
            detail="Черновик не найден или срок его действия истёк. Создайте перевод повторно.",
        )
    if draft["user_id"] != user_id:
        write_security_event(
            "RECIPIENT_DETAILS_OWNERSHIP_VIOLATION",
            f"claimed_user={user_id} draft_owner={draft['user_id']}",
            "HIGH",
        )
        raise HTTPException(status_code=403, detail="Черновик не принадлежит данному пользователю.")

    # ------------------------------------------------------------------
    # CRITICAL-01: Per-draft lock — prevent race conditions on concurrent
    # requests for the same draft_id. Non-blocking: if the lock is already
    # held (another request is in progress), return 409 immediately.
    # ------------------------------------------------------------------
    draft_lock = _get_draft_lock(draft_id)
    if not draft_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=(
                "Параллельная обработка этого черновика. "
                "Повторите запрос через несколько секунд."
            ),
        )
    try:
        # ── State machine guard ──────────────────────────────────────────
        # Validates the transition INSIDE the lock so the check and the
        # status update are atomic with respect to concurrent requests.
        _assert_valid_transition(draft_id, draft["status"], "pending")

        # ── Pending limit check — BEFORE status change (no rollback needed) ──
        if _count_pending_for_user(user_id) >= _MAX_PENDING_PER_USER:
            raise HTTPException(
                status_code=429,
                detail="Превышено максимальное количество ожидающих подтверждений.",
            )

        # ── Merge validated details into draft parameters ─────────────────
        updated_params = dict(draft["parameters"])
        updated_params["account_number"]     = request.account_number
        updated_params["bank_name"]          = request.bank_name
        updated_params["currency_recipient"] = request.currency
        updated_params["purpose"]            = request.purpose  # CRITICAL-01: required
        draft["parameters"] = updated_params

        # ── Re-score risk server-side ─────────────────────────────────────
        _whitelist_check(draft["action"], user_id, "/transfer/recipient-details")
        profile = _user_profile(user_id)
        try:
            risk = calculate_risk(draft["action"], updated_params, profile)
        except Exception as exc:
            write_security_event("RISK_SCORING_ERROR", str(exc)[:200], "MEDIUM")
            raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера.")

        risk_level: str = risk["level"]
        draft["risk_level"] = risk_level

        # ── Transition draft → pending ────────────────────────────────────
        _update_draft_status(draft_id, "pending")
        draft["expires_mono"] = time.monotonic() + _CONFIRMATION_TTL
        draft["expires_at"] = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + _CONFIRMATION_TTL,
            tz=timezone.utc,
        ).isoformat()

        confirmation_token = str(uuid.uuid4())
        _store_confirmation(
            confirmation_token, user_id, draft["action"], updated_params,
            risk_level=risk_level, draft_id=draft_id,
            conversation_id=draft.get("conversation_id", ""),
        )

        # ── Audit log ─────────────────────────────────────────────────────
        _write_audit(
            user_id=user_id,
            action=draft["action"],
            parameters={
                k: v for k, v in updated_params.items()
                if k not in ("account_number",)  # mask full account in audit
            },
            risk_level=risk_level,
            result="recipient_details_provided_pending_confirmation",
        )

        draft_details = _build_transfer_draft(updated_params, risk, profile, draft_id=draft_id)

    finally:
        draft_lock.release()

    return RecipientDetailsResponse(
        confirmation_token=confirmation_token,
        draft_id=draft_id,
        message=(
            "Реквизиты получателя проверены. "
            "Проверьте данные платёжного поручения и подтвердите перевод."
        ),
        draft_details=draft_details,
        risk_level=risk_level,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/notifications
# POST /api/v1/notifications/{notification_id}/read
# ---------------------------------------------------------------------------

@app.get("/api/v1/notifications", dependencies=[Depends(_verify_api_key)])
async def get_notifications(user_id: str) -> JSONResponse:
    items = _notifications.get(user_id, [])
    unread = sum(1 for n in items if not n["read"])
    return JSONResponse({"notifications": items, "unread": unread})


@app.post("/api/v1/notifications/{notification_id}/read", dependencies=[Depends(_verify_api_key)])
async def mark_notification_read(notification_id: str, user_id: str) -> JSONResponse:
    for n in _notifications.get(user_id, []):
        if n["id"] == notification_id:
            n["read"] = True
            return JSONResponse({"ok": True})
    raise HTTPException(status_code=404, detail="Уведомление не найдено.")


# ---------------------------------------------------------------------------
# GET /api/v1/drafts
# ---------------------------------------------------------------------------

@app.get("/api/v1/drafts", dependencies=[Depends(_verify_api_key)])
async def get_drafts(user_id: str) -> JSONResponse:
    user_drafts = [
        {k: v for k, v in d.items() if k != "expires_mono"}
        for d in _drafts.values()
        if d["user_id"] == user_id
    ]
    user_drafts.sort(key=lambda x: x["created_at"], reverse=True)
    return JSONResponse({"drafts": user_drafts, "count": len(user_drafts)})


# ---------------------------------------------------------------------------
# POST /api/v1/feedback
# ---------------------------------------------------------------------------

@app.post("/api/v1/feedback", dependencies=[Depends(_verify_api_key)])
async def submit_feedback(req: FeedbackRequest, user_id: str) -> JSONResponse:
    import logging
    feedback_logger = logging.getLogger("sberik.feedback")
    feedback_logger.info(
        "feedback user=%s conv=%s idx=%d type=%s comment=%s",
        user_id,
        req.conversation_id or "",
        req.message_index,
        req.feedback,
        (req.comment or "")[:200],
    )
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# GET /api/v1/metrics
# ---------------------------------------------------------------------------

@app.get("/api/v1/metrics", dependencies=[Depends(_verify_api_key)])
async def metrics() -> JSONResponse:
    times = _metrics["response_times_ms"]
    avg_ms = round(sum(times) / len(times), 1) if times else 0
    return JSONResponse({
        "requests_total": _metrics["requests_total"],
        "requests_blocked_firewall": _metrics["requests_blocked_firewall"],
        "requests_confirmed": _metrics["requests_confirmed"],
        "requests_cancelled": _metrics["requests_cancelled"],
        "gemini_fallback_activations": _metrics["gemini_fallback_activations"],
        "average_response_ms": avg_ms,
        "active_pending_confirmations": len(_pending_confirmations),
        "active_sessions": len(_sessions),
        "active_conversations": len(_conversation_contexts),
        "total_drafts": len(_drafts),
        "version": "2.0.0",
        "timestamp": _now_iso(),
    })


# ---------------------------------------------------------------------------
# GET /api/v1/health
# ---------------------------------------------------------------------------

@app.get("/api/v1/health")
async def health() -> dict:
    return {
        "status": "ok",
        "timestamp": _now_iso(),
        "gemini_configured": bool(os.getenv("GEMINI_API_KEY")),
        "version": "2.0.0",
        "pipeline_steps": [
            "prompt_firewall",
            "privacy_filter",
            "conversation_context",
            "gemini",
            "response_validation",
            "assistant_mode_gate",
            "action_whitelist",
            "risk_scoring",
            "confidence_escalation",
            "audit_log",
            "new_counterparty_gate",
            "duplicate_detection",
            "execution_gate",
            "action_executor",
        ],
        "features": [
            "dual_mode_banking_assistant",
            "conversation_context",
            "risk_engine_ux_differentiation",
            "duplicate_transfer_detection",
            "new_counterparty_recipient_details",
            "draft_state_machine",
            "notification_center",
            "draft_history",
            "ai_confidence_scoring",
            "periodic_token_cleanup",
            "session_management",
            "user_feedback",
            "metrics",
            "favorites",
        ],
    }
