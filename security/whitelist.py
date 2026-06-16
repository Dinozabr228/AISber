ALLOWED_ACTIONS: list[str] = [
    "get_transactions",
    "create_report",
    "initiate_transfer",
    "get_tariffs",
    "get_requisites",
    "navigate",
    "get_counterparties",
    "get_favorites",
]

_ALLOWED_SET: frozenset[str] = frozenset(ALLOWED_ACTIONS)


def validate_action(action: str) -> bool:
    """Return True only if action is an exact, case-sensitive match in ALLOWED_ACTIONS."""
    return action in _ALLOWED_SET
