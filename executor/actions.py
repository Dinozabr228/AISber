import math
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.mock import MOCK_COUNTERPARTY_REGISTRY, get_user
from security.whitelist import validate_action

# Fields stripped from transaction records before returning to client.
_STRIP_FROM_TX: frozenset[str] = frozenset({
    "account_number", "card_number", "tax_id", "passport_data",
    "personal_id", "customer_id", "internal_id", "description",
})

# Legal entity identifiers valid for SberBusiness Belarus B2B transfers.
_LEGAL_FORMS: frozenset[str] = frozenset({
    "ооо", "оао", "зао", "чуп", "чтуп", "руп", "куп", "муп", "уп", "гп",
    "ип", "тда", "тоо", "банк", "фонд", "союз", "ассоциация", "холдинг",
    "сооо", "иооо", "зооо",
    "llc", "ojsc", "cjsc",
})

_MONTH_NAMES = (
    "", "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)

_MONTH_RU_TO_NUM: dict[str, int] = {
    "январь": 1, "января": 1, "янв": 1,
    "февраль": 2, "февраля": 2, "фев": 2,
    "март": 3, "марта": 3, "мар": 3,
    "апрель": 4, "апреля": 4, "апр": 4,
    "май": 5, "мая": 5,
    "июнь": 6, "июня": 6, "июн": 6,
    "июль": 7, "июля": 7, "июл": 7,
    "август": 8, "августа": 8, "авг": 8,
    "сентябрь": 9, "сентября": 9, "сен": 9,
    "октябрь": 10, "октября": 10, "окт": 10,
    "ноябрь": 11, "ноября": 11, "ноя": 11,
    "декабрь": 12, "декабря": 12, "дек": 12,
}


def _filter_history_by_period(history: list[dict], period: str) -> list[dict]:
    """Filter transaction history by period string.

    Accepts: 'last_month', 'current_month', 'май 2026', 'май', '2026'.
    Returns full history if period cannot be parsed — graceful degradation.
    """
    if not period:
        return history

    now = datetime.now(timezone.utc)
    period_lower = period.lower().strip()
    year: int | None = None
    month: int | None = None

    if period_lower == "last_month":
        month = now.month - 1 if now.month > 1 else 12
        year = now.year if now.month > 1 else now.year - 1
    elif period_lower in ("current_month", "this_month"):
        month = now.month
        year = now.year
    else:
        for part in period_lower.split():
            if part.isdigit() and len(part) == 4:
                year = int(part)
            elif part in _MONTH_RU_TO_NUM:
                month = _MONTH_RU_TO_NUM[part]

    if year is None and month is None:
        return history
    if year is None:
        year = now.year

    filtered = []
    for tx in history:
        date_str = tx.get("date", "")
        try:
            tx_date = datetime.fromisoformat(date_str)
            if tx_date.year == year and (month is None or tx_date.month == month):
                filtered.append(tx)
        except (ValueError, TypeError):
            pass

    return filtered if filtered else history

# Maximum single transfer amount (demo guard against absurd values).
_MAX_TRANSFER_AMOUNT = 10_000_000.0
_MIN_TRANSFER_AMOUNT = 0.01


def _is_b2b_recipient(recipient: str) -> bool:
    words = recipient.lower().replace("«", "").replace("»", "").split()
    return any(w.strip(".,") in _LEGAL_FORMS for w in words)


def _amount_range(amount: float) -> str:
    if amount < 1_000:
        return "до 1 000 BYN"
    if amount < 5_000:
        return "1 000–5 000 BYN"
    if amount < 20_000:
        return "5 000–20 000 BYN"
    if amount < 100_000:
        return "20 000–100 000 BYN"
    return "свыше 100 000 BYN"


def _balance_range(balance: float) -> str:
    """Coarse balance range — exact balance is never exposed to clients."""
    if balance < 5_000:
        return "до 5 000 BYN"
    if balance < 10_000:
        return "до 10 000 BYN"
    if balance < 50_000:
        return "до 50 000 BYN"
    if balance < 100_000:
        return "до 100 000 BYN"
    if balance < 200_000:
        return "до 200 000 BYN"
    return "свыше 200 000 BYN"


def _current_period() -> str:
    now = datetime.now(timezone.utc)
    return f"{_MONTH_NAMES[now.month]} {now.year}"


def _validate_amount(amount: float) -> None:
    """Raise ValueError for any amount that is invalid, unsafe, or out of range."""
    if math.isnan(amount) or math.isinf(amount):
        raise ValueError("Недопустимое значение суммы.")
    if amount <= 0:
        raise ValueError("Сумма перевода должна быть больше нуля.")
    if amount < _MIN_TRANSFER_AMOUNT:
        raise ValueError(f"Минимальная сумма перевода — {_MIN_TRANSFER_AMOUNT:.2f} BYN.")
    if amount > _MAX_TRANSFER_AMOUNT:
        raise ValueError(
            f"Сумма {amount:,.2f} BYN превышает допустимый лимит "
            f"({_MAX_TRANSFER_AMOUNT:,.0f} BYN)."
        )


# Keyword aliases for section matching (normalised, ё→е).
_SECTION_KEYWORDS_MAP: dict[str, list[str]] = {
    "payments":            ["платеж", "расчет", "расчёт", "оплат", "перевод", "payment"],
    "statement":           ["выписк", "транзакц", "операц", "statement", "история"],
    "salary":              ["зарплат", "сотрудник", "ведомост", "salary"],
    "productsAndServices": ["продукт", "услуг", "кредит", "депозит", "лизинг", "эквайр"],
    "partner-services":    ["партнер", "партнёр", "бухгалтер", "юрид", "облак", "partner"],
    "user-account":        ["настройк", "аккаунт", "профил", "уведомлен", "setting", "account"],
    "other":               ["прочее", "справк", "письм", "валют"],
}

_NAVIGATION_SECTIONS: list[dict[str, str]] = [
    {
        "section_id": "payments",
        "section_name": "Расчёты",
        "path": "Расчёты → Платёжные поручения → Создать",
        "description": "Платёжные поручения, переводы контрагентам, оплата счетов поставщиков",
    },
    {
        "section_id": "statement",
        "section_name": "Выписка",
        "path": "Выписка → Расчётный счёт → Скачать",
        "description": "Выписка по расчётному счёту, история операций, экспорт в PDF и CSV",
    },
    {
        "section_id": "salary",
        "section_name": "Зарплатный проект",
        "path": "Зарплатный проект → Ведомости → Создать ведомость",
        "description": "Зарплатные ведомости, выплаты сотрудникам, реестры зачислений",
    },
    {
        "section_id": "productsAndServices",
        "section_name": "Продукты и услуги",
        "path": "Продукты и услуги → Кредиты → Подать заявку",
        "description": "Кредитные продукты для бизнеса, депозиты, лизинг, эквайринг",
    },
    {
        "section_id": "partner-services",
        "section_name": "Сервисы партнёров",
        "path": "Сервисы партнёров → Бухгалтерия онлайн",
        "description": "Бухгалтерия онлайн, юридические и облачные сервисы для бизнеса",
    },
    {
        "section_id": "user-account",
        "section_name": "Настройки",
        "path": "Настройки → Профиль компании → Уведомления",
        "description": "Настройки аккаунта, уведомления, управление пользователями",
    },
    {
        "section_id": "other",
        "section_name": "Прочее",
        "path": "Прочее → Документы → Письма в банк",
        "description": "Справочные документы, письма в банк, валютный контроль, архив",
    },
]

_MOCK_TARIFFS: list[dict[str, Any]] = [
    {
        "name": "Стандарт",
        "monthly_fee": 0,
        "transfers_per_month": 10,
        "transfer_fee_percent": 0.5,
        "currency": "BYN",
        "description": "Для малого бизнеса с редкими платежами",
    },
    {
        "name": "Бизнес",
        "monthly_fee": 29.90,
        "transfers_per_month": 50,
        "transfer_fee_percent": 0.3,
        "currency": "BYN",
        "description": "Оптимальный тариф для активных компаний",
    },
    {
        "name": "Корпоратив",
        "monthly_fee": 79.90,
        "transfers_per_month": -1,
        "transfer_fee_percent": 0.1,
        "currency": "BYN",
        "description": "Безлимитные платежи для крупных предприятий",
    },
]


class ActionExecutor:

    def execute(
        self,
        action: str,
        parameters: dict[str, Any],
        user_id: str,
        session_txs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not validate_action(action):
            raise ValueError(f"Действие «{action}» не входит в список разрешённых операций.")

        if action == "get_balance":
            return self._get_balance(user_id)
        if action == "get_transactions":
            return self._get_transactions(
                user_id,
                filter_type=str(parameters.get("filter", "all")),
                period=str(parameters.get("period", "")),
                session_txs=session_txs,
            )
        if action == "create_report":
            return self._create_report(
                user_id,
                report_type=str(parameters.get("report_type", "summary")),
                period=str(parameters.get("period", "")),
                report_subtype=str(parameters.get("report_subtype", "summary")),
            )
        if action == "initiate_transfer":
            return self._initiate_transfer(
                user_id,
                amount=float(parameters.get("amount", 0)),
                recipient=str(parameters.get("recipient", "")),
            )
        if action == "get_tariffs":
            return self._get_tariffs()
        if action == "get_requisites":
            return self._get_requisites(user_id)
        if action == "navigate":
            return self._navigate(section=str(parameters.get("section", "")))
        if action == "get_counterparties":
            return self._get_counterparties(user_id)
        if action == "get_favorites":
            return self._get_favorites(user_id)

        raise ValueError(f"Маршрут для действия «{action}» не определён.")

    # ------------------------------------------------------------------
    # get_balance — exact balance masked; only coarse range returned
    # ------------------------------------------------------------------

    def _get_balance(self, user_id: str) -> dict[str, Any]:
        try:
            user = _require_user(user_id)
            balance: float = user["balance"]
            return {
                "balance": round(balance, 2),
                "balance_range": _balance_range(balance),
                "currency": "BYN",
                "company_name": user["company_name"],
                "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            }
        except (KeyError, ValueError):
            raise
        except Exception as exc:
            raise RuntimeError(f"Ошибка при получении баланса: {exc}") from exc

    # ------------------------------------------------------------------
    # get_transactions — last 5 operations with exact amounts
    # ------------------------------------------------------------------

    def _get_transactions(
        self,
        user_id: str,
        filter_type: str = "all",
        period: str = "",
        session_txs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        try:
            user = _require_user(user_id)
            history: list[dict] = list(user.get("transaction_history", []))

            if session_txs:
                seen_ids: set[str] = set()
                merged: list[dict] = []
                for tx in session_txs:
                    oid = tx.get("_operation_id", "")
                    if oid and oid not in seen_ids:
                        seen_ids.add(oid)
                        merged.append(tx)
                history = merged + history

            if period:
                history = _filter_history_by_period(history, period)

            if filter_type in ("incoming", "income"):
                history = [tx for tx in history if tx.get("type") == "income"]
            elif filter_type in ("outgoing", "expense"):
                history = [tx for tx in history if tx.get("type") == "expense"]

            # Sort by date descending (session txs appear first as most recent)
            sorted_history = sorted(
                history,
                key=lambda tx: tx.get("date", ""),
                reverse=True,
            )[:5]

            safe_txs = []
            for tx in sorted_history:
                safe_txs.append({
                    "date": tx.get("date", ""),
                    "amount": round(float(tx.get("amount", 0)), 2),
                    "label": tx.get("recipient") or tx.get("sender") or "",
                    "type": tx.get("type", ""),
                })

            return {
                "transactions": safe_txs,
                "count": len(safe_txs),
                "company_name": user["company_name"],
            }
        except (KeyError, ValueError):
            raise
        except Exception as exc:
            raise RuntimeError(f"Ошибка при получении транзакций: {exc}") from exc

    # ------------------------------------------------------------------
    # create_report — period derived from request or current month;
    # filters transaction_history by date when period is parseable.
    # ------------------------------------------------------------------

    def _create_report(self, user_id: str, report_type: str, period: str = "", report_subtype: str = "summary") -> dict[str, Any]:
        try:
            user = _require_user(user_id)
            history: list[dict] = user.get("transaction_history", [])

            resolved_period = period.strip() if period.strip() else _current_period()
            filtered = _filter_history_by_period(history, resolved_period)

            income_txs = [tx for tx in filtered if tx.get("type") == "income"]
            expense_txs = [tx for tx in filtered if tx.get("type") == "expense"]

            total_income = round(sum(float(tx.get("amount", 0)) for tx in income_txs), 2)
            total_expenses = round(sum(float(tx.get("amount", 0)) for tx in expense_txs), 2)
            tax = round(total_expenses * 0.2, 2)
            balance = round(total_income - total_expenses, 2)

            category_totals: dict[str, float] = {}
            for tx in expense_txs:
                category = tx.get("category") or tx.get("recipient", "Прочее")
                category_totals[category] = (
                    category_totals.get(category, 0.0) + float(tx.get("amount", 0))
                )
            top_expenses = sorted(
                [{"category": k, "amount": round(v, 2)} for k, v in category_totals.items()],
                key=lambda x: x["amount"],
                reverse=True,
            )[:3]

            period_display = resolved_period.capitalize()

            return {
                "period": period_display,
                "income": total_income,
                "expenses": total_expenses,
                "tax": tax,
                "balance": balance,
                "top_expenses": top_expenses,
                "subtype": report_subtype,
            }
        except (KeyError, ValueError):
            raise
        except Exception as exc:
            raise RuntimeError(f"Ошибка при создании отчёта: {exc}") from exc

    # ------------------------------------------------------------------
    # initiate_transfer — validates, deducts balance, records transaction
    # ------------------------------------------------------------------

    def _initiate_transfer(self, user_id: str, amount: float, recipient: str) -> dict[str, Any]:
        try:
            user = _require_user(user_id)

            # CRIT-06: reject NaN / Inf / out-of-range before any arithmetic
            _validate_amount(amount)
            amount = round(amount, 2)

            # Validate recipient
            if not recipient.strip():
                raise ValueError("Получатель не указан.")
            if not _is_b2b_recipient(recipient):
                raise ValueError(
                    "Получатель не является юридическим лицом. "
                    "СберБизнес выполняет переводы только между организациями "
                    "(ООО, ИП, ЗАО, ОАО, ЧУП, РУП и др.)."
                )

            # Self-transfer guard
            if recipient.lower().replace("«", "").replace("»", "").strip() == \
               user["company_name"].lower().replace("«", "").replace("»", "").strip():
                raise ValueError(
                    "Перевод на собственный счёт компании не допускается."
                )

            # CRIT-03 / CRIT-05: balance check — negative balance is impossible
            balance: float = user["balance"]
            if amount > balance:
                raise ValueError(
                    f"Недостаточно средств для выполнения операции. "
                    f"Доступный остаток: {balance:,.2f} BYN."
                )

            # CRITICAL-01: the executor creates a confirmed transfer draft and returns it
            # for submission to the core banking system. Balance is NOT mutated here —
            # in a real system the core banking layer performs the actual deduction
            # after processing. This demo returns "pending_confirmation" to reflect that
            # the transfer has been accepted and is awaiting final processing.
            transfer_id = str(uuid.uuid4())
            return {
                "status": "accepted",
                "transfer_id": transfer_id,
                "amount_byn": amount,
                "recipient": recipient,
                "initiator": user["company_name"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        except (KeyError, ValueError):
            raise
        except Exception as exc:
            raise RuntimeError(f"Ошибка при инициации перевода: {exc}") from exc

    def _get_tariffs(self) -> dict[str, Any]:
        try:
            tariffs_out = []
            for t in _MOCK_TARIFFS:
                entry = dict(t)
                if entry["transfers_per_month"] == -1:
                    entry["transfers_per_month"] = "Безлимитно"
                tariffs_out.append(entry)
            return {"tariffs": tariffs_out, "currency": "BYN", "valid_from": "2026-01-01"}
        except Exception as exc:
            raise RuntimeError(f"Ошибка при получении тарифов: {exc}") from exc

    def _get_requisites(self, user_id: str) -> dict[str, Any]:
        try:
            user = _require_user(user_id)
            return {
                "company_name": user["company_name"],
                "bank": "ОАО «Сбер Банк»",
                "bic": "SBBEBY2X",
                "account_number": "BY** **** **** **** **** ****",
                "currency": "BYN",
                "note": "Полные реквизиты доступны только в защищённом разделе личного кабинета.",
            }
        except (KeyError, ValueError):
            raise
        except Exception as exc:
            raise RuntimeError(f"Ошибка при получении реквизитов: {exc}") from exc

    # ------------------------------------------------------------------
    # get_counterparties — MEDIUM-02: counterparty directory
    # ------------------------------------------------------------------

    def _get_counterparties(self, user_id: str) -> dict[str, Any]:
        try:
            user = _require_user(user_id)
            known: list[str] = user.get("known_recipients", [])
            history: list[dict] = user.get("transaction_history", [])

            # Find the most recent expense transfer date per recipient
            last_dates: dict[str, str] = {}
            for tx in history:
                if tx.get("type") == "expense":
                    recip = tx.get("recipient", "")
                    date = tx.get("date", "")
                    if recip and date:
                        if recip not in last_dates or date > last_dates[recip]:
                            last_dates[recip] = date

            result: list[dict[str, Any]] = []
            for name in known:
                registry_entry = MOCK_COUNTERPARTY_REGISTRY.get(name, {})
                result.append({
                    "organization_name": name,
                    "bank": registry_entry.get("bank", "—"),
                    "account_masked": registry_entry.get("account_masked", "—"),
                    "last_four": registry_entry.get("last_four", "—"),
                    "currency": registry_entry.get("currency", "BYN"),
                    "last_transfer_date": last_dates.get(name, "—"),
                })

            result.sort(
                key=lambda x: x["last_transfer_date"] if x["last_transfer_date"] != "—" else "0",
                reverse=True,
            )

            return {
                "counterparties": result,
                "count": len(result),
                "company_name": user["company_name"],
            }
        except (KeyError, ValueError):
            raise
        except Exception as exc:
            raise RuntimeError(f"Ошибка при получении контрагентов: {exc}") from exc

    def _get_favorites(self, user_id: str) -> dict[str, Any]:
        try:
            user = _require_user(user_id)
            favorites: list[str] = user.get("favorites", [])
            result: list[dict[str, Any]] = []
            for name in favorites:
                registry_entry = MOCK_COUNTERPARTY_REGISTRY.get(name, {})
                result.append({
                    "organization_name": name,
                    "bank": registry_entry.get("bank", "—"),
                    "account_masked": registry_entry.get("account_masked", "—"),
                    "currency": registry_entry.get("currency", "BYN"),
                })
            return {
                "favorites": result,
                "count": len(result),
                "company_name": user["company_name"],
            }
        except (KeyError, ValueError):
            raise
        except Exception as exc:
            raise RuntimeError(f"Ошибка при получении избранных: {exc}") from exc

    def _navigate(self, section: str) -> dict[str, Any]:
        try:
            query = section.lower().strip().replace("ё", "е")
            matched: dict[str, str] | None = None
            if query:
                for s in _NAVIGATION_SECTIONS:
                    sid = s["section_id"]
                    name_norm = s["section_name"].lower().replace("ё", "е")
                    desc_norm = s["description"].lower().replace("ё", "е")
                    aliases = [kw.replace("ё", "е") for kw in _SECTION_KEYWORDS_MAP.get(sid, [])]
                    if (query == sid.lower()
                            or query in name_norm
                            or query in desc_norm
                            or any(kw in query for kw in aliases)):
                        matched = s
                        break
            return {
                "matched_section": matched,
                "sections": _NAVIGATION_SECTIONS,
            }
        except Exception as exc:
            raise RuntimeError(f"Ошибка при получении навигации: {exc}") from exc


def _require_user(user_id: str) -> dict[str, Any]:
    user = get_user(user_id)
    if user is None:
        raise ValueError("Пользователь не найден.")
    return user


# ---------------------------------------------------------------------------
# Fuzzy intent matching — suggests an action, never executes directly.
# Result must still pass whitelist validation, risk scoring, and audit log.
# ---------------------------------------------------------------------------

from difflib import get_close_matches as _get_close_matches

KNOWN_INTENTS: dict[str, str] = {
    "баланс": "get_balance",
    "счёт": "get_balance",
    "выписка": "get_transactions",
    "история": "get_transactions",
    "операции": "get_transactions",
    "отчёт": "create_report",
    "перевод": "initiate_transfer",
    "платёж": "initiate_transfer",
    "тарифы": "get_tariffs",
    "реквизиты": "get_requisites",
    "расчёты": "get_transactions",
    "навигация": "show_navigation",
    "помощь": "show_navigation",
    "разделы": "show_navigation",
}


def fuzzy_match_intent(user_input: str) -> dict:
    """Return the closest known intent for user_input.

    Security contract:
    - Only SUGGESTS an action; never executes anything.
    - Caller must run result through whitelist validation, risk scoring,
      and audit logging before acting on the suggested action.
    - confidence='low'  → caller must set requires_confirmation=True.
    - matched=False     → let Gemini handle clarification; do not execute.
    """
    words = user_input.lower().split()
    for word in words:
        matches = _get_close_matches(
            word, KNOWN_INTENTS.keys(), n=1, cutoff=0.6
        )
        if matches:
            return {
                "matched": True,
                "original_word": word,
                "matched_intent": matches[0],
                "action": KNOWN_INTENTS[matches[0]],
                "confidence": "high" if word == matches[0] else "low",
            }
    return {"matched": False, "action": None, "confidence": None}
