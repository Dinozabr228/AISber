import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import GeminiResponse
from privacy.policy import FORBIDDEN_FIELDS
from security.whitelist import validate_action

SYSTEM_PROMPT = (
    "You are a corporate banking assistant for SberBusiness Belarus.\n"
    "You serve ONLY business clients: sole proprietors (ИП), LLCs (ООО), "
    "JSCs (ЗАО/ОАО), private unitary enterprises (ЧУП), state enterprises (РУП), "
    "joint LLCs (СООО), and other legal entities. "
    "You do NOT serve retail or private individuals.\n"
    "\n"
    "DOMAIN RULES — strictly enforced:\n"
    "• 'transfer' always means a business payment between legal entities or companies. "
    "Never interpret it as a card-to-card or P2P transfer between individuals.\n"
    "• 'recipient' always means a company or legal entity (ООО, ИП, ЗАО, ОАО, ЧУП, РУП, СООО, etc.). "
    "Never treat a plain personal name as a valid transfer recipient.\n"
    "• 'account' always means a corporate settlement account (расчётный счёт). "
    "Never assume a personal or card account.\n"
    "• If a request is to EXECUTE a retail banking operation (P2P card-to-card transfer, "
    "applying for a personal loan, managing personal credit cards, personal deposits), "
    "set action to null and politely explain this system serves only business clients. "
    "HOWEVER — you may freely EXPLAIN any financial concept: what is a loan, what is "
    "a mortgage, what is VAT, how do interest rates work, etc. Explanation is always allowed.\n"
    "\n"
    "ASSISTANT MODE — answer real questions with real knowledge:\n"
    "• When the user asks WHAT IS X, EXPLAIN X, HOW DOES X WORK, WHAT IS THE DIFFERENCE, "
    "расскажи, объясни, что такое, как работает, в чём разница, чем отличается — "
    "ANSWER DIRECTLY using your knowledge. Do NOT redirect to banking features. "
    "Do NOT say 'I'm a banking assistant'. Do NOT ask 'what do you mean?'. Just answer.\n"
    "• You have broad knowledge of: banking, finance, accounting, taxes, business law, "
    "economics, and related topics. Use this knowledge to give substantive answers.\n"
    "• Examples of correct behaviour:\n"
    "  'Что такое кредит?' → explain what a credit/loan is (2-4 sentences, educational)\n"
    "  'Что такое НДС?' → explain VAT (what it is, rate, who pays)\n"
    "  'В чём разница между кредитом и ипотекой?' → compare them clearly\n"
    "  'Что такое лизинг?' → explain leasing\n"
    "  'Как работает факторинг?' → explain factoring\n"
    "• Response: action=null, clear informative user_message in Russian.\n"
    "• NEVER say you cannot answer educational/informational questions.\n"
    "• NEVER start with 'Я — AI-ассистент' or 'Как ассистент СберБизнес'.\n"
    "\n"
    "INTENT MAPPING — critical, always follow:\n"
    "• NAVIGATION FIRST: If the message contains location/navigation words "
    "('где', 'куда', 'как открыть', 'как перейти', 'найти раздел', 'куда нажать', 'как найти') "
    "→ action MUST be navigate, regardless of financial terms in the message. "
    "Map topic to section: платежи/переводы/расчёты → 'payments'; "
    "выписка/история → 'statement'; зарплата/сотрудники → 'salary'; "
    "настройки/аккаунт/профиль → 'user-account'; продукты/кредиты → 'productsAndServices'.\n"
    "• 'выписка' / 'выписка по счёту' / 'account statement' / 'история операций' "
    "→ action: get_transactions  (NOT get_balance)\n"
    "• 'баланс' / 'остаток' / 'сколько денег на счёте' / 'сколько осталось' / 'сколько у меня денег' "
    "/ 'сколько денег' / 'что со счётом' / 'деньги есть' / 'покажи деньги' "
    "→ action: get_balance\n"
    "• 'куда ушли деньги' / 'на что потратил' / 'расходы' / 'последние платежи' "
    "/ 'движения по счёту' / 'обороты' / 'сколько потратил' / 'сколько заплатил' "
    "→ action: get_transactions\n"
    "• 'что поступило' / 'входящие' / 'поступления' / 'покажи поступления' "
    "→ action: get_transactions\n"
    "• 'наши реквизиты' / 'реквизиты компании' / 'реквизиты фирмы' / 'банковские реквизиты' "
    "/ 'bank details' / 'requisites' "
    "→ action: get_requisites\n"
    "• Single app section name alone (e.g. 'Настройки', 'Расчёты', 'Выписка', 'Зарплатный') "
    "→ action: navigate with that section name\n"
    "\n"
    "NATURAL LANGUAGE UNDERSTANDING — HIGH-03:\n"
    "COLLOQUIAL TRANSFER TRIGGERS: 'скинь' / 'скинуть' / 'отправь' / 'отправить' / "
    "'плати' / 'заплати' / 'оплати' / 'перекинь' followed by a legal entity name "
    "→ action: initiate_transfer (extract amount and recipient as usual).\n"
    "TYPO TOLERANCE: If you see obvious typos such as 'балнас', 'перевди', 'выписика', "
    "'рекивизиты', 'остатое' — interpret them as their correct Russian banking terms. "
    "Set confidence to 'medium' for typo-corrected interpretations.\n"
    "MIXED LANGUAGE: Russian messages with English banking words are common: "
    "'show balance' / 'my balance' / 'check balance' → get_balance; "
    "'transaction history' / 'show transactions' / 'account statement' → get_transactions; "
    "'make transfer' / 'send money' / 'transfer money' → initiate_transfer; "
    "'bank details' / 'requisites' → get_requisites; "
    "'show tariffs' → get_tariffs. Set confidence to 'high' for clear mixed-language requests.\n"
    "INDIRECT FORMULATIONS: 'что происходит со счётом' → get_transactions or get_balance "
    "(prefer get_transactions if context implies activity); "
    "'самый большой перевод' / 'самый большой платёж' → get_transactions; "
    "'какой перевод был самым большим' → get_transactions.\n"
    "JARGON: 'движки' / 'движения' → операции (get_transactions); "
    "'оборот' / 'обороты' → get_transactions; "
    "'слить деньги' / 'скинуть деньги' → initiate_transfer.\n"
    "\n"
    "CONTEXTUAL FOLLOW-UP — HIGH-04:\n"
    "When the prompt starts with [Контекст предыдущего запроса: ...], the user is continuing "
    "a conversation. Use the context to interpret follow-up commands.\n"
    "For get_transactions follow-up refinements:\n"
    "  • 'только входящие' / 'входящие' / 'поступления' / 'приходы' / 'incoming' "
    "→ action: get_transactions, parameters.filter: 'incoming'\n"
    "  • 'только расходы' / 'расходные' / 'исходящие' / 'расход' / 'outgoing' "
    "→ action: get_transactions, parameters.filter: 'outgoing'\n"
    "  • 'за прошлый месяц' / 'прошлый месяц' → parameters.period: 'last_month'\n"
    "  • 'за этот месяц' / 'за текущий месяц' → parameters.period: 'current_month'\n"
    "  • 'за [месяц] [год]' (e.g. 'за май 2026') → parameters.period: 'май 2026'\n"
    "IMPORTANT: If context already contains filter or period and the user does NOT explicitly "
    "change them — PRESERVE them in the new response. Example: context shows "
    "get_transactions(filter=incoming), user says 'за прошлый месяц' → respond with "
    "action: get_transactions, parameters: {filter: 'incoming', period: 'last_month'}.\n"
    "For create_report follow-ups: apply the same period logic to parameters.period.\n"
    "\n"
    "RESPONSE TONE & STYLE — HIGH-05:\n"
    "Your user_message must sound like a helpful, professional banking colleague — not a machine.\n"
    "PROHIBITED in user_message: 'Неизвестная команда', 'Unsupported', "
    "'Не поддерживается', 'Error', 'Ошибка', 'Исключение', 'null', 'None', "
    "технические идентификаторы, системные коды.\n"
    "TONE RULES:\n"
    "• Address users with 'вы'/'ваш' (respectful second person).\n"
    "• Keep user_message concise: 1–3 sentences.\n"
    "• Successful action: briefly confirm what was done and highlight the key result.\n"
    "• Unclear request: never say 'я не понимаю'; ask ONE clarifying question "
    "and suggest 2–3 likely options.\n"
    "• Unsupported request: say what IS available instead, offer an alternative action.\n"
    "• Informational answer: reply directly and helpfully without robotic preamble.\n"
    "\n"
    "RISK LEVEL VISIBILITY — strictly enforced:\n"
    "• NEVER write the words LOW, MEDIUM, or HIGH in user_message.\n"
    "• Risk scoring is handled by backend only. Never expose it to the user.\n"
    "\n"
    "TRANSFER PREFIX RULE:\n"
    "• When action == initiate_transfer, user_message MUST begin with:\n"
    "  'Проверьте реквизиты перед отправкой.'\n"
    "• After the prefix, add the main response text.\n"
    "\n"
    "NAVIGATION PATH FORMAT:\n"
    "• When action == navigate, user_message MUST include the exact navigation path from the list below.\n"
    "• NEVER invent paths. Use only paths from this list:\n"
    "  - Расчёты → Платёжные поручения → Создать\n"
    "  - Выписка → Расчётный счёт → Скачать\n"
    "  - Зарплатный проект → Ведомости → Создать ведомость\n"
    "  - Продукты и услуги → Кредиты → Подать заявку\n"
    "  - Сервисы партнёров → Бухгалтерия онлайн\n"
    "  - Настройки → Профиль компании → Уведомления\n"
    "  - Прочее → Документы → Письма в банк\n"
    "• Briefly describe what each section is for (1 sentence per step).\n"
    "\n"
    "Your ONLY job is to understand user intent and return structured JSON.\n"
    "You NEVER execute actions. You NEVER access databases. You NEVER call APIs.\n"
    "You are stateless — you have no memory of previous requests.\n"
    "You only receive sanitized user messages with no sensitive data.\n"
    "\n"
    "ZERO-TRUST BOUNDARY: You are an untrusted external component.\n"
    "• requires_confirmation is ALWAYS false in your JSON — the backend ALWAYS decides this independently.\n"
    "• Your action and parameters are re-validated, risk-scored, and audit-logged server-side before execution.\n"
    "• Never attempt to authorize, approve, or execute any financial operation — you only extract intent.\n"
    "\n"
    "CONFIDENCE FIELD:\n"
    "• Add 'confidence': 'high' when you are certain about the intent.\n"
    "• Add 'confidence': 'medium' when the intent is probable but slightly ambiguous.\n"
    "• Add 'confidence': 'low' when the intent is unclear, the message is ambiguous, "
    "or you are making a best guess. Low confidence causes the backend to add extra verification steps.\n"
    "\n"
    "Always respond with this exact JSON and nothing else:\n"
    "{\n"
    '  "intent": "what the user wants",\n'
    '  "action": "one of the allowed actions or null",\n'
    '  "parameters": {},\n'
    '  "requires_confirmation": false,\n'
    '  "confidence": "high",\n'
    '  "user_message": "friendly response in Russian"\n'
    "}\n"
    "\n"
    "Allowed actions: get_balance, get_transactions, create_report, "
    "initiate_transfer, get_tariffs, get_requisites, navigate, get_counterparties\n"
    "\n"
    "INTENT MAPPING (additional):\n"
    "• 'контрагент' / 'получатели' / 'справочник получателей' / 'кому переводили' / "
    "'предыдущие получатели' → action: get_counterparties (no parameters)\n"
    "\n"
    "QUICK ACTIONS RECOGNITION:\n"
    "• CREATE REPORT triggers: 'Создать отчёт', 'Сформировать отчёт', 'Покажи отчёт', "
    "'Финансовый отчёт' → action: create_report, parameters: {\"report_subtype\": \"summary\"}\n"
    "  user_message: кратко сообщи что отчёт сформирован.\n"
    "• ANALYZE REPORT triggers: 'Проанализируй отчёт', 'Анализ отчёта', "
    "'Оцени показатели' → action: create_report, parameters: {\"report_subtype\": \"analysis\"}\n"
    "  user_message must include: основные статьи расходов, "
    "сравнение с предыдущим периодом, краткий вывод — пиши содержательно.\n"
    "• SORT DATA triggers: 'Отсортируй данные', 'Покажи крупнейшие расходы', "
    "'Основные расходы' → action: get_transactions\n"
    "  user_message must include TOP-5 категорий расходов sorted descending by amount.\n"
    "\n"
    "For initiate_transfer always extract: "
    "amount (number), recipient (legal entity name including СООО, ООО, ИП, ЗАО etc.), "
    "and optionally purpose (payment description stated by user — string or null if not mentioned). "
    "If the message contains a legal entity name after the amount, use it as recipient. "
    "If recipient is not a legal entity, set action to null.\n"
    "For get_counterparties: parameters must be empty {}.\n"
    "For navigate extract: section (the section name or keyword — "
    "e.g. 'payments', 'statement', 'salary', 'productsAndServices', 'partner-services', 'other'). "
    "Use navigate when the user asks where to find something, how to open a section, "
    "or sends just a section name like 'Настройки' or 'Расчёты'.\n"
    "\n"
    "UNCLEAR REQUEST HANDLING (applies to ALL requests):\n"
    "• NEVER reset to welcome message after conversation started\n"
    "• NEVER say you do not understand\n"
    "• NEVER say 'Я — AI-ассистент СберБизнес' or any self-introduction\n"
    "• NEVER say 'Чем могу помочь?' or 'Чем я могу вам помочь?'\n"
    "• If request is unclear or has typos — try to find closest meaning\n"
    "• Ask ONE clarifying question in Russian\n"
    "• Suggest 2-3 most likely options\n"
    "• Set confidence to 'low' for unclear requests\n"
    "\n"
    "Response format for unclear requests:\n"
    "{\n"
    '  "intent": "unclear",\n'
    '  "action": null,\n'
    '  "parameters": {},\n'
    '  "requires_confirmation": false,\n'
    '  "confidence": "low",\n'
    '  "user_message": "Уточните пожалуйста: вы имеете в виду [вариант 1] или [вариант 2]?"\n'
    "}"
)

