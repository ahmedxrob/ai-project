# XROB Assistant — Application Logic Guide

**Version:** 0.4.9  
**Purpose:** This guide describes how the application currently works, where its data lives, how the pages share data, and what to check when something appears incorrect. It documents the implemented behavior; it is not a promise that the app uses machine learning or external AI.

## 1. System overview

XROB Assistant is a local-first personal dashboard. The main components are:

| Component | File / location | Responsibility |
|---|---|---|
| Web interface | `rootfs/www/index.html` | Page structure, forms, navigation, and page containers |
| Browser logic | `rootfs/www/app.js` | Loads API data, caches shared records, renders pages, validates user input, and sends mutations |
| Browser styling | `rootfs/www/style.css` | Responsive layout and visual components |
| API and business rules | `rootfs/main.py` | FastAPI routes, validation, scheduling, Telegram, and SQLite operations |
| Persistent database | `/config/xrob_assistant.db` | User records, schedules, notification state, and Telegram processing state |
| Add-on configuration | `config.yaml` | Version, port, Telegram credentials, and application timezone |
| Home Assistant integration | `homeassistant_custom_component/xrob_assistant/` | Optional sensors, calendar, to-do list, and service actions |

The browser does not write directly to SQLite. It calls the FastAPI endpoints under `/api/`. The API validates the request and applies the rules before committing changes to SQLite. The database is the source of truth; the browser cache is a temporary display copy.

```text
Browser pages and forms
        │  GET /api/... and POST/PATCH/DELETE /api/...
        ▼
FastAPI routes and validation (rootfs/main.py)
        │
        ▼
SQLite database (/config/xrob_assistant.db)
        │
        ├── Background meal scheduler
        ├── Telegram reminder / command loops (when configured)
        └── Home Assistant integration polls the API
```

## 2. Pages and how they are connected

All pages use the same API and shared browser cache. A change made on one page can therefore appear on another page after the next successful refresh.

| Page | Main information and actions | Shared data it affects |
|---|---|---|
| **Overview** | Today’s date, attention items, routines, fitness snapshot, upcoming bill, today’s meal | Tasks, bills, routines, fitness logs, daily meals, cook reminders, events |
| **Planner** | Calendar view of events, tasks, routines, daily plates, and cooking reminders; selected-day details | Events, tasks, routines, daily meals, meal cook reminders |
| **Fitness** | Weight history and trends, treadmill sessions, gym logs, workout plan | Weights, treadmill, workouts, workout plan, routine logs |
| **Bills & payments** | Unpaid bills, outstanding total, due dates, mark-paid action | Bills and future recurring bill records |
| **Routines** | Recurring routines, completion history, normal and optional late reminders | Routines, routine logs, notification log |
| **Meals** | Today’s plate, saved meal options, this week’s seven-day plan, stock, shopping checklist | Recipes, daily meals, skip dates, cook reminders, shopping items |
| **Settings** | Telegram setup guide, Home Assistant connection instructions, usage and data notes | Add-on configuration and optional integration setup; credentials are not edited in the web form |

### Navigation and refresh behavior

- Navigation changes the visible page; the pages do not maintain separate databases.
- `refresh()` loads summary and the shared data resources together, then calls the render functions for the pages.
- The browser refreshes data after actions, when the page becomes visible again, and periodically while it remains open.
- Each refresh request receives a sequence number. If a newer refresh starts while an older one is still returning, the older response is discarded so stale data cannot overwrite the latest cache.
- A successful write is followed by a refresh so other pages can reflect it. If the API is unavailable, the current screen reports the connection problem rather than pretending that a change was saved.

## 3. Meal system: one schedule shared by every page

The Meals page, Overview, Planner, and Telegram all use the same `daily_meals` records. There is not a separate weekly-card-only schedule.

### 3.1 Meal option fields

A saved meal (`recipes`) has these main fields:

- **Name** — the meal or restaurant name.
- **Meal source** — `home` or `outside`.
- **Calories per plate** — optional, user-entered data. The app does not estimate missing meal calories.
- **Ingredients** — cleaned ingredient names. Home meals require at least one ingredient; Outside meals may omit them.
- **Eat every (days)** — the recurrence interval, from 1 to 90 whole days.
- **Plates ready** — physical ready-plate count for Home meals. Outside meals always have zero stock.
- **Excluded weekdays** — days on which this meal must not be scheduled.
- **Next routine date** — when the meal next becomes eligible for automatic scheduling.

Weekday values are stored as JSON integers using Python's weekday numbering: **Monday = 0, Tuesday = 1, Wednesday = 2, Thursday = 3, Friday = 4, Saturday = 5, Sunday = 6**. The API returns the parsed integer list as `excluded_weekdays`.

