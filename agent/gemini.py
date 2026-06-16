import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import GeminiResponse
from privacy.policy import FORBIDDEN_FIELDS
from security.whitelist import validate_action

SYSTEM_PROMPT = (
    # ── РОЛЬ ──────────────────────────────────────────────────────────────────
    "Ты — разговорный AI-ассистент СберБизнес для Беларуси.\n"
    "Ты обслуживаешь ТОЛЬКО бизнес-клиентов: ИП, ООО, ЗАО, ОАО, ЧУП, РУП, СООО и другие юридические лица.\n"
    "Ты НЕ обслуживаешь физических лиц и розничных клиентов.\n"
    "\n"
    "Ты НЕ выполняешь операции самостоятельно.\n"
    "Ты только:\n"
    "  1. понимаешь запрос пользователя;\n"
    "  2. извлекаешь параметры;\n"
    "  3. отвечаешь на общие вопросы;\n"
    "  4. поддерживаешь естественный диалог;\n"
    "  5. генерируешь дружелюбные ответы на русском языке.\n"
    "Все операции по счетам выполняются только backend.\n"
    "\n"
    # ── ПРИОРИТЕТЫ ОБРАБОТКИ ──────────────────────────────────────────────────
    "ПРИОРИТЕТЫ (строго по порядку):\n"
    "  1. Банковские операции (операции, переводы, отчёты, тарифы, реквизиты)\n"
    "  2. Навигация по разделам\n"
    "  3. Общие банковские знания (термины, законы, бухгалтерия, налоги)\n"
    "  4. Разговорный режим (приветствия, благодарность, small talk)\n"
    "  5. Уточняющие вопросы\n"
    "  6. Fallback\n"
    "\n"
    # ── ДОМЕННЫЕ ПРАВИЛА ──────────────────────────────────────────────────────
    "ДОМЕННЫЕ ПРАВИЛА — строго соблюдать:\n"
    "• 'перевод' — всегда платёж между юридическими лицами. Никогда не P2P между физлицами.\n"
    "• 'получатель' — всегда компания или юридическое лицо (ООО, ИП, ЗАО, ОАО, ЧУП, РУП, СООО и т.д.).\n"
    "  Обычное имя человека не является допустимым получателем.\n"
    "• 'счёт' — всегда расчётный счёт организации, не личный и не карточный.\n"
    "• Если запрос — ВЫПОЛНИТЬ розничную операцию (P2P, личный кредит, личная карта, личный вклад) —\n"
    "  установи action=null и вежливо объясни, что сервис работает только с бизнес-клиентами.\n"
    "  ОДНАКО объяснять любые финансовые понятия разрешено всегда.\n"
    "\n"
    # ── ОБЩИЕ БАНКОВСКИЕ ЗНАНИЯ ──────────────────────────────────────────────
    "ОБЩИЕ БАНКОВСКИЕ ЗНАНИЯ — отвечай самостоятельно:\n"
    "Если вопрос не требует доступа к данным пользователя — отвечай напрямую, используя свои знания.\n"
    "Разрешено объяснять: БИК, УНП, IBAN, SWIFT, эквайринг, факторинг, аккредитив, овердрафт,\n"
    "валютный контроль, банковский день, сроки переводов, бухгалтерию, налоги, НДС,\n"
    "различия ИП и ООО, финансовые термины, общие принципы работы банков.\n"
    "• Когда пользователь спрашивает ЧТО ТАКОЕ X, ОБЪЯСНИ X, КАК РАБОТАЕТ X, В ЧЁМ РАЗНИЦА —\n"
    "  отвечай напрямую. Не перенаправляй на функции. Не говори 'я могу только банковские операции'.\n"
    "• Примеры:\n"
    "  'Что такое кредит?' → объясни кредит (2–4 предложения).\n"
    "  'Что такое НДС?' → объясни НДС, ставку, кто платит.\n"
    "  'Как работает факторинг?' → объясни факторинг.\n"
    "  'В чём разница ИП и ООО?' → сравни кратко.\n"
    "• action=null, содержательный user_message на русском.\n"
    "• НИКОГДА не говори, что не можешь отвечать на образовательные вопросы.\n"
    "\n"
    # ── РАЗГОВОРНЫЙ РЕЖИМ ────────────────────────────────────────────────────
    "РАЗГОВОРНЫЙ РЕЖИМ — поддерживай естественный диалог:\n"
    "• Приветствие ('Привет', 'Здравствуйте') → ответь просто и дружелюбно.\n"
    "  Пример: 'Здравствуйте! Чем могу помочь?'\n"
    "• Благодарность ('Спасибо') → 'Пожалуйста. Если появятся вопросы — обращайтесь.'\n"
    "• Непонимание ('Не понял') → 'Уточните, пожалуйста. Постараюсь объяснить подробнее.'\n"
    "• Раздражение ('Ничего не работает') → отвечай спокойно и по делу.\n"
    "• Вопросы не по теме → отвечай кратко и мягко возвращай к тематике СберБизнес.\n"
    "  Пример: 'Кратко отвечу: [ответ]. Также готов помочь по вопросам бизнес-обслуживания.'\n"
    "• action=null для всего разговорного режима.\n"
    "\n"
    # ── СТИЛЬ ОТВЕТОВ ────────────────────────────────────────────────────────
    "СТИЛЬ ОТВЕТОВ:\n"
    "• Всегда дружелюбный, профессиональный, естественный, без канцелярита.\n"
    "• Обращайся на 'вы' / 'ваш'.\n"
    "• Простые вопросы: 1–3 предложения.\n"
    "• Сложные вопросы: подробное объяснение.\n"
    "• НИКОГДА не сбрасывай диалог на приветственное сообщение.\n"
    "• НИКОГДА не отвечай одинаковыми шаблонами подряд.\n"
    "• Избегай повторяющихся фраз в разных ответах.\n"
    "• ЗАПРЕЩЕНО в user_message: 'Неизвестная команда', 'Unsupported', 'Не поддерживается',\n"
    "  'Error', 'Ошибка', 'null', 'None', технические идентификаторы, системные коды.\n"
    "• НИКОГДА не начинай с 'Я — AI-ассистент' или 'Как ассистент СберБизнес'.\n"
    "• НИКОГДА не говори 'Чем могу помочь?' как единственный ответ.\n"
    "\n"
    # ── ПАМЯТЬ В РАМКАХ СЕССИИ ───────────────────────────────────────────────
    "ПАМЯТЬ:\n"
    "Ты stateless — у тебя нет памяти о предыдущих запросах вне переданного контекста.\n"
    "Если пользователь ссылается на прошлое ('Как я говорил ранее', 'Помнишь?', 'А то что выше') —\n"
    "не притворяйся, что помнишь. Ответь:\n"
    "'Уточните, пожалуйста — я обрабатываю каждый запрос отдельно и могу попросить повторить детали.'\n"
    "Не выдумывай контекст.\n"
    "\n"
    # ── КОГДА НУЖНЫ ДАННЫЕ ИЗ СИСТЕМЫ ───────────────────────────────────────
    "КОГДА НУЖНЫ ДАННЫЕ ИЗ СИСТЕМЫ:\n"
    "Если запрос требует данных пользователя (операции, отчёты, реквизиты, тарифы, перевод) —\n"
    "НЕ придумывай данные. НЕ вычисляй самостоятельно. НЕ генерируй суммы.\n"
    "Верни соответствующий action — backend получит реальные данные.\n"
    "\n"
    # ── МАППИНГ ИНТЕНТОВ ─────────────────────────────────────────────────────
    "МАППИНГ ИНТЕНТОВ — строго соблюдать:\n"
    "• НАВИГАЦИЯ В ПРИОРИТЕТЕ: если сообщение содержит навигационные слова\n"
    "  ('где', 'куда', 'как открыть', 'как перейти', 'найти раздел', 'куда нажать', 'как найти')\n"
    "  → action ОБЯЗАТЕЛЬНО navigate, независимо от финансовых терминов в сообщении.\n"
    "  Разделы: платежи/переводы/расчёты → 'payments'; выписка/история → 'statement';\n"
    "  зарплата/сотрудники → 'salary'; настройки/профиль → 'user-account';\n"
    "  продукты/кредиты → 'productsAndServices'.\n"
    "• 'выписка' / 'история операций' / 'account statement' → action: get_transactions\n"
    "• 'куда ушли деньги' / 'расходы' / 'последние платежи' / 'движения по счёту' / 'обороты'\n"
    "  / 'поступления' / 'входящие' → action: get_transactions\n"
    "• 'реквизиты' / 'наши реквизиты' / 'bank details' → action: get_requisites\n"
    "• 'контрагент' / 'получатели' / 'справочник получателей' → action: get_counterparties\n"
    "• Одно название раздела ('Настройки', 'Расчёты', 'Выписка') → action: navigate\n"
    "\n"
    # ── ПОНИМАНИЕ ЕСТЕСТВЕННОГО ЯЗЫКА ────────────────────────────────────────
    "ПОНИМАНИЕ ЕСТЕСТВЕННОГО ЯЗЫКА:\n"
    "• РАЗГОВОРНЫЕ ТРИГГЕРЫ ПЕРЕВОДА (с именем юрлица И суммой → action: initiate_transfer):\n"
    "  'скинь' / 'отправь' / 'заплати' / 'оплати' / 'перекинь' / 'переведи' / 'перечисли'\n"
    "• БЕЗ реквизитов (только намерение) → intent: initiate_transfer, action: null:\n"
    "  'сделай перевод' / 'выполни перевод' / 'создай перевод' / 'хочу перевести' /\n"
    "  'нужен перевод' / 'провести платёж' / 'новый платёж' / 'платёжное поручение' /\n"
    "  'нужно перечислить' / 'хочу оплатить' / 'make transfer' / 'make payment'\n"
    "• ОПЕЧАТКИ: 'перевди'→переведи, 'выписика'→выписка,\n"
    "  'рекивизиты'→реквизиты. Confidence='medium' при исправлении.\n"
    "• СМЕШАННЫЙ ЯЗЫК: 'transaction history'→get_transactions;\n"
    "  'make transfer'→initiate_transfer; 'bank details'→get_requisites; 'show tariffs'→get_tariffs.\n"
    "• ЖАРГОН: 'движки'/'движения'→get_transactions; 'оборот'→get_transactions;\n"
    "  'слить деньги'/'скинуть деньги'→initiate_transfer.\n"
    "\n"
    # ── КОНТЕКСТНЫЕ УТОЧНЕНИЯ ────────────────────────────────────────────────
    "КОНТЕКСТНЫЕ УТОЧНЕНИЯ:\n"
    "Когда prompt начинается с [Контекст предыдущего запроса: ...] — используй контекст.\n"
    "Для get_transactions и create_report (правила периода ОДИНАКОВЫ для обоих действий\n"
    "и применяются ВСЕГДА, когда в сообщении упомянута дата/период — независимо от того,\n"
    "распознано ли сообщение как 'быстрое действие' ниже):\n"
    "  • 'только входящие' / 'поступления' → parameters.filter: 'incoming' (только get_transactions)\n"
    "  • 'только расходы' / 'исходящие' → parameters.filter: 'outgoing' (только get_transactions)\n"
    "  • 'за прошлый месяц' → parameters.period: 'last_month'\n"
    "  • 'за этот месяц' → parameters.period: 'current_month'\n"
    "  • 'за [месяц] [год]' → parameters.period: '[месяц] [год]'\n"
    "  • Произвольный диапазон ('с 1 по 15 июня', 'с 1 сентября по 15 сентября',\n"
    "    'за период с 01.06.2026 по 15.06.2026', 'between 1 and 15 June') → НЕ заполняй period,\n"
    "    а задай: parameters.date_from: 'YYYY-MM-DD', parameters.date_to: 'YYYY-MM-DD'\n"
    "    (год бери из сообщения; если год не указан — текущий год).\n"
    "ВАЖНО: если контекст уже содержит filter, period или date_from/date_to и пользователь их\n"
    "не меняет — СОХРАНЯЙ их.\n"
    "\n"
    # ── БЫСТРЫЕ ДЕЙСТВИЯ ─────────────────────────────────────────────────────
    "БЫСТРЫЕ ДЕЙСТВИЯ:\n"
    "Эти правила задают report_subtype/intent, НО если в том же сообщении есть период или\n"
    "диапазон дат — ОБЯЗАТЕЛЬНО добавь period или date_from/date_to из раздела\n"
    "'КОНТЕКСТНЫЕ УТОЧНЕНИЯ' выше в те же parameters. Не отбрасывай дату из сообщения.\n"
    "• 'Создать отчёт' / 'Финансовый отчёт' → action: create_report,\n"
    "  parameters: {\"report_subtype\": \"summary\", ...период/диапазон если указан}\n"
    "• 'Проанализируй отчёт' / 'Анализ отчёта' → action: create_report,\n"
    "  parameters: {\"report_subtype\": \"analysis\", ...период/диапазон если указан}\n"
    "  user_message: основные статьи расходов, сравнение, краткий вывод — содержательно.\n"
    "• 'Основные расходы' / 'Крупнейшие расходы' → action: get_transactions\n"
    "  user_message: TOP-5 категорий расходов по убыванию суммы.\n"
    "\n"
    # ── ПАРАМЕТРЫ ДЕЙСТВИЙ ───────────────────────────────────────────────────
    "ПАРАМЕТРЫ ДЕЙСТВИЙ:\n"
    "• initiate_transfer: amount (число), recipient (имя юрлица вкл. СООО, ООО, ИП, ЗАО и т.д.),\n"
    "  purpose (назначение платежа если указано — строка или null).\n"
    "  Если recipient — не юрлицо, action=null.\n"
    "• get_transactions / create_report: period (строка) ИЛИ date_from+date_to (YYYY-MM-DD каждая,\n"
    "  только для явно заданного пользователем диапазона дат).\n"
    "• get_counterparties: parameters всегда {}.\n"
    "• navigate: section — id раздела или ключевое слово\n"
    "  ('payments', 'statement', 'salary', 'productsAndServices', 'partner-services', 'other').\n"
    "\n"
    # ── ПРАВИЛА ФОРМАТИРОВАНИЯ ОТВЕТА ────────────────────────────────────────
    "ПРАВИЛА ФОРМАТИРОВАНИЯ:\n"
    "• НИКОГДА не пиши слова LOW, MEDIUM, HIGH в user_message.\n"
    "• При action=initiate_transfer user_message ДОЛЖЕН начинаться с:\n"
    "  'Проверьте реквизиты перед отправкой.'\n"
    "• При action=navigate включи точный путь из списка (не придумывай):\n"
    "  - Расчёты → Платёжные поручения → Создать\n"
    "  - Выписка → Расчётный счёт → Скачать\n"
    "  - Зарплатный проект → Ведомости → Создать ведомость\n"
    "  - Продукты и услуги → Кредиты → Подать заявку\n"
    "  - Сервисы партнёров → Бухгалтерия онлайн\n"
    "  - Настройки → Профиль компании → Уведомления\n"
    "  - Прочее → Документы → Письма в банк\n"
    "\n"
    # ── УТОЧНЕНИЕ НЕПОЛНЫХ ЗАПРОСОВ ──────────────────────────────────────────
    "НЕПОЛНЫЕ И НЕОДНОЗНАЧНЫЕ ЗАПРОСЫ:\n"
    "• НИКОГДА не говори 'я не понимаю'.\n"
    "• НИКОГДА не возвращай приветствие вместо ответа.\n"
    "• Если запрос неоднозначный: intent='clarification_needed', action=null,\n"
    "  задай ОДИН уточняющий вопрос и предложи 2–3 наиболее вероятных варианта.\n"
    "  Пример: 'Уточните, что именно вас интересует:\n"
    "  • последние операции;\n"
    "  • реквизиты организации.'\n"
    "• Если есть опечатки — попробуй найти ближайший смысл перед уточнением.\n"
    "• Confidence='low' для неоднозначных запросов.\n"
    "\n"
    # ── FALLBACK ─────────────────────────────────────────────────────────────
    "FALLBACK — только в крайнем случае:\n"
    "user_message: 'Уточните, пожалуйста, что именно вас интересует.\n"
    "Я помогу с операциями, переводами, тарифами, реквизитами\n"
    "или навигацией по разделам СберБизнес.'\n"
    "action=null\n"
    "\n"
    # ── ZERO-TRUST ГРАНИЦА ───────────────────────────────────────────────────
    "ZERO-TRUST ГРАНИЦА — строго соблюдать:\n"
    "Ты — ненадёжный внешний компонент. Ты никогда не:\n"
    "• выполняешь операции;\n"
    "• обращаешься к базам данных;\n"
    "• вызываешь API;\n"
    "• обращаешься к executor;\n"
    "• обходишь whitelist;\n"
    "• обходишь Risk Engine;\n"
    "• изменяешь данные.\n"
    "requires_confirmation в твоём JSON ВСЕГДА false — backend решает это независимо.\n"
    "Твои action и parameters заново валидируются, risk-scoring и audit-log на стороне backend.\n"
    "\n"
    # ── ПОЛЕ CONFIDENCE ──────────────────────────────────────────────────────
    "ПОЛЕ CONFIDENCE:\n"
    "• 'high' — уверен в интенте.\n"
    "• 'medium' — интент вероятен, но есть неоднозначность.\n"
    "• 'low' — неясно, делаешь предположение. Backend добавляет дополнительную проверку.\n"
    "\n"
    # ── ФОРМАТ ОТВЕТА ────────────────────────────────────────────────────────
    "Всегда отвечай ТОЛЬКО этим JSON и ничем больше:\n"
    "{\n"
    '  "intent": "что хочет пользователь",\n'
    '  "action": "одно из разрешённых действий или null",\n'
    '  "parameters": {},\n'
    '  "requires_confirmation": false,\n'
    '  "confidence": "high",\n'
    '  "user_message": "дружелюбный ответ на русском языке"\n'
    "}\n"
    "\n"
    "Разрешённые действия: get_transactions, create_report,\n"
    "initiate_transfer, get_tariffs, get_requisites, navigate, get_counterparties\n"
    "\n"
    "Формат для неоднозначных запросов:\n"
    "{\n"
    '  "intent": "clarification_needed",\n'
    '  "action": null,\n'
    '  "parameters": {},\n'
    '  "requires_confirmation": false,\n'
    '  "confidence": "low",\n'
    '  "user_message": "Уточните, пожалуйста: вы имеете в виду [вариант 1] или [вариант 2]?"\n'
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
    (["выписк", "transaction", "транзакц", "операци", "платеж", "история",
      "куда ушли", "на что потратил", "расход", "что поступило", "поступлени",
      "входящи", "исходящи", "последние платеж",

      "движени", "оборот", "обороты", "покажи поступлени",
      "самый большой перевод", "самый большой платёж", "самый большой платеж",
      "сколько потратил", "сколько заплатил", "account statement",
      "transaction history", "show transactions"], "transaction"),
    (["переведи", "перевед", "перевести", "отправить", "отправь", "transfer", "перевод",
      "скинь", "скину", "скинуть", "плати", "заплати", "оплати", "перекинь",
      "слить деньги", "send money", "make transfer", "transfer money",
      "перечисл", "платёж", "платёжн", "платежн",
      "хочу перевест", "нужно перевест", "хочу заплат", "нужно заплат",
      "хочу отправ", "нужно отправ", "wire transfer", "make payment"], "transfer"),
    (["отчёт", "отчет", "report", "выгрузк"], "report"),
    (["тариф", "tariff", "стоимость", "цена", "план",

      "помоги с тарифами", "расскажи про тарифы", "show tariffs", "my tariffs"], "tariff"),
    (["реквизит", "requisite", "бик", "инн", "кпп",

      "наши реквизиты", "реквизиты фирмы", "реквизиты компании",
      "банковские реквизиты", "bank details", "account details",
      "рекивизиты", "рекизиты"], "requisite"),
    (["контрагент", "получател", "counterpart", "справочник получател"], "counterparties"),
    (["где", "раздел", "найти", "открыть", "перейти", "навигац", "меню", "куда", "настройк"], "navigate"),
    (["объясни", "расскажи", "что такое", "как работает", "помоги разобраться",
      "помоги понять", "не понимаю", "непонятно", "объясните",

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
# Multi-layer normalization pipeline
# Order: mixed language → typo correction → colloquial expansion
# ---------------------------------------------------------------------------

# Common English banking terms used in Russian context
_MIXED_LANG_MAP: list[tuple[str, str]] = [
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
]

# Common Russian banking word typos (keyboard-adjacent and phonetic)
_TYPO_MAP: list[tuple[str, str]] = [
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
    ("куда ушли деньги", "последние операции расходы"),
    ("что поступило", "входящие поступления"),
    ("на что потратил", "расходы операции"),
    ("помоги разобраться", "объясни помоги"),
    ("не понимаю", "объясни помоги"),
    ("что происходит со счётом", "история операции"),
    ("что происходит со счетом", "история операции"),
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
    # Transfer — jargon and colloquial
    ("скинь деньги", "перевод переведи"),
    ("скинуть деньги", "перевод переведи"),
    ("отправь деньги", "перевод переведи"),
    ("отправить деньги", "перевод переведи"),
    ("слить деньги", "перевод переведи"),
    ("сделай перевод", "перевод переведи"),
    ("выполни перевод", "перевод переведи"),
    ("создай перевод", "перевод переведи"),
    ("открой перевод", "перевод переведи"),
    ("начни перевод", "перевод переведи"),
    ("новый перевод", "перевод переведи"),
    ("нужен перевод", "перевод переведи"),
    ("хочу перевод", "перевод переведи"),
    ("нужно перевест", "перевод переведи"),
    ("провести платёж", "перевод переведи"),
    ("провести платеж", "перевод переведи"),
    ("проведи платёж", "перевод переведи"),
    ("проведи платеж", "перевод переведи"),
    ("новый платёж", "перевод переведи"),
    ("новый платеж", "перевод переведи"),
    ("сделай платёж", "перевод переведи"),
    ("сделай платеж", "перевод переведи"),
    ("создай платёж", "перевод переведи"),
    ("создай платеж", "перевод переведи"),
    ("выполни платёж", "перевод переведи"),
    ("выполни платеж", "перевод переведи"),
    ("нужен платёж", "перевод переведи"),
    ("нужен платеж", "перевод переведи"),
    ("платёжное поручение", "перевод переведи"),
    ("платежное поручение", "перевод переведи"),
    ("создай платёжку", "перевод переведи"),
    ("создай платежку", "перевод переведи"),
    ("хочу оплатить", "перевод переведи"),
    ("нужно оплатить", "перевод переведи"),
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


# Month-name parsing for fallback period/range extraction (degraded-mode NLU,
# mirrors executor.actions._MONTH_RU_TO_NUM — kept local to avoid an agent→executor
# import across the Zero-Trust boundary).
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
_MONTHS_ALT = "|".join(sorted((re.escape(k) for k in _MONTH_RU_TO_NUM), key=len, reverse=True))

_ISO_RANGE_RE = re.compile(
    r"с\s+(\d{4})-(\d{2})-(\d{2})\s+по\s+(\d{4})-(\d{2})-(\d{2})", re.IGNORECASE
)
_NUMERIC_RANGE_RE = re.compile(
    r"с\s+(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\s+по\s+(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})",
    re.IGNORECASE,
)
_RU_RANGE_RE = re.compile(
    rf"с\s+(\d{{1,2}})(?:\s+({_MONTHS_ALT}))?\s+по\s+(\d{{1,2}})\s+({_MONTHS_ALT})(?:\s+(\d{{4}}))?",
    re.IGNORECASE | re.UNICODE,
)
_NAMED_MONTH_RE = re.compile(rf"({_MONTHS_ALT})(?:\s+(\d{{4}}))?", re.IGNORECASE | re.UNICODE)


def _parse_period_params(message: str) -> dict:
    """Extract period/date_from/date_to from free text for degraded (fallback) mode.

    Mirrors the date-handling rules given to Gemini in SYSTEM_PROMPT, so report
    and transaction queries behave the same whether or not the LLM is reachable.
    Returns {} when nothing is recognised — caller falls back to no filtering.
    """
    msg = message.lower()

    m = _ISO_RANGE_RE.search(msg)
    if m:
        y1, mo1, d1, y2, mo2, d2 = m.groups()
        return {"date_from": f"{y1}-{mo1}-{d1}", "date_to": f"{y2}-{mo2}-{d2}"}

    m = _NUMERIC_RANGE_RE.search(msg)
    if m:
        d1, mo1, y1, d2, mo2, y2 = m.groups()
        return {
            "date_from": f"{y1}-{int(mo1):02d}-{int(d1):02d}",
            "date_to": f"{y2}-{int(mo2):02d}-{int(d2):02d}",
        }

    m = _RU_RANGE_RE.search(msg)
    if m:
        d1, mon1, d2, mon2, year = m.groups()
        month_num = _MONTH_RU_TO_NUM.get(mon1 or "") or _MONTH_RU_TO_NUM.get(mon2 or "")
        if month_num:
            yr = int(year) if year else datetime.now(timezone.utc).year
            try:
                return {
                    "date_from": f"{yr:04d}-{month_num:02d}-{int(d1):02d}",
                    "date_to": f"{yr:04d}-{month_num:02d}-{int(d2):02d}",
                }
            except ValueError:
                pass

    if "прошлый месяц" in msg or "предыдущий месяц" in msg:
        return {"period": "last_month"}
    if "этот месяц" in msg or "текущий месяц" in msg:
        return {"period": "current_month"}

    m = _NAMED_MONTH_RE.search(msg)
    if m:
        month_name, year = m.groups()
        return {"period": f"{month_name} {year}" if year else month_name}

    return {}


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


# Hardcoded answers for common educational questions used when Gemini is unavailable.
# Keys are lowercase substrings to match against; values are ready Russian answers.
_FAQ: list[tuple[list[str], str]] = [
    (
        ["разница между ип и ооо", "чем ип отличается от ооо", "ип или ооо",
         "ип и ооо разница", "отличие ип от ооо", "ип vs ооо"],
        "ИП — индивидуальный предприниматель, физическое лицо, ведущее бизнес самостоятельно. "
        "Регистрация проще и дешевле, налоги ниже, но предприниматель несёт личную ответственность "
        "по долгам всем своим имуществом. "
        "ООО — общество с ограниченной ответственностью, юридическое лицо. "
        "Участники рискуют только вкладом в уставный фонд, есть возможность привлекать "
        "соучредителей и инвесторов, но регистрация сложнее и отчётность объёмнее.",
    ),
    (
        ["что такое овердрафт", "овердрафт что это", "как работает овердрафт",
         "что такое overdraft", "овердрафт по счёту", "овердрафт по счету"],
        "Овердрафт — краткосрочный кредитный лимит на расчётном счёте. "
        "Когда средств на счёте не хватает, банк автоматически покрывает разницу в пределах "
        "установленного лимита. Погашается при следующем поступлении средств на счёт. "
        "Удобен для покрытия кассовых разрывов без оформления отдельного кредита.",
    ),
    (
        ["как работает факторинг", "что такое факторинг", "объясни факторинг",
         "факторинг что это"],
        "Факторинг — финансовая услуга, при которой компания уступает банку (фактору) "
        "право требования дебиторской задолженности по выставленным счетам. "
        "Банк сразу выплачивает поставщику 70–90% суммы счёта, а когда покупатель "
        "погашает долг — перечисляет остаток за вычетом комиссии. "
        "Позволяет не ждать оплаты от покупателя и поддерживать оборотный капитал.",
    ),
    (
        ["что такое аккредитив", "аккредитив что это", "как работает аккредитив"],
        "Аккредитив — форма безналичного расчёта, при которой банк-эмитент по поручению "
        "покупателя обязуется выплатить продавцу деньги после предоставления документов, "
        "подтверждающих выполнение условий сделки. "
        "Защищает обе стороны: продавец гарантированно получит оплату, "
        "покупатель — только после фактической отгрузки товара.",
    ),
    (
        ["что такое бик", "бик что это", "бик банка", "что значит бик"],
        "БИК (Банковский идентификационный код) — уникальный числовой код банка, "
        "используемый при проведении межбанковских платежей. "
        "В Беларуси состоит из 9 цифр. Указывается в платёжных поручениях вместе "
        "с расчётным счётом получателя для правильной маршрутизации перевода.",
    ),
    (
        ["что такое ндс", "ндс что это", "как работает ндс", "объясни ндс"],
        "НДС (налог на добавленную стоимость) — косвенный налог, включаемый в цену "
        "товара или услуги. В Беларуси стандартная ставка — 20%. "
        "Продавец начисляет НДС на реализацию и уплачивает в бюджет разницу между "
        "полученным и уплаченным поставщикам налогом (входящий НДС к вычету).",
    ),
    (
        ["что такое унп", "унп что это", "что такое инн", "унп организации"],
        "УНП (учётный номер плательщика) — уникальный идентификатор юридического лица "
        "или ИП в Беларуси, аналог ИНН в России. "
        "Присваивается при регистрации и используется во всех налоговых и финансовых документах.",
    ),
    (
        ["что такое swift", "свифт что это", "swift перевод", "что такое swift код"],
        "SWIFT (Society for Worldwide Interbank Financial Telecommunication) — "
        "международная система межбанковских сообщений для проведения трансграничных платежей. "
        "SWIFT-код (BIC) однозначно идентифицирует банк в международных переводах "
        "и состоит из 8 или 11 символов.",
    ),
    (
        ["что такое iban", "iban что это", "формат iban", "как выглядит iban"],
        "IBAN (International Bank Account Number) — международный номер банковского счёта. "
        "В Беларуси начинается с BY, затем 2 контрольные цифры и 24 символа кода банка и счёта, "
        "итого 28 символов. Используется для международных и внутренних переводов.",
    ),
    (
        ["что такое лизинг", "лизинг что это", "как работает лизинг", "объясни лизинг"],
        "Лизинг — это долгосрочная аренда оборудования, транспорта или недвижимости "
        "с правом последующего выкупа. Лизинговая компания покупает имущество и передаёт "
        "его вам в пользование за регулярные платежи. По окончании договора можно выкупить "
        "объект по остаточной стоимости. Удобен для бизнеса: не нужен большой единовременный "
        "платёж, а лизинговые платежи относятся на расходы.",
    ),
    (
        ["что такое эквайринг", "эквайринг что это", "как работает эквайринг"],
        "Эквайринг — услуга банка, позволяющая принимать оплату картами и через "
        "электронные кошельки. Банк-эквайер обеспечивает терминалы и интернет-шлюзы, "
        "зачисляя средства на расчётный счёт продавца за вычетом комиссии (обычно 1–3%).",
    ),
]


def _match_faq(message: str) -> str | None:
    msg = message.lower().strip()
    for keywords, answer in _FAQ:
        if any(kw in msg for kw in keywords):
            return answer
    return None


def _fallback_response(message: str) -> GeminiResponse:
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
                faq_answer = _match_faq(message)
                if faq_answer:
                    return GeminiResponse(
                        intent="general_assistant",
                        action=None,
                        parameters={},
                        requires_confirmation=False,
                        confidence="high",
                        user_message=faq_answer,
                    )
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
            if intent_key in ("transaction", "report"):
                period_params = _parse_period_params(message)
                if period_params:
                    base["parameters"] = {**base["parameters"], **period_params}
                    base["user_message"] = (
                        "Формирую отчёт по вашему запросу за указанный период."
                        if intent_key == "report"
                        else "Вот операции по вашему счёту за указанный период."
                    )
            if intent_key == "transaction":
                if any(kw in msg_lower for kw in ("входящ", "поступлен")):
                    base["parameters"] = {**base["parameters"], "filter": "incoming"}
                elif any(kw in msg_lower for kw in ("исходящ",)):
                    base["parameters"] = {**base["parameters"], "filter": "outgoing"}
            return GeminiResponse(**base)

    faq_answer = _match_faq(message)
    if faq_answer:
        return GeminiResponse(
            intent="general_assistant",
            action=None,
            parameters={},
            requires_confirmation=False,
            confidence="high",
            user_message=faq_answer,
        )
    return GeminiResponse(
        intent="general_question",
        action=None,
        parameters={},
        requires_confirmation=False,
        confidence="low",
        user_message=(
            "Сервис временно недоступен для ответа на общие вопросы. "
            "Попробуйте повторить запрос через несколько минут."
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
