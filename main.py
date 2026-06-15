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
    now_iso = datetime.now(timezone.utc).isoformat()
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
_draft_locks: dict[str, threading.Lock] = {}
_draft_locks_guard = threading.Lock()


def _get_draft_lock(draft_id: str) -> threading.Lock:
    """Return (creating if needed) the per-draft Lock for concurrent-access protection."""
    with _draft_locks_guard:
        if draft_id not in _draft_locks:
            _draft_locks[draft_id] = threading.Lock()
        return _draft_locks[draft_id]


def _assert_valid_transition(draft_id: str, current_status: str, target_status: str) -> None:
    """Raise HTTP 409 if the transition is not in _VALID_DRAFT_TRANSITIONS."""
    if (current_status, target_status) not in _VALID_DRAFT_TRANSITIONS:
        write_security_event(
            "INVALID_DRAFT_TRANSITION",
            f"draft_id={draft_id[:36]} {current_status} → {target_status}",
            "HIGH",
        )
        raise HTTPException(
            status_code=409,
            detail=f"Недопустимый переход состояния черновика: {current_status} → {target_status}.",
        )


def _update_draft_status(draft_id: str | None, status: str) -> None:
    """Update draft status after validating the transition."""
    if not draft_id or draft_id not in _drafts:
        return
    current = _drafts[draft_id].get("status", "unknown")
    if (current, status) not in _VALID_DRAFT_TRANSITIONS:
        write_security_event(
            "INVALID_DRAFT_TRANSITION",
            f"draft_id={draft_id[:36]} {current} → {status}",
            "HIGH",
        )
        return
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
        "created_at": datetime.now(timezone.utc).isoformat(),
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
    """Return only non-sensitive parameters safe to store in conversation history."""
    safe: dict[str, str] = {}
    if action == "get_transactions":
        if parameters.get("filter"):
            safe["filter"] = str(parameters["filter"])[:50]
        if parameters.get("period"):
            safe["period"] = str(parameters["period"])[:50]
    elif action == "navigate":
        if parameters.get("section"):
            safe["section"] = str(parameters["section"])[:50]
    elif action == "create_report":
        if parameters.get("report_type"):
            safe["report_type"] = str(parameters["report_type"])[:50]
        if parameters.get("period"):
            safe["period"] = str(parameters["period"])[:50]
    return safe


_TOPIC_STRIP_RE = re.compile(r'[\[\]{}]')

# ---------------------------------------------------------------------------
# Conversation history helpers
# ---------------------------------------------------------------------------