The form permits any combination of excluded weekdays, but it rejects all seven days being excluded. A meal must remain available on at least one day. An excluded weekday is checked by the scheduler, the Overview's manual chooser, and the weekly-plan assignment endpoint.

### 3.2 Automatic scheduling

The background scheduler runs at application startup and every five minutes. It maintains a rolling schedule up to 45 days ahead.

For each date without an existing plate or a saved day-off marker, it:

1. Finds active meals whose `next_routine_date` is on or before that date.
2. Counts existing pending plates for each meal as reservations.
3. Keeps Outside meals eligible without stock checks. A Home meal is eligible only when `plates_ready` is greater than its pending reservation count.
4. Removes meals excluded on that date's weekday.
5. Orders remaining candidates by next due date, then prefers Home over Outside when the due dates tie, then uses meal name and ID as stable tie-breakers.
6. Assigns one eligible meal to that date, then advances that meal’s next due date by its eating interval.

If no meal is eligible, the date remains empty. The scheduler does not invent a plate or nutrition data. Because the schedule is stored in `daily_meals`, the same result is shown on the Overview, Planner, Meals page, and Telegram.

The database enforces one daily plate per date with a `UNIQUE(plan_date)` constraint. The app’s current meal model is **one plate per day**, not separate breakfast, lunch, and dinner slots.

### 3.3 This week's meal-plan card

The card appears immediately below **Meal options** and shows Monday through Sunday for the browser's current week.

- A pending plate can be replaced with another eligible Home or Outside meal.
- An empty date can be assigned a meal.
- A day can be deliberately skipped.
- A skipped day can be restored to automatic planning.
- Past dates are read-only. A plate already confirmed as eaten is also locked.
- Home options are filtered by unreserved ready stock; Outside options do not need stock.
- A meal excluded on that weekday is not offered in the day's selector.

The card writes to `daily_meals` and `meal_schedule_skips`; it does not create a second plan table.

### 3.4 Skip and restore behavior

The `meal_schedule_skips` table stores dates the user explicitly wants to leave free.

- **Skip day** removes a pending plate, if present, and adds the date to `meal_schedule_skips`.
- Skipping does not mark the plate as eaten and does not decrement Home stock.
- The automatic scheduler checks the skip table and will not refill that date.
- **Restore day** removes the skip marker and runs schedule maintenance. The scheduler may then add an eligible plate.
- Manually assigning a meal to a skipped date also clears that date's skip marker.

This distinction is important: an empty date without a skip marker is available for automatic planning; an empty date with a skip marker is intentionally kept free.

### 3.5 Home stock and reservations

`recipes.plates_ready` is the number of physical Home plates available. A pending Home plate reserves one plate, but does not consume it yet.

- The scheduler and manual assignment compare ready stock with pending reservations before assigning a Home plate.
- Replacing or skipping a pending plate releases its reservation; it does not reduce stock.
- Confirming **YES / eaten** reduces Home stock by one.
- Confirming an Outside meal does not change stock.
- If stock is reduced below the number of existing pending reservations, the app removes the latest pending reservations until the count fits the physical stock. Confirmed eating history is not removed.
- When stock is exhausted, the app creates a cooking reminder for the next day. The reminder appears in the Planner and can be sent through Telegram. Replenishing stock closes open cooking reminders.

### 3.6 Confirming a plate and moving the schedule

- **YES** changes a pending plate to `eaten`, records its confirmation timestamp, and consumes one Home plate if applicable.
- **NO** keeps it pending and moves it forward. Later pending plates are shifted as a sequence so that one daily plate remains assigned to each date without overwriting another record.
- During a shift, the app checks each meal’s excluded weekdays, explicit skipped days, and dates occupied by fixed records. It moves a restaurant plate past a closure day rather than placing it on a closed day.
- At startup and on the five-minute maintenance cycle, an unanswered overdue pending plate is rolled forward to today. Later pending plates move with it.
- Confirmed `eaten` rows are retained as history and are not shifted or deleted by these actions.

When a recipe’s excluded weekdays are edited, future pending plates for that recipe which now fall on a newly excluded weekday are cancelled. Eaten history is kept. Other eligible meals may fill an available date during the next maintenance run.

When a meal option is removed, pending suggestions are removed. If the recipe has confirmed history or legacy meal-plan references, the option is deactivated instead of hard-deleted so its historical references remain valid.

## 4. Shopping list logic

- The shopping list stores item names, checked state, and a source label.
- Manually added items are stored as manual entries.
- **Add ingredients from planned meals** reads ingredient names from pending daily plates within the selected date range.
- Ingredient names are normalized for comparison and duplicate names are collapsed.
- Items already on the unchecked shopping list are not added again. Purchased/checked items do not block a later re-add if a future meal needs that ingredient again.
- The list does not calculate ingredient quantities, pantry levels, serving-size scaling, or prices. It is a name checklist, not an inventory or purchasing integration.

