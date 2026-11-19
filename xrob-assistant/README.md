# XROB Assistant 0.4.7 · Home Assistant add-on

## 0.4.7 release

- Kept **Today's plate** on the Meals page. The Planner now focuses on the selected date: the date heading and its other scheduled/recorded items appear above the calendar; the separate “All planner items” list is not shown on Meals.
- Added a guided Telegram `/deletemeal` flow with a meal-name lookup and explicit YES/NO confirmation. It uses the same safe deletion behavior as the website: pending plates are cancelled, while confirmed eating history is retained.
- Fixed guided Telegram conversation routing so YES/NO replies inside an active Add Meal, stock, shopping or delete flow cannot accidentally confirm a daily plate. YES/NO is used for meal check-in only when no guided form is active.
- Added contextual Telegram reply keyboards: Home/Outside, common eating intervals, ready-plate counts, meal choices for stock/removal, and YES/NO confirmation buttons.
- Added clearer validation prompts for incomplete or misplaced answers and updated the Telegram command menu and in-app guide.
- Consolidated recipe removal into one backend operation shared by the website API and Telegram, reducing the risk of different delete behavior between interfaces.
- Cleaned small duplicate/unreachable code paths found during the Telegram and backend review.

## 0.4.6 release

- The Overview card counts all items scheduled for tomorrow (events, open tasks, routines, daily plates, cooking reminders and unpaid bills).
- The Planner selected-day area displays the day's meal and confirmation state above the calendar; confirmed eaten plates are highlighted in green.
- Added safe removal of pending daily plates without marking them eaten or decreasing ready-plate stock.
- Outside meals can be saved without kcal or ingredient details. Home meals still require at least one ingredient.
- Replaced the Meals overview's “Next suggested plate” stat with the number of shopping-list items left to buy.


## 0.4.4 release

- Unified the visual system across desktop, tablet and phone, with responsive layouts, touch-friendly controls, keyboard focus states, reduced-motion support and iPhone safe-area handling.
- Updated the web app metadata for iPhone/iPad Home Screen use and Windows browser installation where supported. XROB remains a responsive web app, not a native iOS/Windows binary.
- Replaced the separate Home/Outside meal sections with **All options**, **Home meals** and **Outside options** filters. All saved meals remain visible, including Home meals with zero ready plates; you can change a meal's source by editing it.
- Added a per-routine **Move to tomorrow if still incomplete** option. It is shown only when a late reminder time is configured.
- When enabled, an overdue routine moves to tomorrow only after its late Telegram reminder is successfully delivered and it is still incomplete. Completing it from the website or Telegram updates the same schedule and prevents an unnecessary rollover.
- The rollover checkbox is available only while a late reminder time is configured. Routines without a late reminder stay on their scheduled date.

## 0.4.3 release

- Split Meals into **Home meals** and **Outside meals**. Only Home meals track ready plates; the count can be changed in the page or Telegram.
- Home meal routines use the selected eating interval and available plates. Once the final ready plate is confirmed eaten, XROB creates a cooking reminder for the following day and pauses that meal until stock is replenished.
- Telegram-created meals are added to the automatic schedule immediately. `/homemeals`, `/outsidemeals` and `/stock Meal name|3` manage the two meal types and kitchen stock.
- When a cooking reminder is due, Telegram sends a message with a ready-plate update shortcut.
- Removed the separate Meal Planner tab and manual generate-plan button.
- Meal routines automatically fill the Planner calendar using the selected eating interval and current Home-meal plate stock; Outside meals do not use stock.
- Redesigned the Meals page as separate Home and Outside meal collections, with a ready-plate total for home-cooked food.
- Moved the shopping checklist and the complete planner-items list into the Meals page; Planner is focused on the calendar.
- The Add Meal dialog stays open when clicking outside; use its Cancel (×) control or Save.
- Telegram sends the suggested plate at **14:00** and asks whether it was eaten at **22:00** in the configured timezone.
- Unanswered plates roll forward at the start of the next day, moving later plates with them.
- Added explicit `tzdata` to the image so `Africa/Casablanca` reminder times work reliably.

A local-first personal dashboard for planning, fitness, bills, routines and meal planning. Records are stored in SQLite at `/config/xrob_assistant.db`.

