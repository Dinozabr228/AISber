import re
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

# user_id: только буквы, цифры, подчёркивание, дефис — без path separators и спецсимволов
_USER_ID_RE = re.compile(r'^[a-zA-Z0-9_\-]{1,64}$')
_CONV_ID_RE  = re.compile(r'^[a-zA-Z0-9_\-]{1,128}$')


class ChatRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    message: str = Field(..., min_length=1, max_length=2000)
    # Optional conversation context
    conversation_id: Optional[str] = Field(None, max_length=128)
    # Mode: "banking" (default) or "assistant"
    mode: str = Field(default="banking", pattern=r'^(banking|assistant)$')

    @field_validator("user_id")
    @classmethod
    def _validate_user_id(cls, v: str) -> str:
        v = v.strip()
        if not _USER_ID_RE.match(v):
            raise ValueError("user_id содержит недопустимые символы.")
        return v

    @field_validator("message")
    @classmethod
    def _validate_message(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Сообщение не может быть пустым.")
        return v

    @field_validator("conversation_id")
    @classmethod
    def _validate_conv_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not _CONV_ID_RE.match(v):
            raise ValueError("conversation_id содержит недопустимые символы.")
        return v


class ChatResponse(BaseModel):
    user_message: str
    action_result: Any
    requires_confirmation: bool
    confirmation_message: Optional[str] = None
    disclaimer: str = "ИИ может ошибаться. Пожалуйста, проверьте информацию."
    confirmation_token: Optional[str] = None
    draft_details: Optional[dict] = None
    # Risk UX flow: "show_draft" | "show_warning_then_draft" | "require_draft_confirmation"
    risk_action: Optional[str] = None
    # Conversation tracking
    conversation_id: Optional[str] = None
    # Whether this was handled in assistant mode (no banking ops executed)
    mode: str = "banking"
    # True when an existing duplicate confirmation token was reused
    is_duplicate: bool = False
    requires_recipient_details: bool = False
    # draft_id to pass to POST /api/v1/transfer/recipient-details
    pending_draft_id: Optional[str] = None


class ConfirmRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    confirmation_token: str = Field(..., min_length=1, max_length=128)
    confirmed: bool

    @field_validator("user_id")
    @classmethod
    def _validate_user_id(cls, v: str) -> str:
        v = v.strip()
        if not _USER_ID_RE.match(v):
            raise ValueError("user_id содержит недопустимые символы.")
        return v


class ConfirmResponse(BaseModel):
    result: Any
    message: str


class GeminiResponse(BaseModel):
    intent: str
    action: Optional[str] = None
    parameters: dict
    requires_confirmation: bool
    user_message: str
    # AI self-reported confidence — only used to INCREASE friction, never decrease
    confidence: str = "high"


class AuditEntry(BaseModel):
    timestamp: str
    user_id: str
    action: str
    parameters: dict
    risk_level: str
    result: str
    conversation_id: Optional[str] = None
    mode: str = "banking"


class SessionResponse(BaseModel):
    session_token: str
    user_id: str
    expires_in: int


class NotificationItem(BaseModel):
    id: str
    text: str
    level: str
    created_at: str
    read: bool


class DraftItem(BaseModel):
    draft_id: str
    user_id: str
    action: str
    parameters: dict
    status: str  # pending | confirmed | cancelled | expired
    created_at: str
    expires_at: str
    risk_level: Optional[str] = None


_ALLOWED_CURRENCIES: frozenset[str] = frozenset({"BYN", "USD", "EUR", "RUB"})

_ACCOUNT_RE = re.compile(r'^[A-Za-z0-9 \-\.]{5,100}$')
_IBAN_BY_RE  = re.compile(r'^BY\d{2}[A-Z0-9]{24}$')


class RecipientDetailsRequest(BaseModel):
    """CRITICAL-01: submitted by client after backend returns requires_recipient_details=True.
    All fields are required — no payment can be drafted without complete recipient data."""
    user_id: str = Field(..., min_length=1, max_length=64)
    draft_id: str = Field(..., min_length=1, max_length=128)
    account_number: str = Field(..., min_length=5, max_length=100)
    bank_name: str = Field(..., min_length=1, max_length=200)
    currency: str = Field(..., min_length=3, max_length=3)
    purpose: str = Field(..., min_length=1, max_length=500)

    @field_validator("user_id")
    @classmethod
    def _validate_user_id(cls, v: str) -> str:
        v = v.strip()
        if not _USER_ID_RE.match(v):
            raise ValueError("user_id содержит недопустимые символы.")
        return v

    @field_validator("account_number")
    @classmethod
    def _validate_account_number(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Номер счёта не может быть пустым.")
        normalized = v.upper().replace(" ", "")
        if normalized.startswith("BY"):
            if not _IBAN_BY_RE.match(normalized):
                raise ValueError(
                    "Неверный формат IBAN. Ожидается: BY + 2 цифры + 24 символа "
                    "(пример: BY20OLMP31350000000936000000)."
                )
        elif not _ACCOUNT_RE.match(v):
            raise ValueError(
                "Номер счёта содержит недопустимые символы. "
                "Разрешены: буквы, цифры, пробелы, дефисы, точки."
            )
        return v

    @field_validator("bank_name")
    @classmethod
    def _validate_bank_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Название банка не может быть пустым.")
        return v

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in _ALLOWED_CURRENCIES:
            allowed = ", ".join(sorted(_ALLOWED_CURRENCIES))
            raise ValueError(f"Валюта должна быть одной из: {allowed}.")
        return v

    @field_validator("purpose")
    @classmethod
    def _validate_purpose(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Назначение платежа не может быть пустым.")
        return v


class RecipientDetailsResponse(BaseModel):
    """Returned after recipient details are validated and draft moves to pending."""
    confirmation_token: str
    draft_id: str
    message: str
    draft_details: Optional[dict] = None
    risk_level: str


class FeedbackRequest(BaseModel):
    conversation_id: Optional[str] = Field(None, max_length=128)
    message_index: int = Field(..., ge=0)
    feedback: str = Field(..., pattern=r'^(correct|incorrect|not_helpful)$')
    comment: Optional[str] = Field(None, max_length=500)

    @field_validator("user_id", mode="before", check_fields=False)
    @classmethod
    def _noop(cls, v: Any) -> Any:
        return v


class RenameConversationRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=50)

    @field_validator("user_id")
    @classmethod
    def _validate_user_id(cls, v: str) -> str:
        v = v.strip()
        if not _USER_ID_RE.match(v):
            raise ValueError("user_id содержит недопустимые символы.")
        return v

    @field_validator("title")
    @classmethod
    def _validate_title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Название не может быть пустым.")
        return v