_ACTION_TITLES: dict[str, str] = {
    "get_balance":        "Баланс счёта",
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
        if new_filters:
            entry["last_filters"] = new_filters

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


_FOLLOW_UP_FIELDS: dict[str, list[str]] = {
    "get_transactions": ["filter", "period"],
    "create_report":    ["report_type", "period", "report_subtype"],
}

_EMPTY_VALUES: frozenset[str] = frozenset({"", "all", "none", "any"})


def _apply_follow_up_context(
    conversation_id: str,
    action: str | None,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    if action not in _FOLLOW_UP_FIELDS:
        return parameters

    ctx_obj = context_store.get(conversation_id)
    if not ctx_obj or not ctx_obj.last_filters:
        return parameters

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
    history = ctx.get("history", [])
    recent = history[-3:]
    turn_strs: list[str] = []
    for turn in recent:
        if "action" in turn:
            action = turn["action"]
            extras = [f"{k}={turn[k]}" for k in ("filter", "period", "section", "report_type", "report_subtype") if k in turn]
            turn_strs.append(f"{action}({', '.join(extras)})" if extras else action)
        elif "topic" in turn:
            turn_strs.append(f"[{turn['topic']}]")

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
# Rate limiting
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
# App initialization and Static Mounting
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

# Проверяем существование папок перед монтированием, чтобы избежать системных крашей
for folder in ["icon", "fonts"]:
    pathlib.Path(folder).mkdir(exist_ok=True)

app.mount("/icon", StaticFiles(directory="icon"), name="icon")
app.mount("/fonts", StaticFiles(directory="fonts"), name="fonts")

_SBBOL_DIR = pathlib.Path(__file__).parent.resolve()
_SBBOL_ASSETS = _SBBOL_DIR

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
      + '<div style="border:1px solid #E5E7EB;border-radius:12px;padding:24px;cursor:pointer;transition:box-shadow .2s" onmouseover="this.style.boxShadow=\'0 4px 12px rgba(0,0,0,0.08)\'" onmouseout="this.style.boxShadow=\'\'"><div style="font-size:18px;font-weight:600;margin-bottom:8px">Кредиты для бизнеса</div></div></div></div>'
  };

  window.navigateToSection = function(section) {
    var wrapper = document.querySelector(S);
    if (!wrapper) return;
    if (section === 'moneyAndEvents' || !pages[section]) {
      wrapper.innerHTML = dashboardHTML;
    } else {
      wrapper.innerHTML = pages[section];
    }
  };
});
</script>
"""

def _inject_api_key(html_content: str) -> str:
    """Inject API Key and custom logic securely before sending the HTML response."""
    if "</body" in html_content:
        return html_content.replace("</body>", f"{_DEMO_INTERACTIVE_JS}</body>")
    return html_content + _DEMO_INTERACTIVE_JS


def _build_demo_html() -> str:
    global _DEMO_HTML_CACHE
    if _DEMO_HTML_CACHE is not None:
        return _DEMO_HTML_CACHE
    html_path = _SBBOL_DIR / "index.html"
    if not html_path.exists():
        return "<h3>Ошибка: Файл index.html не найден в корне проекта.</h3>"
    _DEMO_HTML_CACHE = html_path.read_text(encoding="utf-8", errors="replace")
    return _DEMO_HTML_CACHE


# ---------------------------------------------------------------------------
# Core API Router and Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/demo")


@app.get("/demo", response_class=HTMLResponse)
async def demo_page(request: Request):
    if not (_SBBOL_DIR / "index.html").exists():
        raise HTTPException(status_code=404, detail="Файл интерфейса index.html не найден")
    return HTMLResponse(_inject_api_key(_build_demo_html()))


@app.post("/api/auth/session", response_model=SessionResponse)
async def start_session():
    # Выбираем случайного демо-пользователя из разрешенного списка
    user_id = secrets.choice(_DEMO_USER_IDS)
    token = _create_session(user_id)
    return SessionResponse(session_token=token, user_id=user_id)


@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(payload: ChatRequest, request: Request, x_api_key: str = Header(None)):
    start_time = time.perf_counter()
    _metrics["requests_total"] += 1
    
    await _check_rate_limit(request)
    
    # 1. Prompt Firewall
    if not check_request(payload.message):
        _metrics["requests_blocked_firewall"] += 1
        raise HTTPException(status_code=400, detail="Запрос заблокирован системой безопасности.")
        
    # 2. Privacy filter
    sanitized_message = filter_user_request(payload.message)
    
    # Резолв контекста диалога
    ctx = _get_or_create_context(payload.conversation_id, payload.user_id)
    _store_message(payload.conversation_id, "user", payload.message)
    
    # Наращиваем историю для Gemini
    context_hint = _build_context_hint(ctx)
    
    # 3. Gemini call
    gemini_response = await call_gemini(sanitized_message, context_hint)
    
    # 4. Response validation & 5. Action Whitelist
    action = gemini_response.get("action")
    parameters = gemini_response.get("parameters", {})
    if action:
        validate_action(action)
        parameters = _apply_follow_up_context(payload.conversation_id, action, parameters)

    # 6. Risk scoring
    risk_level, requires_confirm = calculate_risk(action, parameters)
    
    # Генерируем название чата, если это первое сообщение
    if not ctx.get("title"):
        title = _generate_title(payload.message, action, gemini_response.get("intent", ""), parameters)
        _set_conversation_title(payload.conversation_id, title)

    execution_result = None
    confirmation_token = None
    
    # 7. Audit log & 8. Execution gate
    if action:
        if requires_confirm:
            confirmation_token = secrets.token_hex(16)
            _store_confirmation(
                token=confirmation_token,
                user_id=payload.user_id,
                action=action,
                parameters=parameters,
                risk_level=risk_level,
                conversation_id=payload.conversation_id
            )
            write_audit(action, parameters, status="PENDING_CONFIRMATION", user_id=payload.user_id)
        else:
            # 9. Action executor (безопасное синхронное исполнение mock-данных)
            execution_result = _executor.execute(action, parameters, user_id=payload.user_id)
            write_audit(action, parameters, status="SUCCESS", user_id=payload.user_id)
            
    _update_context(
        payload.conversation_id, 
        action, 
        parameters, 
        intent=gemini_response.get("intent", ""), 
        result=execution_result
    )
    
    reply_text = gemini_response.get("reply", "Запрос обработан успешно.")
    _store_message(payload.conversation_id, "assistant", reply_text)
    
    duration_ms = (time.perf_counter() - start_time) * 1000
    _record_response_time(duration_ms)
    
    return ChatResponse(
        reply=reply_text,
        action=action,
        parameters=parameters,
        requires_confirmation=requires_confirm,
        confirmation_token=confirmation_token,
        risk_level=risk_level,
        result=execution_result
    )


@app.post("/api/chat/confirm", response_model=ConfirmResponse)
async def confirm_endpoint(payload: ConfirmRequest):
    entry = _pop_confirmation(payload.confirmation_token)
    if not entry:
        raise HTTPException(status_code=400, detail="Токен подтверждения недействителен или истёк.")
        
    action = entry["action"]
    parameters = entry["parameters"]
    user_id = entry["user_id"]
    
    # Выполнение подтвержденного действия
    execution_result = _executor.execute(action, parameters, user_id=user_id)
    write_audit(action, parameters, status="CONFIRMED_AND_SUCCESS", user_id=user_id)
    _metrics["requests_confirmed"] += 1
    
    if entry["conversation_id"]:
        _update_context(entry["conversation_id"], action, parameters, result=execution_result)
        
    return ConfirmResponse(success=True, result=execution_result)


@app.get("/api/metrics")
async def metrics_endpoint():
    # Защищенный служебный эндпоинт для мониторинга состояния
    current_metrics = dict(_metrics)
    times = current_metrics.get("response_times_ms", [])
    current_metrics["avg_response_time_ms"] = sum(times) / len(times) if times else 0.0
    current_metrics["active_pending_confirmations"] = len(_pending_confirmations)
    return JSONResponse(content=current_metrics)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main.py:app", host="0.0.0.0", port=10000, reload=True)
