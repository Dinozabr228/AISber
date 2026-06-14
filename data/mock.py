from typing import Dict, Any

MOCK_USERS: Dict[str, Any] = {
    "user_001": {
        "user_id": "user_001",
        "company_name": "ООО «ТехноСтрой БЕЛ»",
        "balance": 100000.00,
        "known_recipients": [
            "ООО «СтройМатериалы Плюс»",
            "ИП Ковалёв А.С.",
            "ООО «Электроснаб»",
            "ЗАО «МеталлоПрокат»",
            "ООО «ЛогистикПро»",
        ],
        "favorites": [
            "ООО «СтройМатериалы Плюс»",
            "ИП Ковалёв А.С.",
        ],
        "average_transfer": 3200.00,
        "active_hours": [9, 10, 11, 12, 13, 14, 15, 16, 17, 18],
        "transaction_history": [
            {"date": "2026-05-01", "amount": 12500.00, "type": "expense", "category": "Строительные материалы",  "recipient": "ООО «СтройМатериалы Плюс»"},
            {"date": "2026-05-02", "amount": 85000.00, "type": "income",                                          "sender":    "ЗАО «МеталлоПрокат»"},
            {"date": "2026-05-05", "amount": 4200.00,  "type": "expense", "category": "Складские услуги",        "recipient": "ООО «СкладЛогистик»"},
            {"date": "2026-05-06", "amount": 1850.00,  "type": "expense", "category": "Электроэнергия",          "recipient": "РУП «Минскэнерго»"},
            {"date": "2026-05-07", "amount": 6700.00,  "type": "expense", "category": "Инструменты",             "recipient": "ООО «ИнструментПрофи»"},
            {"date": "2026-05-09", "amount": 32000.00, "type": "income",                                          "sender":    "ООО «ЛогистикПро»"},
            {"date": "2026-05-12", "amount": 3100.00,  "type": "expense", "category": "Транспорт",               "recipient": "ООО «АвтоТранс»"},
            {"date": "2026-05-13", "amount": 900.00,   "type": "expense", "category": "Канцтовары",              "recipient": "ООО «ОфисМаркет»"},
            {"date": "2026-05-14", "amount": 5500.00,  "type": "expense", "category": "Субподрядчики",           "recipient": "ИП Ковалёв А.С."},
            {"date": "2026-05-15", "amount": 18000.00, "type": "income",                                          "sender":    "ЗАО «МеталлоПрокат»"},
            {"date": "2026-05-16", "amount": 2200.00,  "type": "expense", "category": "Оборудование",            "recipient": "ООО «СнабТехника»"},
            {"date": "2026-05-19", "amount": 7800.00,  "type": "expense", "category": "Строительные работы",     "recipient": "ООО «СтройПодряд»"},
            {"date": "2026-05-20", "amount": 45000.00, "type": "income",                                          "sender":    "ООО «СтройМатериалы Плюс»"},
            {"date": "2026-05-21", "amount": 3500.00,  "type": "expense", "category": "Техническое обслуживание","recipient": "ООО «ТехСервис»"},
            {"date": "2026-05-22", "amount": 1100.00,  "type": "expense", "category": "Связь",                   "recipient": "ООО «ТелеКом»"},
            {"date": "2026-05-23", "amount": 9600.00,  "type": "expense", "category": "Спецоснастка",            "recipient": "ООО «СпецОснастка»"},
            {"date": "2026-05-26", "amount": 22000.00, "type": "income",                                          "sender":    "ООО «Электроснаб»"},
            {"date": "2026-05-27", "amount": 4800.00,  "type": "expense", "category": "Аренда оборудования",     "recipient": "ООО «ТехПрокат»"},
            {"date": "2026-05-28", "amount": 2700.00,  "type": "expense", "category": "Маркетинг",               "recipient": "ООО «МедиаСервис»"},
            {"date": "2026-05-30", "amount": 11200.00, "type": "income",                                          "sender":    "ЗАО «МеталлоПрокат»"},
        ],
    },

    "user_002": {
        "user_id": "user_002",
        "company_name": "ИП Романова Екатерина Сергеевна",
        "balance": 8940.75,
        "known_recipients": [
            "ООО «Дизайн Студия Арт»",
            "ИП Петров Д.В.",
            "ООО «ПринтХаус»",
            "ООО «Медиагруп»",
        ],
        "favorites": [
            "ООО «Медиагруп»",
        ],
        "average_transfer": 850.00,
        "active_hours": [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20],
        "transaction_history": [
            {"date": "2026-05-01", "amount": 2400.00, "type": "expense", "category": "Субподрядчики",           "recipient": "ИП Морозова Т.С."},
            {"date": "2026-05-02", "amount": 5000.00, "type": "income",                                          "sender":    "ООО «Медиагруп»"},
            {"date": "2026-05-04", "amount": 750.00,  "type": "expense", "category": "Программное обеспечение", "recipient": "ООО «СофтСервис»"},
            {"date": "2026-05-05", "amount": 1200.00, "type": "expense", "category": "Печатные услуги",         "recipient": "ООО «ПринтХаус»"},
            {"date": "2026-05-07", "amount": 8500.00, "type": "income",                                          "sender":    "ООО «Дизайн Студия Арт»"},
            {"date": "2026-05-08", "amount": 450.00,  "type": "expense", "category": "Связь",                   "recipient": "РУП «Белтелеком»"},
            {"date": "2026-05-09", "amount": 600.00,  "type": "expense", "category": "Офисные расходы",         "recipient": "ООО «ОфисМир»"},
            {"date": "2026-05-12", "amount": 3200.00, "type": "income",                                          "sender":    "ООО «Медиагруп»"},
            {"date": "2026-05-13", "amount": 1800.00, "type": "expense", "category": "Субподрядчики",           "recipient": "ИП Петров Д.В."},
            {"date": "2026-05-14", "amount": 950.00,  "type": "expense", "category": "Аренда офиса",            "recipient": "ООО «КоворкингБай»"},
            {"date": "2026-05-15", "amount": 6000.00, "type": "income",                                          "sender":    "ООО «Дизайн Студия Арт»"},
            {"date": "2026-05-16", "amount": 340.00,  "type": "expense", "category": "Доставка",                "recipient": "ООО «КурьерСервис»"},
            {"date": "2026-05-19", "amount": 2100.00, "type": "expense", "category": "Субподрядчики",           "recipient": "ИП Соколов К.А."},
            {"date": "2026-05-20", "amount": 4500.00, "type": "income",                                          "sender":    "ООО «БизнесКомпани»"},
            {"date": "2026-05-21", "amount": 890.00,  "type": "expense", "category": "Банковские услуги",       "recipient": "ОАО «Сбер Банк»"},
            {"date": "2026-05-22", "amount": 1500.00, "type": "expense", "category": "Субподрядчики",           "recipient": "ИП Лебедева А.Н."},
            {"date": "2026-05-23", "amount": 7200.00, "type": "income",                                          "sender":    "ООО «Медиагруп»"},
            {"date": "2026-05-26", "amount": 680.00,  "type": "expense", "category": "Обучение",                "recipient": "ООО «КнигаБизнес»"},
            {"date": "2026-05-27", "amount": 2800.00, "type": "income",                                          "sender":    "ИП Кравцова М.П."},
            {"date": "2026-05-29", "amount": 1100.00, "type": "expense", "category": "Налоги",                  "recipient": "МНС Республики Беларусь"},
        ],
    },

    "user_003": {
        "user_id": "user_003",
        "company_name": "ООО «Агрокомплекс Нива»",
        "balance": 213450.00,
        "known_recipients": [
            "ООО «АгроХимия Плюс»",
            "РУП «Белагросервис»",
            "ООО «СемТех»",
            "ООО «ЗерноТрейд»",
            "ИП Лукашенко В.М.",
            "ООО «МТЗ-Сервис»",
        ],
        "favorites": [
            "ООО «АгроХимия Плюс»",
            "ООО «МТЗ-Сервис»",
        ],
        "average_transfer": 18500.00,
        "active_hours": [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17],
        "transaction_history": [
            {"date": "2026-05-01", "amount": 45000.00,  "type": "expense", "category": "Агрохимия",               "recipient": "ООО «АгроХимия Плюс»"},
            {"date": "2026-05-02", "amount": 180000.00, "type": "income",                                           "sender":    "ООО «ЗерноТрейд»"},
            {"date": "2026-05-04", "amount": 28000.00,  "type": "expense", "category": "Агросервис",               "recipient": "РУП «Белагросервис»"},
            {"date": "2026-05-05", "amount": 12500.00,  "type": "expense", "category": "Топливо",                  "recipient": "РУП «Белнефтехим»"},
            {"date": "2026-05-06", "amount": 8900.00,   "type": "expense", "category": "Техническое обслуживание", "recipient": "ООО «МТЗ-Сервис»"},
            {"date": "2026-05-07", "amount": 95000.00,  "type": "income",                                           "sender":    "Министерство сельского хозяйства РБ"},
            {"date": "2026-05-09", "amount": 15000.00,  "type": "expense", "category": "Субподрядчики",            "recipient": "КУСП «Агро-Нива»"},
            {"date": "2026-05-10", "amount": 6200.00,   "type": "expense", "category": "Ветеринарные услуги",      "recipient": "УП «Белветсервис»"},
            {"date": "2026-05-12", "amount": 22000.00,  "type": "expense", "category": "Агрохимия",                "recipient": "ООО «АгроХимия Плюс»"},
            {"date": "2026-05-13", "amount": 67000.00,  "type": "income",                                           "sender":    "ООО «МолочГрупп»"},
            {"date": "2026-05-14", "amount": 18500.00,  "type": "expense", "category": "Техническое обслуживание", "recipient": "ООО «МТЗ-Сервис»"},
            {"date": "2026-05-15", "amount": 4800.00,   "type": "expense", "category": "Электроэнергия",           "recipient": "РУП «Минскэнерго»"},
            {"date": "2026-05-16", "amount": 11200.00,  "type": "expense", "category": "Агрохимия",                "recipient": "ООО «АгроХимия Плюс»"},
            {"date": "2026-05-19", "amount": 130000.00, "type": "income",                                           "sender":    "ООО «ЗерноТрейд»"},
            {"date": "2026-05-20", "amount": 35000.00,  "type": "expense", "category": "Сельхозтехника",           "recipient": "ООО «АгроМаш»"},
            {"date": "2026-05-21", "amount": 9100.00,   "type": "expense", "category": "Мелиорация",               "recipient": "ООО «МелиоТехника»"},
            {"date": "2026-05-22", "amount": 3200.00,   "type": "expense", "category": "Связь",                    "recipient": "ООО «ТелеКом»"},
            {"date": "2026-05-23", "amount": 48000.00,  "type": "income",                                           "sender":    "ООО «МолочГрупп»"},
            {"date": "2026-05-26", "amount": 16800.00,  "type": "expense", "category": "Зарплата",                 "recipient": "Зарплатный проект"},
            {"date": "2026-05-27", "amount": 7500.00,   "type": "expense", "category": "Страхование",              "recipient": "ОАО «Белгосстрах»"},
        ],
    },
}


