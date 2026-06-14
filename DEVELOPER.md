# Developer Guide

## Setup

```bash
python -m venv venv && source venv/bin/activate   # or venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env   # set GEMINI_API_KEY and API_KEY
uvicorn main:app --reload
```

Optional overrides: `GEMINI_MODEL` (default: `gemini-2.0-flash`), `CORS_ORIGINS` (default: `http://localhost,http://127.0.0.1`).

Demo page: `http://localhost:8000/demo`. API docs: `http://localhost:8000/docs`.

## Tests

```bash
# All tests (no API key required — Gemini is mocked)
python -m pytest tests_security.py tests_integration.py -v
# 121 passed

# Individual module tests
python -m pytest agent/tests.py security/tests.py privacy/tests.py executor/tests.py -v
```

## Zero-Trust Pipeline (13 steps, enforced — no step may be skipped)

1. **Prompt Firewall** (`security/prompt_firewall.py`) — NFKC normalisation + 7 injection/exfiltration detectors. Blocks before any user data is read.
2. **Privacy Filter** (`privacy/`) — strips IBAN, cards, tax IDs, phones, emails, passport numbers from the message before Gemini sees it.
3. **Conversation Context** (`main.py:_get_or_create_context`) — structured TTL-30min context lookup; builds `context_hint` for follow-up questions. No financial data stored.
4. **Gemini** (`agent/gemini.py`) — stateless intent extraction, returns `{intent, action, parameters, confidence, user_message}`. Keyword fallback activates on 429/quota.
5. **Response Validation** (`agent/gemini.py:validate_gemini_response`) — strict schema: unknown fields rejected, `user_message` ≤ 500 chars, `action` must be in whitelist, no forbidden keys in `parameters`, `confidence` defaults to `high`.
6. **Action Whitelist** (`security/whitelist.py`) — exact case-sensitive match: `get_balance`, `get_transactions`, `create_report`, `initiate_transfer`, `get_tariffs`, `get_requisites`, `navigate`, `get_counterparties`, `get_favorites`.
7. **Assistant Mode Gate** — if `mode="assistant"` and `action!=null`, action is discarded and only `user_message` is returned. Logged in audit.
8. **Risk Scoring** (`security/risk_scoring.py`) — server-only: unknown recipient (+40), unknown+amount>1000 (+20), amount>2×avg (+30), off-hours (+20), amount>10000 (+10). Score≥60→HIGH, ≥30→MEDIUM.
9. **Confidence Escalation** — `confidence="low"` from Gemini escalates risk level by one tier (LOW→MEDIUM, MEDIUM→HIGH). AI confidence can only *increase* friction, never decrease it.
10. **Audit Log** (`security/audit.py`) — append-only JSON lines to `logs/audit.log` written *before* execution. Security events → `logs/security.log`.
11. **Duplicate Detection** (`main.py:_find_duplicate_transfer`) — same `user_id+action+amount+recipient` with live TTL → reuse existing token + `is_duplicate=true`. Prevents token proliferation.
12. **Execution Gate** — all `initiate_transfer` + any HIGH-risk action require `/api/v1/confirm`. Per-user limit: 3 concurrent pending tokens.
13. **Action Executor** (`executor/actions.py`) — mock data only. Balance: exact value hidden, coarse range exposed. Transactions: amounts replaced with ranges. Requisites: account numbers masked.

## Key Invariants

- **Client data never trusted for risk level** — `/api/v1/confirm` always re-scores risk server-side.
- **Gemini is stateless** — no session memory. System prompt forbids it from executing actions or accessing data.
- **`FORBIDDEN_FIELDS` in `privacy/policy.py`** — single source of truth used by both the privacy filter and response validator.
- **All errors return generic 500 to clients**; details go only to `security.log`.
- **Confirmation tokens**: UUID, single-use, TTL=15min. Expired tokens write `expired_without_action` to audit.

## In-Memory Stores (restart-safe — for production replace with Redis/DB)

| Store | TTL | Purpose |
|---|---|---|
| `_pending_confirmations` | 15 min | Confirmation tokens |
| `_sessions` | 1 h (rolling) | Demo session → user_id |
| `_conversation_contexts` | 30 min inactivity | Follow-up context |
| `_drafts` | 24 h | Draft operation history |
| `_notifications` | — | Per-user notification list |

Background cleanup task runs every 60 s; writes audit entries for expired tokens.

## Risk UX Flows

| Level | `risk_action` | UX |
|---|---|---|
| LOW | `show_draft` | Draft shown inline, one-click confirm |
| MEDIUM | `show_warning_then_draft` | Warning reasons + draft |
| HIGH | `require_draft_confirmation` | Full confirmation screen with security block |

## New Endpoints (v2.0)

```
POST /api/v1/session                   → SessionResponse
GET  /api/v1/notifications?user_id=…  → {notifications[], unread}
POST /api/v1/notifications/{id}/read  → {ok}
GET  /api/v1/drafts?user_id=…         → {drafts[], count}
POST /api/v1/feedback?user_id=…       → {ok}
GET  /api/v1/metrics                  → counters + avg_response_ms
```

## Test Users

| user_id | Company | Balance |
|---|---|---|
| user_001 | ООО «ТехноСтрой БЕЛ» | 2 000 000.00 BYN |
| user_002 | ИП Романова Екатерина Сергеевна | 2 000 000.00 BYN |
| user_003 | ООО «Агрокомплекс Нива» | 2 000 000.00 BYN |
