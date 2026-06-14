# SberBusiness AI Agent v2.0

Интеллектуальный банковский ассистент для бизнес-клиентов на базе Gemini AI.  
Встраивается поверх любого SPA/HTML-страницы в виде чат-виджета.

## Быстрый старт (Windows)

Запустите `start.bat` — он сам найдёт Python, установит зависимости и откроет браузер.

## Ручной запуск

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env   # задайте GEMINI_API_KEY и API_KEY
uvicorn main:app --reload
```

Сервер: `http://localhost:8000`  
Демо-страница: `http://localhost:8000/demo`  
API docs: `http://localhost:8000/docs`

## Ключевые возможности

| Функция | Описание |
|---|---|
| Понимание естественного языка | Gemini извлекает намерение; keyword-fallback при недоступности API |
| Двойной режим | **Банкинг** — выполняет операции; **Помощник** — только отвечает, ничего не выполняет |
| Zero-Trust pipeline | 14-шаговый конвейер безопасности; Gemini никогда не выполняет операции напрямую |
| Риск-скоринг | LOW / MEDIUM / HIGH (0–100) с разными UX-сценариями подтверждения |
| AI Confidence | Низкая уверенность Gemini повышает уровень риска и добавляет +20 к score |
| Подтверждение переводов | Перевод всегда требует подтверждения; при score ≥ 60 — любое действие |
| Черновик перевода | Карточка с реквизитами, суммой и уровнем риска перед подтверждением |
| Новый контрагент | Отдельный шаг запроса реквизитов получателя перед выполнением |
| Обнаружение дублей | Повторный идентичный перевод получает тот же токен подтверждения |
| История чатов | Список диалогов с автоматическими заголовками и изоляцией по пользователю |
| Автозаголовки | Название диалога генерируется из первого действия (баланс, перевод и т.д.) |
| Переименование чатов | Inline-редактирование названия в списке истории или двойной клик по заголовку |
| Удаление чата | Кнопка удаляет текущий диалог и сразу открывает новый |
| Диалоговый контекст | `conversation_id` хранит контекст 30 мин, история сообщений сохраняется |
| Фильтр ПДН | Персональные данные маскируются до отправки в AI |
| Уведомления | Бейдж непрочитанных в шапке виджета, polling каждые 15 сек |
| Избранные контрагенты | Быстрый доступ к часто используемым получателям |

## API

Все `/api/v1/*` требуют заголовок `X-API-Key`.

```
POST   /api/v1/session                          → {session_token, user_id, expires_in}
POST   /api/v1/chat                             → {user_message, action_result, requires_confirmation, ...}
POST   /api/v1/confirm                          → {result, message}
GET    /api/v1/conversations?user_id=…          → {conversations}
GET    /api/v1/conversations/{id}/messages      → {messages, title}
PATCH  /api/v1/conversations/{id}/title         → {title}
DELETE /api/v1/conversations/{id}?user_id=…     → {deleted}
GET    /api/v1/notifications?user_id=…          → {notifications, unread}
POST   /api/v1/notifications/{id}/read          → {ok}
GET    /api/v1/drafts?user_id=…                 → {drafts, count}
POST   /api/v1/feedback?user_id=…              → {ok}
GET    /api/v1/metrics                          → счётчики
GET    /api/v1/health                           → статус, pipeline_steps, features
```

Пример:

```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sberik-dev-2026" \
  -d '{"user_id":"user_001","message":"Какой у меня баланс?","mode":"banking"}'
```

## Тестовые пользователи

| user_id | Компания | Баланс (BYN) |
|---|---|---|
| user_001 | ООО «ТехноСтрой БЕЛ» | 100 000.00 |
| user_002 | ИП Романова Екатерина Сергеевна | 8 940.75 |
| user_003 | ООО «Агрокомплекс Нива» | 213 450.00 |

## Структура проекта

```
main.py              — FastAPI приложение, Zero-Trust pipeline, история чатов
main.js              — JS-виджет (чат, история, переименование, уведомления)
main.css             — стили виджета
models.py            — Pydantic-модели
agent/gemini.py      — Gemini + keyword-fallback, коллоквиальные фразы
security/            — whitelist, risk_scoring, prompt_firewall, audit
privacy/             — фильтр ПДН перед отправкой в AI
executor/actions.py  — выполнение операций (mock)
data/mock.py         — тестовые данные 3 пользователей
start.bat            — запуск на Windows (авто-установка зависимостей)
```