## Meal Manager foundation (0.4.0)

- **Simple Meals page:** separate Home and Outside meals. Each meal has a name, kcal per plate, ingredient names and eating interval; Home meals additionally track ready plates now.
- **One plate per day:** the scheduler observes each meal's eating interval and available batch plates, and never places two suggested plates on the same date.
- **Overview:** today's suggested plate replaces the old upcoming planner-items panel.
- **Planner:** daily plates appear on calendar dates and in the planner list, with their Telegram confirmation status.
- **Telegram check-in:** the bot asks whether you ate the scheduled plate. YES confirms it. NO moves that plate to the next day and shifts every later planned plate by the same delay, preserving the sequence.
- **Plain shopping checklist:** shopping items show only their names and checked state. Collect it from the upcoming meals' ingredient names or add items manually.
- **Safer shopping:** purchased items can be re-added if a later plan needs them again.
- **Additive upgrade:** old records and legacy 0.3.0 meal-plan data remain in the database; retired recipe fields are no longer exposed in the new Meal Manager.

## Existing features

- **Planner:** events, tasks and reminders in one place.
- **Fitness:** weight records, treadmill sessions, gym logs, activity history and weight trends.
- **Bills:** unpaid tracking and reminders; recurring bills create the next occurrence when marked paid.
- **Routines:** repeat intervals, target reminders, optional late reminders and completion history.
- **Home Assistant:** optional native integration, summary sensors, calendar/todo support and service calls.

## Meals and automatic meal routines

1. Open **Meals** and add the meals you regularly cook.
2. Choose Home or Outside, then enter the name, kcal per plate, ingredients and eating interval. For a Home meal, also enter how many plates are ready in your kitchen now.
3. XROB automatically maintains a rolling meal schedule. There is no Generate plan button or separate Meal Planner page.
4. Suggested plates appear on the Overview and on the correct dates in the **Planner** calendar.
5. The Meals page contains today's plate, meal options and the shopping checklist. Add grocery names manually or collect unique ingredients from upcoming planned meals. The Planner shows the selected date's other scheduled and recorded items above the calendar.
6. Telegram sends **today's suggested plate at 14:00** and asks **whether you ate it at 22:00**. Reply **YES** to confirm or **NO** to move this plate and later plates forward by one day.
7. If a plate is still unconfirmed when the next day arrives, the schedule rolls that overdue plate onto today and moves later plates forward, so the calendar does not remain stuck in the past.

### Meal fields

- **Meal type:** Home (cooked and stock-tracked) or Outside (no kitchen stock).
- **Name:** the meal's title.
- **Kcal per plate:** calories for one plate (optional when unknown).
- **Ingredients:** names only; one per line. Required for Home meals and optional for Outside meals.
- **Eat every (days):** for example, `1` means daily, `2` means every other day and `3` means every three days.
- **Plates ready now:** Home meals only. Update this count when you cook more or eat plates outside the planned routine. Each planned Home plate reserves one available plate. When stock is exhausted, the next-day cooking reminder appears in the Planner on its due date and is sent through Telegram; scheduling resumes after stock is replenished.

## Telegram setup and commands

1. Create a bot with `@BotFather` and copy its token.
2. Get your numeric Telegram chat ID.
3. In the add-on Configuration, set `telegram_bot_token` and `telegram_allowed_chat_id`, save and restart.
4. Open your bot and send `/start`.

Meal commands:

- `/meals` — today's suggested plate.
- `/mealplan` — the next seven days of planned plates.
- `/addrecipe` — add a Home or Outside meal through a short guided conversation.
- `/homemeals` and `/outsidemeals` — list each meal type; Home meals include ready-plate counts.
- `/stock Meal name|3` — update how many ready plates you have for a Home meal.
- `/deletemeal` — remove a meal option with a guided YES/NO confirmation; confirmed eaten history is kept.
- `/shopping` — show the item-name checklist.
- `/additem` — add one shopping item by name.
- `bought tomatoes` — mark an item as purchased.
- At **14:00** the bot sends today’s suggested plate. At **22:00** it asks whether you ate it. Reply `YES` or `NO`.
- When a Home meal runs out of ready plates, Telegram reminds you to cook again on the due date and includes a `/stock` shortcut.
- `/cancel` — cancel a guided entry.

