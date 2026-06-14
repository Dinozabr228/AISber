import re

# BY + 2 check digits + 4-char bank code + 20 digits
_RE_ACCOUNT = re.compile(r'\bBY\d{2}[A-Z]{4}\d{20}\b')

_RE_CARD = re.compile(
    r'\b(?:\d{4}[\s\-]){3}\d{4}\b'
    r'|\b\d{16}\b'
)

# УНП — exactly 9 digits not adjacent to other digits
_RE_TAX_ID = re.compile(r'(?<!\d)\d{9}(?!\d)')

_RE_PHONE = re.compile(
    r'(?:\+375|375|80)'
    r'[\s\-]?'
    r'(?:\(\d{2}\)|\d{2})'
    r'[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}'
)

_RE_EMAIL = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')

# Серия + номер паспорта РБ: 2 буквы + 7 цифр
_RE_PERSONAL = re.compile(r'\b[A-Z]{2}\d{7}\b')

_RE_UUID = re.compile(
    r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'
)


def redact_accounts(text: str) -> str:
    return _RE_ACCOUNT.sub('[ACCOUNT_REDACTED]', text)

def redact_cards(text: str) -> str:
    return _RE_CARD.sub('[CARD_REDACTED]', text)

def redact_tax_ids(text: str) -> str:
    return _RE_TAX_ID.sub('[TAX_ID_REDACTED]', text)

def redact_phone_numbers(text: str) -> str:
    return _RE_PHONE.sub('[PHONE_REDACTED]', text)

def redact_emails(text: str) -> str:
    return _RE_EMAIL.sub('[EMAIL_REDACTED]', text)

def redact_personal_data(text: str) -> str:
    return _RE_PERSONAL.sub('[PERSONAL_DATA_REDACTED]', text)

def redact_internal_ids(text: str) -> str:
    return _RE_UUID.sub('[ID_REDACTED]', text)

def sanitize_prompt(text: str) -> str:
    text = redact_accounts(text)
    text = redact_cards(text)
    text = redact_emails(text)
    text = redact_phone_numbers(text)
    text = redact_personal_data(text)
    text = redact_tax_ids(text)
    text = redact_internal_ids(text)
    return text