def get_user(user_id: str) -> Dict[str, Any] | None:
    return MOCK_USERS.get(user_id)


def get_all_user_ids() -> list[str]:
    return list(MOCK_USERS.keys())


# ---------------------------------------------------------------------------
# Counterparty registry — HIGH-02 / MEDIUM-02
# Provides bank, masked account, and currency for known legal entities.
# ---------------------------------------------------------------------------

MOCK_COUNTERPARTY_REGISTRY: Dict[str, Any] = {
    # user_001 known recipients
    "ООО «СтройМатериалы Плюс»": {
        "organization_name": "ООО «СтройМатериалы Плюс»",
        "bank": "ОАО «Сбер Банк»",
        "account_masked": "BY** **** **** **** 4821",
        "last_four": "4821",
        "currency": "BYN",
    },
    "ИП Ковалёв А.С.": {
        "organization_name": "ИП Ковалёв А.С.",
        "bank": "АСБ «Беларусбанк»",
        "account_masked": "BY** **** **** **** 7310",
        "last_four": "7310",
        "currency": "BYN",
    },
    "ООО «Электроснаб»": {
        "organization_name": "ООО «Электроснаб»",
        "bank": "ОАО «Белинвестбанк»",
        "account_masked": "BY** **** **** **** 5529",
        "last_four": "5529",
        "currency": "BYN",
    },
    "ЗАО «МеталлоПрокат»": {
        "organization_name": "ЗАО «МеталлоПрокат»",
        "bank": "ОАО «Приорбанк»",
        "account_masked": "BY** **** **** **** 9014",
        "last_four": "9014",
        "currency": "BYN",
    },
    "ООО «ЛогистикПро»": {
        "organization_name": "ООО «ЛогистикПро»",
        "bank": "ОАО «Сбер Банк»",
        "account_masked": "BY** **** **** **** 3377",
        "last_four": "3377",
        "currency": "BYN",
    },
    # user_002 known recipients
    "ООО «Дизайн Студия Арт»": {
        "organization_name": "ООО «Дизайн Студия Арт»",
        "bank": "АСБ «Беларусбанк»",
        "account_masked": "BY** **** **** **** 6102",
        "last_four": "6102",
        "currency": "BYN",
    },
    "ИП Петров Д.В.": {
        "organization_name": "ИП Петров Д.В.",
        "bank": "ОАО «БНБ-Банк»",
        "account_masked": "BY** **** **** **** 2248",
        "last_four": "2248",
        "currency": "BYN",
    },
    "ООО «ПринтХаус»": {
        "organization_name": "ООО «ПринтХаус»",
        "bank": "ОАО «Сбер Банк»",
        "account_masked": "BY** **** **** **** 8815",
        "last_four": "8815",
        "currency": "BYN",
    },
    "ООО «Медиагруп»": {
        "organization_name": "ООО «Медиагруп»",
        "bank": "ОАО «БСБ Банк»",
        "account_masked": "BY** **** **** **** 4439",
        "last_four": "4439",
        "currency": "BYN",
    },
    # user_003 known recipients
    "ООО «АгроХимия Плюс»": {
        "organization_name": "ООО «АгроХимия Плюс»",
        "bank": "ОАО «Белагропромбанк»",
        "account_masked": "BY** **** **** **** 1703",
        "last_four": "1703",
        "currency": "BYN",
    },
    "РУП «Белагросервис»": {
        "organization_name": "РУП «Белагросервис»",
        "bank": "АСБ «Беларусбанк»",
        "account_masked": "BY** **** **** **** 5521",
        "last_four": "5521",
        "currency": "BYN",
    },
    "ООО «СемТех»": {
        "organization_name": "ООО «СемТех»",
        "bank": "ОАО «Белагропромбанк»",
        "account_masked": "BY** **** **** **** 9902",
        "last_four": "9902",
        "currency": "BYN",
    },
    "ООО «ЗерноТрейд»": {
        "organization_name": "ООО «ЗерноТрейд»",
        "bank": "ОАО «Приорбанк»",
        "account_masked": "BY** **** **** **** 7745",
        "last_four": "7745",
        "currency": "BYN",
    },
    "ИП Лукашенко В.М.": {
        "organization_name": "ИП Лукашенко В.М.",
        "bank": "АСБ «Беларусбанк»",
        "account_masked": "BY** **** **** **** 3318",
        "last_four": "3318",
        "currency": "BYN",
    },
    "ООО «МТЗ-Сервис»": {
        "organization_name": "ООО «МТЗ-Сервис»",
        "bank": "ОАО «Белинвестбанк»",
        "account_masked": "BY** **** **** **** 6641",
        "last_four": "6641",
        "currency": "BYN",
    },
}


def get_counterparty(name: str) -> Dict[str, Any] | None:
    return MOCK_COUNTERPARTY_REGISTRY.get(name)
