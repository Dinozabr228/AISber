import re
import unicodedata

# Invisible and zero-width Unicode characters used to break pattern matching.
_INVISIBLE_CHARS_RE = re.compile(
    r"[​‌‍‎‏﻿­"  # zero-width, soft-hyphen, BOM
    r"⁠⁡⁢⁣⁤"               # invisible math operators
    r"᠎͏ᅟᅠ឴឵"         # various invisible separators
    r"ㅤﾠ  - ]"             # non-breaking and thin spaces
)


def _normalize_for_scan(text: str) -> str:
    """
    Prepare text for pattern matching:
    1. NFKC normalization collapses look-alike Unicode variants (e.g. full-width letters).
    2. Strip invisible characters that break word boundaries.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE_CHARS_RE.sub("", text)
    return text


_INJECTION_PATTERNS: list[tuple[str, str]] = [
    # English
    (r"ignore\s+(?:previous|all\s+previous)\s+(?:instructions?|prompts?|commands?)", "CRITICAL"),
    (r"forget\s+(?:all\s+)?(?:previous\s+)?instructions?", "CRITICAL"),
    (r"disregard\s+(?:all\s+)?(?:previous\s+)?instructions?", "CRITICAL"),
    (r"override\s+(?:system\s+)?(?:prompt|instructions?)", "CRITICAL"),
    # Russian
    (r"забудь\s+(?:все\s+)?(?:предыдущие\s+)?(?:инструкции|правила|ограничения)", "CRITICAL"),
    (r"игнорируй\s+(?:все\s+)?(?:предыдущие\s+)?(?:инструкции|правила|команды)", "CRITICAL"),
    (r"проигнорируй\s+(?:все\s+)?(?:инструкции|правила)", "CRITICAL"),
    (r"отмени\s+(?:все\s+)?(?:предыдущие\s+)?(?:инструкции|правила|ограничения)", "CRITICAL"),
    (r"сброс\s+инструкций", "CRITICAL"),
    # Transliteration
    (r"ignore\s+instrukcii", "CRITICAL"),
    (r"zabud[i]?\s+(?:vse\s+)?(?:pravila|instrukcii)", "CRITICAL"),
]

_ROLE_OVERRIDE_PATTERNS: list[tuple[str, str]] = [
    # English
    (r"(?:act|behave)\s+as\b", "HIGH"),
    (r"you\s+are\s+now\b", "HIGH"),
    (r"pretend\s+(?:you\s+are|to\s+be)\b", "HIGH"),
    (r"jailbreak", "CRITICAL"),
    (r"developer\s+mode", "HIGH"),
    (r"\bdan\s+mode\b", "HIGH"),
    (r"do\s+anything\s+now", "CRITICAL"),
    # Russian
    (r"ты\s+теперь\b", "HIGH"),
    (r"притворись\s+(?:что\s+)?(?:ты\b|будто\b)", "HIGH"),
    (r"действуй\s+как\b", "HIGH"),
    (r"веди\s+себя\s+как\b", "HIGH"),
    (r"ты\s+(?:больше\s+)?не\s+(?:AI|ИИ|ассистент|бот)", "HIGH"),
    (r"представь\s+(?:что\s+)?(?:ты\b|себя\b)", "HIGH"),
    (r"(?:новые|другие)\s+(?:правила|инструкции)\s*:", "HIGH"),
    (r"(?:режим|mode)\s+разработчика", "HIGH"),
    # Transliteration
    (r"ty\s+teper[']?\b", "HIGH"),
    (r"pritvoris[']?\s+chto", "HIGH"),
]

_HIDDEN_PROMPT_PATTERNS: list[tuple[str, str]] = [
    # English
    (r"show\s+(?:me\s+)?(?:your\s+)?system\s+prompt", "HIGH"),
    (r"reveal\s+(?:your\s+)?(?:system\s+)?prompt", "HIGH"),
    (r"show\s+(?:me\s+)?(?:your\s+)?hidden\s+(?:prompt|instructions?)", "HIGH"),
    (r"what\s+(?:are\s+)?(?:your\s+)?(?:hidden\s+)?instructions?", "MEDIUM"),
    (r"repeat\s+(?:your\s+)?(?:system\s+)?prompt", "HIGH"),
    # Russian
    (r"покажи\s+(?:мне\s+)?(?:свой\s+)?системный\s+промпт", "HIGH"),
    (r"раскрой\s+(?:свои\s+)?(?:скрытые\s+)?инструкции", "HIGH"),
    (r"покажи\s+(?:свои\s+)?(?:скрытые\s+)?инструкции", "HIGH"),
    (r"что\s+написано\s+в\s+(?:(?:системном\s+)?промпте|инструкции)", "HIGH"),
    (r"повтори\s+(?:свои\s+)?(?:системные\s+)?инструкции", "HIGH"),
    (r"системный\s+промпт", "MEDIUM"),
    (r"скрытые\s+инструкции", "MEDIUM"),
    (r"твои\s+(?:настоящие\s+)?(?:правила|инструкции|ограничения)", "MEDIUM"),
    # Transliteration
    (r"sistemn[yi][ji]?\s*prompt", "HIGH"),
    (r"skrytye\s+instrukcii", "HIGH"),
]

_CODE_EXECUTION_PATTERNS: list[tuple[str, str]] = [
    (r"\beval\s*\(", "CRITICAL"),
    (r"\bexec\s*\(", "CRITICAL"),
    (r"\bexecute\s*\(", "HIGH"),
    (r"\bcompile\s*\(", "HIGH"),
    (r"\b__import__\s*\(", "CRITICAL"),
    (r"\bimportlib\b", "HIGH"),
]

_SHELL_PATTERNS: list[tuple[str, str]] = [
    (r"\bsubprocess\b", "CRITICAL"),
    (r"\bos\.system\s*\(", "CRITICAL"),
    (r"\bos\.popen\s*\(", "CRITICAL"),
    (r"\brm\s+-rf\b", "CRITICAL"),
    (r"\bpip\s+install\b", "HIGH"),
    (r"\bcurl\s+", "HIGH"),
    (r"\bwget\s+", "HIGH"),
    (r"\b(?:bash|sh|zsh)\s+[-/]", "CRITICAL"),
    (r"\bcmd(?:\.exe)?\b", "HIGH"),
    (r"\bpowershell\b", "HIGH"),
    (r"\bnc\s+-[a-z]*e\b", "CRITICAL"),
]

_SQL_PATTERNS: list[tuple[str, str]] = [
    (r"\bSELECT\s+.+\s+FROM\b", "HIGH"),
    (r"\bDROP\s+TABLE\b", "CRITICAL"),
    (r"\bUNION\s+SELECT\b", "CRITICAL"),
    (r"\bINSERT\s+INTO\b", "HIGH"),
    (r"\bDELETE\s+FROM\b", "HIGH"),
    (r"\bUPDATE\s+\w+\s+SET\b", "HIGH"),
    (r"--\s*$", "MEDIUM"),
    (r"'\s*OR\s*'?\d+'\s*=\s*'?\d+", "CRITICAL"),
]

_EXFILTRATION_PATTERNS: list[tuple[str, str]] = [
    (r"send\s+(?:all\s+)?(?:user\s+)?data\s+to\b", "CRITICAL"),
    (r"export\s+(?:all\s+)?(?:user\s+)?(?:data|records)\b", "HIGH"),
    (r"(?:upload|post)\s+(?:all\s+)?(?:data|records)\s+to\b", "HIGH"),
    (r"base64\s*(?:encode|decode)", "MEDIUM"),
    (r"hex(?:lify)?\s*\(", "MEDIUM"),
    # Russian data exfiltration
    (r"отправь\s+(?:все\s+)?данные\s+(?:на|по)", "CRITICAL"),
    (r"передай\s+(?:все\s+)?(?:данные|записи)\s+(?:на|по|в)", "HIGH"),
    (r"слить\s+(?:базу|данные|информацию)", "CRITICAL"),
]

_SEVERITY_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def _scan(text: str, patterns: list[tuple[str, str]], block_reason: str) -> dict:
    worst_severity: str | None = None
    for pattern, severity in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            if worst_severity is None or _SEVERITY_ORDER[severity] > _SEVERITY_ORDER[worst_severity]:
                worst_severity = severity
    if worst_severity is not None:
        return {"blocked": True, "reason": block_reason, "severity": worst_severity}
    return {"blocked": False, "reason": "", "severity": "LOW"}


def detect_prompt_injection(text: str) -> dict:
    return _scan(text, _INJECTION_PATTERNS, "Обнаружена попытка внедрения инструкций (prompt injection)")


def detect_role_override(text: str) -> dict:
    return _scan(text, _ROLE_OVERRIDE_PATTERNS, "Обнаружена попытка изменить роль ассистента")


def detect_hidden_prompt_access(text: str) -> dict:
    return _scan(text, _HIDDEN_PROMPT_PATTERNS, "Обнаружена попытка получить доступ к системному промпту")


def detect_code_execution(text: str) -> dict:
    return _scan(text, _CODE_EXECUTION_PATTERNS, "Обнаружена попытка выполнения произвольного кода")


def detect_shell_commands(text: str) -> dict:
    return _scan(text, _SHELL_PATTERNS, "Обнаружена попытка выполнения shell-команд")


def detect_sql_injection(text: str) -> dict:
    return _scan(text, _SQL_PATTERNS, "Обнаружена попытка SQL-инъекции")


def detect_data_exfiltration(text: str) -> dict:
    return _scan(text, _EXFILTRATION_PATTERNS, "Обнаружена попытка несанкционированной передачи данных")


_DETECTORS = [
    detect_prompt_injection,
    detect_role_override,
    detect_hidden_prompt_access,
    detect_code_execution,
    detect_shell_commands,
    detect_sql_injection,
    detect_data_exfiltration,
]


def check_request(text: str) -> dict:
    # Normalize before scanning to defeat Unicode-based evasion.
    normalized = _normalize_for_scan(text)
    worst: dict | None = None
    for detector in _DETECTORS:
        result = detector(normalized)
        if result["blocked"]:
            if worst is None or _SEVERITY_ORDER[result["severity"]] > _SEVERITY_ORDER[worst["severity"]]:
                worst = result
    return worst if worst is not None else {"blocked": False, "reason": "", "severity": "LOW"}
