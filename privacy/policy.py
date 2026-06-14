from typing import Any

FORBIDDEN_FIELDS: list[str] = [
    "account_number",
    "card_number",
    "balance",
    "transaction_history",
    "requisites",
    "tax_id",
    "passport_data",
    "personal_id",
    "customer_id",
    "api_key",
    "token",
    "session_id",
    "phone",
    "email",
    "address",
    "document_number",
    "secret",
    "credential",
    "internal_id",
]

_FORBIDDEN_SET: frozenset[str] = frozenset(FORBIDDEN_FIELDS)


def clean_context(context: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in context.items():
        if key in _FORBIDDEN_SET:
            continue
        if isinstance(value, dict):
            result[key] = clean_context(value)
        elif isinstance(value, list):
            result[key] = _clean_list(value)
        else:
            result[key] = value
    return result


def _clean_list(items: list[Any]) -> list[Any]:
    cleaned = []
    for item in items:
        if isinstance(item, dict):
            cleaned.append(clean_context(item))
        elif isinstance(item, list):
            cleaned.append(_clean_list(item))
        else:
            cleaned.append(item)
    return cleaned
