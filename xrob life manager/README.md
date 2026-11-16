# XROB Life Manager — Home Assistant add-on (MVP 0.1.0)

A local-first personal management app for calendar events, tasks, workouts, treadmill sessions, weight history, bills, document expiry and recurring personal-care routines. It includes a Telegram command bot and a companion Home Assistant custom integration for native calendar, todo and sensor entities.

## Current MVP scope

- Web dashboard with Overview, Calendar, Tasks, Fitness, Bills, Documents, Personal Care and Integrations pages.
- SQLite database stored persistently at `/config/xrob_life_manager.db`.
- Create/delete calendar events and tasks; view events in a monthly calendar grid; mark tasks complete.
- Record body weight, gym workouts, a weekly workout-day plan and treadmill sessions.
- Track bills in MAD, configure advance reminders and mark bills paid (tracking only; no money movement).
- Track document expiry dates and configurable reminder lead time.
- Recurring routines with configurable interval; completion updates the next due date. Starter routines: nail care every 15 days, haircut every 30 days and shower daily. Edit intervals in the app by deleting/recreating a routine for now; an edit screen is planned.
- Telegram commands: `/start`, `/today`, `/tasks`, `/bills`, `/weight 89.5`, `/event Meeting|2026-11-17 10:00`, `/task Buy groceries`, `/naildone`.
- Telegram due-date reminders for overdue/unpaid bills, tasks, document expiry and due routines. Reminders are sent once per item per day.
- Companion HA integration: `calendar.xrob_calendar`, `todo.xrob_tasks`, and sensors for weight, unpaid bills, bill total, expiring documents, open tasks, today's events and next care routine.
- No cloud database, Google Calendar sync or AI/Ollama connection.

## Install as a local Home Assistant add-on

1. Copy the entire `xrob-life-manager` folder into the Home Assistant add-ons directory, normally `/addons/xrob-life-manager` using the SSH add-on or File Editor.
2. In **Settings → Add-ons → Add-on Store**, open the three-dot menu and choose **Check for updates** / reload local add-ons.
3. Open **XROB Life Manager**, review its configuration, then install and start it.
4. Open `http://HOME_ASSISTANT_IP:8090` (for the remembered LAN host, `http://192.168.1.73:8090`).
5. The database is persisted in the add-on configuration mount. Back up Home Assistant regularly.

The add-on uses host networking so the web UI is reachable on port 8090 and the companion HA integration can reach the API over the LAN. Check that port 8090 is unused before starting.

## Telegram setup

1. Create a bot with Telegram's BotFather and copy its token.
2. Obtain your numeric Telegram chat ID (for example, send a message to the bot and inspect `getUpdates`).
3. Enter `telegram_bot_token` and `telegram_allowed_chat_id` in the add-on Configuration tab.
4. Save and restart the add-on.
5. Message the bot with `/start`.

The allowed chat ID is an access-control boundary. Do not leave it blank if the bot token is configured. The MVP accepts only this configured chat ID when one is set.

## Install the companion Home Assistant integration

Copy `homeassistant_custom_component/xrob_life_manager` into:

`/config/custom_components/xrob_life_manager/`

Restart Home Assistant. Then go to **Settings → Devices & services → Add integration**, search for **XROB Life Manager**, and enter:

`http://192.168.1.73:8090`

Replace the IP if your HA host differs. The integration polls locally once per minute. It creates `calendar.xrob_calendar`, `todo.xrob_tasks` and summary sensors. It also registers `xrob_life_manager.create_event`, `xrob_life_manager.record_weight` and `xrob_life_manager.complete_routine` services for HA automations.


### Home Assistant automation service examples

Create a calendar event:

```yaml
service: xrob_life_manager.create_event
data:
  title: Dentist appointment
  start_at: "2026-11-20T11:00:00"
  category: personal
```

Record a weigh-in:

```yaml
service: xrob_life_manager.record_weight
data:
  kg: 89.5
```

Complete a routine (use the routine's database ID):

```yaml
service: xrob_life_manager.complete_routine
data:
  routine_id: 1
```

## API

- `GET /api/health`
- `GET /api/summary`
- `GET /api/ha/state`
- `GET/POST /api/events`, `/api/tasks`, `/api/weights`, `/api/workouts`, `/api/treadmill`, `/api/bills`, `/api/documents`, `/api/routines`
- `PATCH/DELETE /api/{resource}/{id}`
- `POST /api/bills/{id}/pay`
- `POST /api/routines/{id}/complete`
- `POST /api/workouts/{id}/exercises`

## Important MVP limitations / next iteration

- No user login yet. Keep port 8090 on a trusted LAN; do not expose it directly to the internet. If publishing remotely, put it behind an authenticated reverse proxy/VPN first.
- Telegram currently uses explicit commands rather than natural-language parsing. AI is intentionally disabled.
- The calendar page currently includes a monthly grid and chronological upcoming-event list; day/week views and drag-and-drop rescheduling are future improvements.
- Workout logging stores sessions; exercise-set history is supported by the API but the exercise editor UI is a next milestone.
- Treadmill sessions are entered manually for now; automatic Walkfitpad/ESPHome integration is a later milestone.
- Recurrence currently records the interval and next due date. Automatic bill instance generation and recurring calendar expansion are planned.
- Documents currently store metadata only; local file attachments and encrypted document storage are planned.
- Add authentication, API authorization, input validation, migration management and tested backup/restore before any remote exposure or sensitive document uploads.

## Development

Backend: FastAPI + SQLite. Frontend: static HTML/CSS/JS. Home Assistant integration: Python custom integration. The app has no Ollama dependency.