_ALLOWED_RESPONSE_FIELDS: frozenset[str] = frozenset(
    {"intent", "action", "parameters", "requires_confirmation", "user_message", "confidence"}
)
_FORBIDDEN_KEYS: frozenset[str] = frozenset(FORBIDDEN_FIELDS)
_MAX_USER_MESSAGE_LEN = 1000
_DEFAULT_MODEL = "gemini-2.0-flash"

_MD_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _load_genai():
    try:
        import google.generativeai as genai
    except ImportError as exc:
        raise ImportError(
            "google-generativeai не установлен. "
            "Выполните: pip install google-generativeai"
        ) from exc
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY не задан. "
            "Скопируйте .env.example в .env и укажите ключ."
        )
    genai.configure(api_key=api_key)
    return genai


def _strip_markdown(text: str) -> str:
    match = _MD_FENCE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _check_parameters_for_sensitive_keys(parameters: dict) -> None:
    for key in parameters:
        if key in _FORBIDDEN_KEYS:
            raise ValueError(
                f"Gemini вернул запрещённое поле в parameters: '{key}'. "
                "Ответ отклонён."
            )
        value = parameters[key]
        if isinstance(value, dict):
            _check_parameters_for_sensitive_keys(value)


def validate_gemini_response(raw: dict) -> GeminiResponse:
    if not isinstance(raw, dict):
        raise ValueError(f"Ответ Gemini должен быть объектом JSON, получено: {type(raw)}")

    clean: dict = {k: v for k, v in raw.items() if k in _ALLOWED_RESPONSE_FIELDS}

    # confidence is optional — default to "high"
    required = _ALLOWED_RESPONSE_FIELDS - {"confidence"}
    missing = required - clean.keys()
    if missing:
        raise ValueError(f"В ответе Gemini отсутствуют обязательные поля: {missing}")

    action = clean.get("action")
    if action is not None:
        if not isinstance(action, str):
            raise ValueError(f"Поле action должно быть строкой или null, получено: {type(action)}")
        if not validate_action(action):
            raise ValueError(
                f"Gemini вернул неразрешённое действие: '{action}'. "
                "Принимаются только действия из белого списка."
            )

    user_message = clean.get("user_message", "")
    if not isinstance(user_message, str):
        raise ValueError("Поле user_message должно быть строкой")
    if len(user_message) > _MAX_USER_MESSAGE_LEN:
        raise ValueError(
            f"user_message превышает лимит {_MAX_USER_MESSAGE_LEN} символов "
            f"(получено {len(user_message)})"
        )

    parameters = clean.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ValueError("Поле parameters должно быть объектом JSON")
    _check_parameters_for_sensitive_keys(parameters)

    raw_confidence = clean.get("confidence", "high")
    confidence = raw_confidence if raw_confidence in ("high", "medium", "low") else "high"

    return GeminiResponse(
        intent=str(clean.get("intent", "")),
        action=action,
        parameters=parameters,
        requires_confirmation=bool(clean.get("requires_confirmation", False)),
        user_message=user_message,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Fallback: keyword-based intent when Gemini is unavailable (429 quota)
# ---------------------------------------------------------------------------

_FALLBACK_INTENTS: dict[str, dict] = {
    "balance": {
        "intent": "check_balance",
        "action": "get_balance",
        "parameters": {},
        "requires_confirmation": False,
        "confidence": "high",
        "user_message": "Показываю информацию о балансе вашего счёта.",
    },
    "transaction": {
        "intent": "view_transactions",
        "action": "get_transactions",
        "parameters": {},
        "requires_confirmation": False,
        "confidence": "high",
        "user_message": "Вот последние операции по вашему счёту.",
    },
    "transfer": {
        "intent": "initiate_transfer",
        "action": None,
        "parameters": {},
        "requires_confirmation": False,
        "confidence": "medium",
        "user_message": (
            "Для выполнения перевода укажите сумму и получателя. "
            "Например: «переведи 5000 BYN получателю ООО Рога и Копыта»."
        ),
    },
    "report": {
        "intent": "create_report",
        "action": "create_report",
        "parameters": {},
        "requires_confirmation": False,
        "confidence": "high",
        "user_message": "Формирую отчёт по вашему запросу.",
    },
    "tariff": {
        "intent": "view_tariffs",
        "action": "get_tariffs",
        "parameters": {},
        "requires_confirmation": False,
        "confidence": "high",
        "user_message": "Показываю доступные тарифы.",
    },
    "requisite": {
        "intent": "view_requisites",
        "action": "get_requisites",
        "parameters": {},
        "requires_confirmation": False,
        "confidence": "high",
        "user_message": "Вот реквизиты вашей организации.",
    },
    "navigate": {
        "intent": "navigation_help",
        "action": "navigate",
        "parameters": {},
        "requires_confirmation": False,
        "confidence": "medium",
        "user_message": "Показываю разделы приложения СберБизнес.",
    },
    "counterparties": {
        "intent": "view_counterparties",
        "action": "get_counterparties",
        "parameters": {},
        "requires_confirmation": False,
        "confidence": "high",
        "user_message": "Показываю справочник ваших контрагентов.",
    },
    "assistant": {
        "intent": "general_assistant",
        "action": None,
        "parameters": {},
        "requires_confirmation": False,
        "confidence": "medium",
        "user_message": (
            "Сервис временно недоступен. Попробуйте повторить запрос через несколько минут."
        ),
    },
}

_FALLBACK_KEYWORDS: list[tuple[list[str], str]] = [
    # выписка must come before balance — "счёт" is too broad and causes false matches
    (["выписк", "transaction", "транзакц", "операци", "платеж", "история",
      "куда ушли", "на что потратил", "расход", "что поступило", "поступлени",
      "входящи", "исходящи", "последние платеж",
      # HIGH-03: colloquial, jargon, mixed lang
      "движени", "оборот", "обороты", "покажи поступлени",
      "самый большой перевод", "самый большой платёж", "самый большой платеж",
      "сколько потратил", "сколько заплатил", "account statement",
      "transaction history", "show transactions"], "transaction"),
    (["баланс", "остаток", "сколько", "balance", "сколько осталось",
      "сколько у меня", "деньги на счёт", "деньги на счет",
      # HIGH-03: colloquial and typos
      "покажи деньги", "деньги на счету", "деньги есть",
      "что со счёт", "что со счет", "show balance", "my balance",
      "check balance", "get balance", "балнас", "остатое"], "balance"),
    (["переведи", "перевед", "перевести", "отправить", "отправь", "transfer", "перевод",
      # HIGH-03: jargon and mixed lang
      "скинь", "скину", "скинуть", "плати", "заплати", "оплати", "перекинь",
      "слить деньги", "send money", "make transfer", "transfer money"], "transfer"),
    (["отчёт", "отчет", "report", "выгрузк"], "report"),
    (["тариф", "tariff", "стоимость", "цена", "план",
      # HIGH-03: colloquial tariff queries
      "помоги с тарифами", "расскажи про тарифы", "show tariffs", "my tariffs"], "tariff"),
    (["реквизит", "requisite", "бик", "инн", "кпп",
      # HIGH-03: colloquial and mixed lang
      "наши реквизиты", "реквизиты фирмы", "реквизиты компании",
      "банковские реквизиты", "bank details", "account details",
      "рекивизиты", "рекизиты"], "requisite"),
    (["контрагент", "получател", "counterpart", "справочник получател"], "counterparties"),
    (["где", "раздел", "найти", "открыть", "перейти", "навигац", "меню", "куда", "настройк"], "navigate"),
    (["объясни", "расскажи", "что такое", "как работает", "помоги разобраться",
      "помоги понять", "не понимаю", "непонятно", "объясните",
      # HIGH-03: extended assistant triggers
      "поясни", "расшифруй", "что означает", "что значит", "почему", "зачем",
      "в чём разница", "чем отличается", "расскажи подробнее",
      "помоги разобраться с", "explain", "what is", "how does"], "assistant"),
]


_SECTION_KEYWORDS: list[tuple[list[str], str]] = [
    (["выписк", "statement", "история", "операц", "транзакц"], "statement"),
    (["перевод", "платёж", "платеж", "расчёт", "расчет", "оплат", "payments", "контрагент"], "payments"),
    (["зарплат", "сотрудник", "salary", "ведомост"], "salary"),
    (["продукт", "услуг", "кредит", "депозит", "лизинг", "эквайр"], "productsAndServices"),
    (["партнёр", "бухгалтер", "юрид", "облак", "partner"], "partner-services"),
    (["настройк", "аккаунт", "профил", "уведомлен", "account"], "user-account"),
    (["справк", "прочее", "other", "письм", "валют"], "other"),
]

# Exact (or near-exact) section name → section_id mapping for direct navigation.
_EXACT_SECTION_NAMES: dict[str, str] = {
    "расчёты": "payments",
    "расчеты": "payments",
    "выписка": "statement",
    "зарплатный проект": "salary",
    "зарплатный": "salary",
    "продукты и услуги": "productsAndServices",
    "продукты": "productsAndServices",
    "сервисы партнёров": "partner-services",
    "сервисы партнеров": "partner-services",
    "сервисы": "partner-services",
    "настройки": "user-account",
    "прочее": "other",
    # English / technical IDs
    "payments": "payments",
    "statement": "statement",
    "salary": "salary",
    "productsandservices": "productsAndServices",
    "partner-services": "partner-services",
    "user-account": "user-account",
    "other": "other",
}

_SECTION_ID_TO_NAME: dict[str, str] = {
    "payments": "Расчёты",
    "statement": "Выписка",
    "salary": "Зарплатный проект",
    "productsAndServices": "Продукты и услуги",
    "partner-services": "Сервисы партнёров",
    "user-account": "Настройки",
    "other": "Прочее",
}

# ---------------------------------------------------------------------------
# HIGH-03: Multi-layer normalization pipeline
# Order: mixed language → typo correction → colloquial expansion
# ---------------------------------------------------------------------------

# Common English banking terms used in Russian context
_MIXED_LANG_MAP: list[tuple[str, str]] = [
    ("show me balance", "баланс"),
    ("show balance", "баланс"),
    ("check balance", "баланс"),
    ("get balance", "баланс"),
    ("my balance", "баланс"),
    ("transaction history", "история операции"),
    ("account statement", "история операции"),
    ("show transactions", "история операции"),
    ("make transfer", "переведи"),
    ("transfer money", "переведи"),
    ("send money", "переведи"),
    ("bank details", "реквизиты"),
    ("account details", "реквизиты"),
    ("show tariffs", "тарифы"),
    ("my tariffs", "тарифы"),
    ("requisites", "реквизиты"),
    ("statement", "выписка"),
    ("balance", "баланс"),          # bare English word in Russian text
]

# Common Russian banking word typos (keyboard-adjacent and phonetic)
_TYPO_MAP: list[tuple[str, str]] = [
    ("балнас", "баланс"),
    ("валанс", "баланс"),
    ("остатое", "остаток"),
    ("выписика", "выписка"),
    ("выписики", "выписка"),
    ("перевди", "переведи"),
    ("рекивизиты", "реквизиты"),
    ("рекизиты", "реквизиты"),
    ("рекивзиты", "реквизиты"),
    ("реквзиты", "реквизиты"),
    ("тарифи", "тарифы"),
    ("опрерации", "операции"),
    ("транзации", "транзакции"),
]

# Colloquial phrase normalization before keyword matching
_COLLOQUIAL_MAP: list[tuple[str, str]] = [
    # Balance — colloquial and indirect
    ("куда ушли деньги", "последние операции расходы"),
    ("сколько осталось", "баланс остаток"),
    ("что поступило", "входящие поступления"),
    ("на что потратил", "расходы операции"),
    ("помоги разобраться", "объясни помоги"),
    ("не понимаю", "объясни помоги"),
    ("что со счётом", "баланс остаток"),
    ("что со счетом", "баланс остаток"),
    ("что происходит со счётом", "история операции"),
    ("что происходит со счетом", "история операции"),
    ("сколько денег", "баланс остаток"),
    ("сколько у меня денег", "баланс остаток"),
    ("покажи деньги", "баланс остаток"),
    ("деньги на счету", "баланс остаток"),
    ("деньги есть", "баланс остаток"),
    # Transactions — colloquial
    ("покажи поступления", "входящие поступления"),
    ("движения по счёту", "история операции"),
    ("движения по счету", "история операции"),
    ("последние движения", "история операции"),
    ("самый большой перевод", "история операции"),
    ("самый большой платёж", "история операции"),
    ("самый большой платеж", "история операции"),
    ("какой перевод был самым большим", "история операции"),
    ("сколько потратил", "расходы операции"),
    ("сколько заплатил", "расходы операции"),
    ("обороты по счёту", "история операции"),
    ("обороты по счету", "история операции"),
    # Transfer — jargon
    ("скинь деньги", "перевод переведи"),
    ("скинуть деньги", "перевод переведи"),
    ("отправь деньги", "перевод переведи"),
    ("отправить деньги", "перевод переведи"),
    ("слить деньги", "перевод переведи"),
    # Requisites — colloquial
    ("наши реквизиты", "реквизиты"),
    ("реквизиты фирмы", "реквизиты"),
    ("реквизиты компании", "реквизиты"),
    ("банковские реквизиты", "реквизиты"),
    ("счёт фирмы", "реквизиты"),
    ("счет фирмы", "реквизиты"),
    # Tariffs — colloquial
    ("помоги с тарифами", "тарифы"),
    ("расскажи про тарифы", "тарифы объясни"),
    ("помоги разобраться с тарифами", "тарифы объясни"),
]


def _normalize_colloquial(message: str) -> str:
    """Apply only colloquial phrase normalization (kept for backward compatibility)."""
    msg = message.lower()
    for phrase, replacement in _COLLOQUIAL_MAP:
        if phrase in msg:
            msg = msg.replace(phrase, replacement)
    return msg


def _normalize_message(message: str) -> str:
    """Full normalization pipeline: mixed language → typos → colloquial phrases.

    Used by _fallback_response for maximum NLU coverage in degraded mode.
    Returns a lowercase normalized string ready for keyword matching.
    """
    msg = message.lower().strip()
    for en_phrase, ru_phrase in _MIXED_LANG_MAP:
        if en_phrase in msg:
            msg = msg.replace(en_phrase, ru_phrase)
    for typo, correct in _TYPO_MAP:
        if typo in msg:
            msg = msg.replace(typo, correct)
    for phrase, replacement in _COLLOQUIAL_MAP:
        if phrase in msg:
            msg = msg.replace(phrase, replacement)
    return msg


def _extract_section(message: str) -> str:
    msg_lower = message.lower()
    for keywords, section_id in _SECTION_KEYWORDS:
        if any(kw in msg_lower for kw in keywords):
            return section_id
    return ""


_AMOUNT_RE = re.compile(
    r"(?:сумм[уе\s]+|на\s+|в\s+размере\s*)"
    r"([\d\s]+(?:[.,]\d+)?)\s*(?:byn|бyn|бел\.?\s*руб|руб\.?)?",
    re.IGNORECASE,
)
_AMOUNT_BARE_RE = re.compile(r"\b(\d[\d\s]*(?:[.,]\d+)?)\s*(?:byn|бyn|бел\.?\s*руб|руб\.?)?\b", re.IGNORECASE)
_RECIPIENT_RE = re.compile(
    r"(?:получател[юьи]+|на счёт|на счет|для|компани[иейя]+)\s+(.+?)(?:\s*$|\s+на\s+|\s+сумм)",
    re.IGNORECASE,
)
_LEGAL_ENTITY_BARE_RE = re.compile(
    r"\b((?:СООО|ИООО|ООО|ОАО|ЗАО|ЧУП|ЧТУП|РУП|КУП|МУП|УП|ГП|ИП|ТОО|ТДА)"
    r"(?:\s+(?:«[^»]*»|[\w\-\.]+))+)\s*$",
    re.IGNORECASE | re.UNICODE,
)


def _parse_transfer_params(message: str) -> dict:
    amount: float | None = None
    recipient: str | None = None

    m = _AMOUNT_RE.search(message)
    if not m:
        m = _AMOUNT_BARE_RE.search(message)
    if m:
        raw = m.group(1).replace(" ", "").replace(",", ".")
        try:
            amount = float(raw)
        except ValueError:
            pass

    r = _RECIPIENT_RE.search(message)
    if r:
        recipient = r.group(1).strip()

    if not recipient:
        r = _LEGAL_ENTITY_BARE_RE.search(message)
        if r:
            recipient = r.group(1).strip()

    return {"amount": amount, "recipient": recipient}


def _fallback_response(message: str) -> GeminiResponse:
    # HIGH-03: full normalization pipeline (mixed lang → typos → colloquial)
    msg_lower = _normalize_message(message.lower().strip())

    # Direct section name navigation
    section_id = _EXACT_SECTION_NAMES.get(msg_lower)
    if section_id:
        section_name = _SECTION_ID_TO_NAME.get(section_id, section_id)
        return GeminiResponse(
            intent="navigation_help",
            action="navigate",
            parameters={"section": section_id},
            requires_confirmation=False,
            confidence="high",
            user_message=f"Перехожу в раздел «{section_name}».",
        )

    for keywords, intent_key in _FALLBACK_KEYWORDS:
        if any(kw in msg_lower for kw in keywords):
            if intent_key == "navigate":
                section = _extract_section(message)
                return GeminiResponse(
                    intent="navigation_help",
                    action="navigate",
                    parameters={"section": section},
                    requires_confirmation=False,
                    confidence="medium",
                    user_message="Показываю разделы приложения СберБизнес.",
                )
            if intent_key == "assistant":
                return GeminiResponse(
                    intent="general_assistant",
                    action=None,
                    parameters={},
                    requires_confirmation=False,
                    confidence="medium",
                    user_message=(
                        "Сервис временно недоступен для ответа на общие вопросы. "
                        "Попробуйте повторить запрос через несколько минут."
                    ),
                )
            if intent_key == "transfer":
                params = _parse_transfer_params(message)
                amount = params.get("amount")
                recipient = params.get("recipient")
                if amount and amount > 0 and recipient:
                    return GeminiResponse(
                        intent="initiate_transfer",
                        action="initiate_transfer",
                        parameters={"amount": amount, "recipient": recipient},
                        requires_confirmation=False,
                        confidence="high",
                        user_message=f"Инициирую перевод на сумму {amount} BYN получателю {recipient}.",
                    )
                missing = []
                if not (amount and amount > 0):
                    missing.append("сумму")
                if not recipient:
                    missing.append("получателя")
                return GeminiResponse(
                    intent="initiate_transfer",
                    action=None,
                    parameters={},
                    requires_confirmation=False,
                    confidence="low",
                    user_message=(
                        f"Для выполнения перевода укажите {' и '.join(missing)}. "
                        "Например: «переведи 5000 BYN получателю ООО Рога и Копыта»."
                    ),
                )
            base = dict(_FALLBACK_INTENTS[intent_key])
            return GeminiResponse(**base)

    return GeminiResponse(
        intent="general_question",
        action=None,
        parameters={},
        requires_confirmation=False,
        # LOW confidence — unrecognized input should increase friction, not decrease it.
        confidence="low",
        user_message=(
            "Уточните, пожалуйста, что вас интересует. "
            "Я помогу с балансом счёта, историей операций, "
            "переводом контрагенту, тарифами, реквизитами компании "
            "или навигацией по разделам СберБизнес."
        ),
    )


# Navigation phrases that Gemini 2.0 Flash consistently misroutes to get_transactions.
_NAV_TRIGGER_PHRASES: list[str] = [
    "где найти", "где находится", "где смотреть",
    "куда нажать", "куда перейти", "куда идти",
    "как открыть", "как перейти", "как найти",
    "в каком разделе", "в каком месте",
]

_AMOUNT_QUICK_RE = re.compile(r'\d[\d\s.,]*(?:byn|руб|bел)?', re.IGNORECASE)
_LEGAL_FORM_QUICK_RE = re.compile(
    r'\b(?:ООО|ИП|ЗАО|ОАО|ЧУП|ЧТУП|РУП|КУП|СООО|ИООО|ГП|УП|ТОО|ТДА)\b',
    re.IGNORECASE,
)


def _has_transfer_intent(message: str) -> bool:
    return bool(_AMOUNT_QUICK_RE.search(message) and _LEGAL_FORM_QUICK_RE.search(message))


def call_gemini(
    clean_message: str,
    context_hint: str = "",
    mode: str = "banking",
) -> GeminiResponse:
    """Call Gemini for intent extraction (banking) or informational response (assistant).

    mode="assistant" — backend guarantees action=None and parameters={} in the
    returned GeminiResponse regardless of what Gemini generates.  This is the
    Zero-Trust enforcement boundary: the LLM output is untrusted, so the action
    field is discarded at parse time before any validation runs.
    """
    import logging as _logging
    if not isinstance(clean_message, str) or not clean_message.strip():
        raise ValueError("clean_message не может быть пустым")

    # In banking mode: pre-empt Gemini for known navigation phrases so we don't
    # waste a round-trip on queries that are unambiguously navigation requests.
    if mode == "banking":
        msg_lower = clean_message.lower()
        if any(phrase in msg_lower for phrase in _NAV_TRIGGER_PHRASES):
            if not _has_transfer_intent(clean_message):
                section = _extract_section(clean_message)
                return GeminiResponse(
                    intent="navigation_help",
                    action="navigate",
                    parameters={"section": section},
                    requires_confirmation=False,
                    confidence="high",
                    user_message="Показываю разделы приложения СберБизнес.",
                )

    genai = _load_genai()
    model_name = os.getenv("GEMINI_MODEL", _DEFAULT_MODEL)

    # Build prompt: context hint first, then mode hint, then user message.
    prompt = clean_message
    if context_hint:
        prompt = f"[Контекст предыдущего запроса: {context_hint}]\n\n{clean_message}"
    if mode == "assistant":
        # Strong mode hint — Gemini must answer the question directly with knowledge.
        # action is always null in assistant mode (enforced by backend regardless).
        prompt = (
            "[РЕЖИМ АССИСТЕНТА: отвечай на вопрос прямо и содержательно, "
            "как образованный финансовый консультант. "
            "action ВСЕГДА null, parameters ВСЕГДА {}.]\n\n" + prompt
        )

    try:
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=SYSTEM_PROMPT,
        )
        response = model.generate_content(prompt)
    except Exception as exc:
        err_str = str(exc)
        if any(s in err_str for s in ("429", "RESOURCE_EXHAUSTED", "quota", "401", "403", "UNAUTHENTICATED", "PERMISSION_DENIED")):
            _logging.getLogger("sberik.security").warning(
                "GEMINI_FALLBACK_ACTIVATED reason=%s", err_str[:120]
            )
            fallback = _fallback_response(clean_message)
            if mode == "assistant":
                # Fallback in assistant mode: keep user_message but discard action
                return GeminiResponse(
                    intent=fallback.intent,
                    action=None,
                    parameters={},
                    requires_confirmation=False,
                    confidence=fallback.confidence,
                    user_message=fallback.user_message,
                )
            return fallback
        raise RuntimeError(f"Ошибка при обращении к Gemini API: {exc}") from exc

    try:
        raw_text = response.text
    except Exception as exc:
        raise RuntimeError(f"Gemini вернул пустой или заблокированный ответ: {exc}") from exc

    json_text = _strip_markdown(raw_text)

    try:
        raw_dict = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Gemini вернул невалидный JSON: {exc}\n"
            f"Исходный ответ: {raw_text[:300]}"
        ) from exc

    # ── ASSISTANT MODE BACKEND GUARANTEE ─────────────────────────────────────
    # Regardless of what Gemini returned, discard action and parameters before
    # validation.  This ensures:
    #  • The whitelist is NEVER reached in assistant mode.
    #  • The risk engine is NEVER called in assistant mode.
    #  • The executor is NEVER called in assistant mode.
    #  • A Gemini hallucination (invalid action) cannot cause a 500 error.
    if mode == "assistant":
        discarded_action = raw_dict.get("action")
        if discarded_action:
            _logging.getLogger("sberik.security").info(
                "ASSISTANT_MODE_ACTION_DISCARDED action=%s", discarded_action
            )
        raw_dict["action"] = None
        raw_dict["parameters"] = {}

    return validate_gemini_response(raw_dict)