## 5. Planner, tasks, events, and reminders

- **Events** have a required start date/time and an optional end. An end must be later than the start.
- **Tasks** may have a due date and optional 24-hour reminder time. A reminder time requires a due date.
- The Planner combines event and task records with meal schedule entries, cooking reminders, and routine information for the selected date.
- Notification records use a unique `(kind, item_id, reminder_date)` key to prevent the same reminder from being sent repeatedly for the same date or time.
- Date-only overdue task reminders are sent once per day while the task remains open. Timed task reminders are keyed to the due date/time.

## 6. Fitness logic

### Weight

- A weight must be between 20 kg and 350 kg.
- Only one weight record is allowed per date. The API performs a write-locked check, and SQLite triggers also prevent duplicate dates at the database level.
- Adding a weight entry automatically completes matching Weight/Weigh-in routines for that activity date.
- Backdated records are retained; they do not move a routine's current completion date backwards when a newer completion already exists.

### Treadmill

- The current form and Telegram entry record duration. The application uses its configured estimate of 5 km/h and 0% incline.
- Distance is calculated as `5 × duration_minutes ÷ 60`.
- Calories are estimated using the standard formula coded in the app: `3.5 × 3.5 × body_weight_kg ÷ 200 × duration_minutes`.
- The most recently recorded body weight is used; if none exists, the estimate uses 70 kg.
- The page labels the result as estimated. It is not a measurement from a connected treadmill.
- Adding a treadmill record automatically completes matching treadmill routines for that activity date.

### Gym and workout plan

- Gym records are dated workout entries.
- The weekly workout plan is a separate reference list of weekday/name entries; it does not automatically create a workout log.

## 7. Bills and recurring payments

- Bills are records with an amount, due date, recurrence, reminder lead time, and status.
- The app does not initiate bank transfers or pay bills. **Mark paid** records the user's confirmation.
- For monthly, quarterly, and yearly recurrence, marking a bill paid creates the next occurrence.
- The recurrence calculation preserves the original day-of-month anchor where possible and clamps to the last calendar day when a month is shorter (for example, a 31st can become the last day of February).
- If a bill is paid late, the next occurrence advances until it is strictly after today.
- The original bill is marked paid; the next occurrence is a separate unpaid record.

## 8. Routines

- A routine has an interval, last completion date, next due date, and normal reminder time.
- The optional late reminder must be later than the normal reminder time.
- Late reminder rollover can only be enabled when a late reminder time is configured.
- Completing a routine records a date in `routine_logs`, updates the last completion date, and calculates the next due date from the latest completion.
- Weight and treadmill routines can be completed automatically when the matching activity is logged.
- If late rollover is enabled, the unfinished routine moves to tomorrow after the late Telegram reminder is delivered. A completion made before the rollover is applied is not overwritten.

## 9. Telegram logic

Telegram runs only when both the bot token and allowed chat ID are configured. Incoming messages are restricted to the configured chat ID.

### Meal messages

- At **14:00**, Telegram sends today's pending suggested plate, if there is one.
- At **22:00**, Telegram asks whether the plate was eaten.
- The scheduler records separate sent dates for the suggestion and check-in, preventing repeat sends for the same day.
- The app does not send a late 14:00 suggestion after 22:00 if it was offline during the afternoon.
- `YES` confirms the plate; `NO` moves the pending sequence forward while respecting excluded weekdays and skipped dates.
- `/choosemeal` only offers meals that are open today; excluded weekdays are honored for Outside and Home meals, and Home meals without an unreserved ready plate are hidden.

### Meal commands

| Command | Behavior |
|---|---|
| `/meals` | Show today's plate or available choices if there is no plate |
| `/choosemeal` | Choose a meal for today |
| `/mealplan` | Show the next seven days of planned plates |
| `/homemeals` | List Home meal options, ready stock, and excluded weekdays |
| `/outsidemeals` | List Outside meal options and excluded weekdays |
| `/addrecipe` | Add a meal through guided questions, including weekdays when it is closed |
| `/stock` | Update ready plates for a Home meal |
| `/deletemeal` | Remove a meal option after confirmation; preserve history where needed |
| `/shopping` | Show the shopping checklist |
| `/additem` | Add a shopping-list item |
| `bought tomatoes` | Mark a matching item as purchased |
| `/cancel` | Cancel the current guided flow |

Other commands cover tasks, routines, bills, weight, treadmill, and weekly summaries. The bot is a command-and-guided-form interface; it does not use an LLM to infer or generate meals.

Incoming Telegram updates and prepared replies are persisted in SQLite. A reply is stored before it is sent, so a temporary Telegram send failure can be retried without re-running the command action a second time.

## 10. Home Assistant integration

The optional custom integration polls the local API approximately once per minute and exposes:

