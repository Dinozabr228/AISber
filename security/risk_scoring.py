from datetime import datetime
from typing import Any


def calculate_risk(
    action: str,
    parameters: dict[str, Any],
    user_profile: dict[str, Any],
) -> dict[str, Any]:
    score = 0
    reasons: list[str] = []

    amount: float = float(parameters.get("amount", 0))
    recipient: str = parameters.get("recipient", "")
    known_recipients: list[str] = user_profile.get("known_recipients", [])
    average_transfer: float = float(user_profile.get("average_transfer", 0))
    active_hours: list[int] = user_profile.get("active_hours", [])

    is_unknown_recipient = bool(recipient and recipient not in known_recipients)
    if is_unknown_recipient:
        score += 40
        reasons.append(f"Получатель «{recipient}» не найден среди ваших контрагентов")

    # Extra penalty: unknown recipient + non-trivial amount always requires confirmation.
    if is_unknown_recipient and amount > 1_000:
        score += 20
        reasons.append("Перевод новому контрагенту на значительную сумму требует дополнительной проверки")

    # HIGH-01: missing bank details for unknown recipient
    has_account = bool(parameters.get("account_number"))
    has_bank    = bool(parameters.get("bank_name"))
    if is_unknown_recipient and not (has_account and has_bank):
        score += 30
        reasons.append("missing_recipient_details")
        reasons.append("Отсутствуют банковские реквизиты нового получателя — операция заблокирована до их предоставления")

    if amount and average_transfer and amount > average_transfer * 2:
        score += 30
        reasons.append("Сумма перевода значительно превышает вашу обычную активность")

    current_hour = datetime.now().hour
    if active_hours and current_hour not in active_hours:
        score += 20
        reasons.append("Операция выполняется в нерабочее время")

    if amount > 10_000:
        score += 10
        reasons.append("Крупная сумма операции")

    if score >= 60:
        level = "HIGH"
    elif score >= 30:
        level = "MEDIUM"
    else:
        level = "LOW"

    return {"score": score, "level": level, "reasons": reasons}