Existing commands are preserved, including `weight 82.4`, `treadmill 30`, `task Study tomorrow at 18:00`, `task done Study`, `done Shower`, `paid Electricity`, `today`, `tasks`, `bills`, `routines`, `fitness` and `week`.

Telegram only accepts your configured chat ID. The bot is a command-and-guided-flow interface, not an AI assistant. The notification times use the add-on configured timezone (default `Africa/Casablanca`). Meal schedules are created from your saved meals; nutrition values are not invented.

## Install / update

1. Back up your Home Assistant add-on configuration and `/config/xrob_assistant.db` before upgrading.
2. Copy this folder to your Home Assistant local add-ons directory, for example `/addons/xrob-assistant/`.
3. Reload/check local add-ons in the Add-on Store.
4. Install/update XROB Assistant to **0.4.7**, review the add-on options and start it.
5. Open `http://HOME_ASSISTANT_IP:8090` or use the add-on Web UI button.

The app keeps existing records and adds the meal source, ready-plate count, next routine date, cook-reminder and Telegram notification fields to the existing database on startup. Existing tasks, events, fitness, bills, routines, Telegram state, recipes and old meal-plan rows are preserved. The Meals page has no archive control. Back up before updating.

## Phone and desktop access

XROB uses a responsive web interface that adapts to iPhone-sized screens, tablets and Windows desktop browsers. On iPhone/iPad, open XROB in Safari and use **Share → Add to Home Screen** when the browser offers it. On Windows, use the browser's **Install app** option (for example in Edge or Chrome) when available. Installation availability depends on the browser and the URL security context; the normal browser interface remains supported either way. The project does not include native iOS or Windows executables.

## Routine late-reminder rollover

When editing a routine, set an optional **Late reminder** time. Only then does the **Move to tomorrow if still incomplete** checkbox appear. When checked, XROB sends the late Telegram reminder on an overdue day; after Telegram confirms delivery, the still-open routine is moved to the next day. If you complete it from either the website or Telegram, the completion is recorded normally and the next interval is calculated from that completion date. Leave the late reminder blank to keep the routine on its original due date until completed.

## Optional Home Assistant custom integration

Copy `homeassistant_custom_component/xrob_assistant` to `/config/custom_components/xrob_assistant/`, restart Home Assistant, and add **XROB Assistant** under Devices & services. Configure the add-on API URL, for example `http://HOME_ASSISTANT_IP:8090`.

## Project layout

- `rootfs/main.py`: FastAPI API, SQLite schema/migrations, reminder scheduler and Telegram bot.
- `rootfs/www/`: responsive dashboard and Meal Manager UI.
- `homeassistant_custom_component/xrob_assistant/`: optional native Home Assistant integration.
- `config.yaml`, `Dockerfile`, `build.yaml`, `rootfs/run.sh`: add-on packaging.

## Privacy and limitations

The app is local-first. All core records are stored in SQLite. Telegram messages are sent only to the configured chat ID. The web recipe-search feature has been removed. Nutrition values are user-entered, not estimated. No cloud database, payment processing or external AI service is used. The API is intended for a trusted local network; do not expose port 8090 publicly without a secure access layer.



## Meal types and ready-plate stock (0.4.3)

Meals are separated into **Home meals** and **Outside meals**. Home meals track the number of plates currently ready. Each scheduled plate reserves one ready plate; confirming it as eaten reduces stock. When stock reaches zero, XROB creates a cooking reminder for the next day, shows it in the Planner's selected-day summary, sends it through Telegram on the due date, and pauses further Home-meal scheduling until stock is replenished. Update ready plates from the Meals page or Telegram with `/stock Meal name|3`. Outside meals follow their eating interval without kitchen stock tracking. The Meals page contains today's plate, meal options and the shopping list. The Planner shows the selected date's scheduled and recorded items above its calendar; it does not show a separate global all-items list. Meal options can be removed from the Meals page or with `/deletemeal`; confirmed eating history is retained.

Telegram commands: `/homemeals`, `/outsidemeals`, `/addrecipe`, `/stock`, `/stock Meal name|3`. During `/addrecipe`, choose HOME or OUTSIDE. Home meals additionally ask how many plates are currently ready.
