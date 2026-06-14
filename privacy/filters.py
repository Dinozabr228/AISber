from typing import Any

from .policy import FORBIDDEN_FIELDS, clean_context
from .redactor import sanitize_prompt


def filter_user_request(
    user_message: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    """
    Sanitize user message and context dict before sending anything to Gemini.

    Returns a dict with:
      clean_message   — user message with all sensitive patterns redacted
      safe_context    — context dict with forbidden keys removed
      redacted_fields — list of top-level keys that were stripped from context
    """
    clean_message = sanitize_prompt(user_message)

    redacted_fields = [key for key in context if key in set(FORBIDDEN_FIELDS)]
    safe_context = clean_context(context)

    return {
        "clean_message": clean_message,
        "safe_context": safe_context,
        "redacted_fields": redacted_fields,
    }