- Sensors for weight, unpaid bills, unpaid bill total, expiring documents, open tasks, today's events, and next routine.
- A calendar entity backed by `/api/events`.
- A to-do entity backed by `/api/tasks`.
- Services to create an event, record a weight, and complete a routine.

The integration is optional. The web app and Telegram do not require the custom integration to be installed.

## 11. Important API endpoints

The browser uses the generic resource routes for normal create/read/update/delete operations and dedicated routes for actions which need business rules.

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | API/database health and app version |
| `GET /api/summary` | Overview summary values |
| `GET /api/{resource}` | Read supported resources such as `events`, `tasks`, `recipes`, `daily_meals`, and `shopping_items` |
| `POST /api/{resource}` | Create a validated record |
| `PATCH /api/{resource}/{id}` | Update a validated record |
| `DELETE /api/{resource}/{id}` | Delete a record with resource-specific protections |
| `POST /api/daily-meals/select` | Select today's plate from the Meals page when no plate is already assigned |
| `POST /api/daily-meals/assign` | Assign, replace, or skip a date in the weekly meal plan |
| `POST /api/daily-meals/restore` | Remove a day-off marker and resume automatic planning |
| `GET /api/meal-schedule-skips` | Read explicitly skipped dates for a date range |
| `POST /api/daily-meals/{id}/confirm` | Confirm a pending plate as eaten or move it forward |
| `POST /api/recipes/{id}/stock` | Update Home ready-plate stock |
| `POST /api/meals/shopping-list` | Collect unique ingredients from pending meals |
| `POST /api/routines/{id}/complete` | Complete a routine |
| `POST /api/bills/{id}/pay` | Mark a bill paid and create its next recurrence if applicable |
| `GET /api/ha/state` | Compact data for Home Assistant sensors |

## 12. Database tables that matter most

| Table | Role |
|---|---|
| `events`, `tasks` | Planner records |
| `weights`, `treadmill`, `workouts`, `workout_plan` | Fitness history and workout plan |
| `bills`, `documents` | Financial and expiry tracking |
| `routines`, `routine_logs` | Routine schedule and completion history |
| `recipes` | Saved Home and Outside meal options, stock, intervals, weekday exclusions |
| `daily_meals` | The single source of truth for scheduled and eaten daily plates |
| `meal_schedule_skips` | Dates deliberately left without a plate |
| `meal_cook_reminders` | Reminders to prepare more Home meal plates |
| `shopping_items` | Shopping checklist |
| `notification_log` | Deduplication of scheduled notifications |
| `telegram_state`, `telegram_inbox` | Telegram polling offset and durable message/reply state |
| `meal_plans`, `meal_batches`, `exercises` | Legacy/compatibility data retained to avoid discarding older records |

## 13. Diagnostics and expected limitations

### Quick checks when a meal looks wrong

1. Confirm the saved meal is active and has the correct Home/Outside type.
2. Check its **Excluded weekdays** setting. The API value should be an array of weekday numbers from 0 (Monday) to 6 (Sunday).
3. For Home meals, check `plates_ready` and compare it with pending `daily_meals` reservations for that recipe.
4. Check whether the date is present in `meal_schedule_skips`; a skipped date is intentionally not auto-filled.
5. Check `daily_meals` for the date and status. There can be only one row per date.
6. Check add-on logs for `Meal schedule maintenance error`, `Reminder scheduler error`, or Telegram API errors.
7. Check `GET /api/health` for API/database connectivity and the reported version.

### Deliberate limitations

- One planned plate per day; no separate breakfast/lunch/dinner scheduling in the current Meal Manager.
- The weekly card shows the current Monday–Sunday week. It is not an arbitrary multi-week planner.
- Meal calories and ingredient names are user-entered. The app does not calculate macros or infer missing nutritional values.
- The shopping list does not know what is already in the pantry and does not calculate ingredient quantities.
- Treadmill distance and calories are estimates based on the app's fixed 5 km/h, 0% incline assumptions.
- Telegram requires an external Telegram connection and only sends to the configured allowed chat ID.
- The local API is designed for a trusted network and does not implement user login/session authentication. Do not expose it directly to the public internet; use a secure access layer if remote access is required.
- There is no cloud database or external AI service. The app's “smart” behavior is implemented through deterministic scheduling, validation, shared records, notification deduplication, and cross-page refresh logic.

## 14. Upgrade and data safety

The 0.4.9 migration adds `recipes.excluded_weekdays_json` and creates `meal_schedule_skips` if they do not exist. It is additive: existing recipe, schedule, fitness, planner, bills, routine, Telegram, and legacy records are not intentionally cleared by this migration.

Before installing an update, back up the Home Assistant add-on configuration and `/config/xrob_assistant.db`. After an update, verify `/api/health`, open Meals, check the current week's card, and verify an existing Home meal's ready-plate count before relying on automatic scheduling.
