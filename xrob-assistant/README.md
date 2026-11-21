
## 0.4.23 — standalone Mikasa voice pack

- Rebuilds Mikasa's Telegram character with more natural pacing, varied greetings, warm reassurance, bolder playful flirting and shorter contextual action confirmations. Her style is suggestive rather than explicit, supportive rather than judgemental, and she remains transparent that she is an AI assistant.
- Moves Mikasa's social replies, command tone, morning report copy, playful motivation messages, routine/task/bill reminders, and meal check-in wording into `rootfs/mikasa_persona.py`.
- Adds a safe `/config/mikasa_persona.py` override. On XROB 0.4.23+, a voice-only update can be installed by replacing this single file in the add-on configuration directory and restarting the add-on; the bundled persona remains the fallback if the override fails to load.
- Exposes the active persona version and source in `/api/health`. No new database schema, external AI service, or change to task/meal/bill/fitness actions is introduced.
- This release is accompanied by a standalone `Mikasa-Persona-1.0.0.zip` update package and a raw `mikasa_persona.py` file, so the voice can be replaced separately after the first 0.4.23 install.

### Updating Mikasa without updating XROB

1. Keep XROB Assistant **0.4.23 or newer** installed.
2. Copy the standalone `mikasa_persona.py` into the XROB add-on's own configuration directory as `mikasa_persona.py`. The container sees that location as `/config/mikasa_persona.py` (on HAOS, this is normally the add-on folder under `/addon_configs/xrob_assistant/`, not the main Home Assistant `/config`).
3. Restart the XROB add-on. Check `/api/health`: `mikasa_persona_source` should report `config override`, and `mikasa_persona_version` should show the voice-pack version.
4. To return to the bundled voice, remove the override file and restart the add-on.

The standalone file controls Mikasa's personality, social copy, message templates and reminder tone. XROB continues to own factual data, command parsing, database operations and Telegram delivery.

## 0.4.22 — compact Overview and scheduled Mikasa messages

- Rebuilt the Overview into two independently sized columns (agenda + fitness on the left; suggested plate + daily progress on the right) to remove tall blank areas and make the dashboard denser and easier to scan.
- Tightened the Overview header, statistics and panel spacing without changing the agenda or meal data sources.
- Telegram display name is now **Mikasa** (no role suffix). The profile description and voice are more confident, cheeky and playfully flirtatious while remaining supportive and non-explicit.
- Added opt-in **Morning briefing** settings. Choose a daily 24-hour time; the report uses the shared agenda and the add-on timezone, and is sent at most once per local date.
- Added opt-in **Playful motivation** messages. Choose a few times per week or daily; each selected day receives at most one short, randomly timed nudge between 11:00 and 20:30 local time.
- Preferences and send-once markers are stored in the existing `telegram_state` table; no new database table, cloud service or external AI dependency is introduced. Telegram must be configured in the Home Assistant add-on options for scheduled messages to be delivered.
- On mobile, the Settings save action stays accessible above the fixed bottom navigation, including narrow 320px screens.

# XROB Assistant 0.4.23 · Home Assistant add-on

## 0.4.21 release

- Slightly tightens a few longer Mikasa social replies while retaining her affectionate, confident and supportive tone.
- Fixes the Settings layout so the Home Assistant integration card stays content-sized instead of stretching to match the longer Telegram guide.
- Prevents page-header action buttons such as **Add bill** from breaking onto multiple lines.
- Adds automated checks for static asset references, duplicate HTML IDs and the two layout regressions.

## 0.4.20 release

- Renames the Telegram secretary consistently to **Mikasa · XROB Secretary** in bot profile setup, greetings, natural replies, help text and project documentation.
- Keeps Mikasa affectionate, playful and supportive while trimming social replies and command introductions slightly; operational facts, command syntax and stored-data behaviour remain unchanged.
- Stabilises responsive layouts across desktop, tablet and narrow phones: the Overview keeps agenda-left / compact-plate-right on larger screens and stacks cleanly on phones; mobile navigation and bottom spacing are tightened.
- Adds a keyboard skip link, accessible navigation state, labelled dialog semantics and live status announcements; navigation now ignores invalid page targets instead of hiding every page.
- Checks HTML structure and IDs, CSS parsing and JavaScript syntax, and adds regression coverage for the Mikasa identity, concise tone, responsive layout and navigation accessibility.

## 0.4.19 release

- Reordered the Overview's main row so **Today's agenda** is on the left and **Today's suggested plate** is on the right. The plate panel now sizes to its own short content instead of stretching to match a tall agenda; its full actions and details remain available on the Meals page.
- Introduced **Mikasa**, a clearly identified AI personal-secretary character: warm, confident, affectionate, playful and lightly flirtatious, with supportive replies for greetings, stress, loneliness, setbacks, small wins and motivation, plus a consistent voice across commands and proactive reminders.
- Added a context-aware voice wrapper around the existing deterministic Telegram command engine. It changes presentation only; database writes, scheduling, validation, confirmation flows and command syntax remain in the original handlers.
- Telegram's bot profile is set to **Mikasa · XROB Secretary** with an AI description. Profile-update failures are non-fatal and do not stop polling.
- Expanded `/help` with a verified map of Telegram capabilities and clear boundaries: bill payment is status tracking only; adding/editing bills, routines, documents and gym sessions remains in the web app.

## 0.4.17 release

- Replaces the old single **Routines due** dashboard statistic with a complete **Today's agenda**: overdue and due tasks/routines/bills, today's events and workout plan, the daily meal, activity logs, plus bills entering their 3-day reminder window and documents entering their configured expiry window.
- Adds one backend `build_today_agenda()` source of truth. The overview dashboard, current-day Planner calendar, Telegram `/today`, and Home Assistant agenda sensors now consume the same ordered entries and status counts.
- Telegram reminders use shorter, warmer personal-secretary wording. The Today message uses compact human-readable lines grouped into **Your to-do**, **On the radar**, and **Done**, with a safe length limit.
- Adds cross-tab synchronization: a successful write in one open XROB tab prompts other tabs to refresh immediately. Background refresh and focus/visibility refresh continue to pick up Telegram and Home Assistant changes.
- Adds a safe `completed_at` task migration so tasks completed through the website, Telegram or Home Assistant appear under today’s Done recap. Existing completed tasks are not assigned guessed dates.
- Avoids duplicate Today entries when a logged gym session already satisfies its matching workout plan, and skips malformed legacy dates instead of failing the whole agenda. Adds regression tests for full-day aggregation, due-soon bill/document alerts, task completion migration, compact Telegram wording, shared dashboard/Planner/Home Assistant data and workout de-duplication. The full suite passes **74 tests**.

## 0.4.16 release

- Recurring bills now have a stable **bill-series identity**. Paying one occurrence records the payment and creates the next due occurrence inside the same series; the website no longer displays each occurrence as a separate unrelated bill.
- The Bills page displays one card per logical bill, with the next due occurrence and the most recent payment shown together. Previous payments remain accessible in an expandable Payment history section, and the latest payment can be corrected directly.
- Overview, Planner calendar, Telegram bill lists and bill reminders now use the same active unpaid occurrence per bill series. The summary counts each bill series once.
- Existing 0.4.15 databases are upgraded additively. Unambiguous adjacent recurring occurrences are linked automatically; ambiguous or one-off bills remain separate. Existing payment dates and amounts are preserved.
- Repeated payment actions cannot create a second open installment for a bill series. Multiple pre-existing unpaid occurrences remain visible under a review section rather than being silently hidden.
- Added regression coverage for legacy migration, occurrence linking, duplicate prevention, logical outstanding totals and recurring-bill history rendering.

## 0.4.15 release

- Every modal remains open when the user clicks the dark backdrop. The explicit close button remains available; tapping or clicking outside no longer discards an in-progress form.
- Bills now have **Edit bill** actions for both unpaid and paid entries. Paid entries also have **Edit payment**, which lets the user correct the amount and payment date/time without creating a duplicate payment or changing the due date.
- Telegram bill reminders follow a strict two-stage schedule: one reminder when 3 days remain (with catch-up if the add-on was offline), then a reminder at least every 4 hours from 1 day before the due date onward, including overdue days, until the bill is marked paid.
- Editing a bill's due date resets its old reminder schedule; deleting a bill removes its reminder log. The existing SQLite schema is reused, so no new database migration is required.
- Added regression tests for reminder cadence, paid-bill suppression, payment editing validation and reminder reset after due-date edits.

## 0.4.14 release

- Corrected routine reminder timing: the normal reminder and late reminder now occur on the same scheduled day. An unfinished opted-in routine advances to tomorrow after the late alert; overdue dates recover to today after a restart instead of remaining stuck in the past.
- Routine completion logs now record whether a check came from a weight entry, treadmill session or manual check-off. Deleting a weight or the last treadmill session for a day reverses only its automatic completion; explicit manual completion is preserved. Multiple treadmill sessions on one date are handled as one routine completion.
- Added Remove actions to every weight-history entry and treadmill session. Deletions refresh the graph, weekly averages, activity heatmap, overview statistics, planner history and related routine completion state from the same database.
- The backend now supplies the authoritative local date. The web app uses the add-on timezone for date labels, tomorrow counts, date prefill and planner navigation, keeping iPhone, Android and desktop aligned with Telegram.
- Reworked iPhone navigation into a bottom tab bar with safe-area padding, touch-sized actions, smaller-screen history layouts and iOS-friendly form controls.
- Added 12 routine/fitness regression tests, including old-database migration and activity-date edits. All 43 automated tests pass.

## 0.4.13 release

- Restores Telegram **Today**, **Meals**, **I ate it**, and **Not today** buttons alongside **Add weight**, **Add treadmill**, and **Fitness week**.
- Adds Telegram commands `/today`, `/meals`, `/yes`, `/no`, and `/choosemeal` for the same actions.

- Any edit in the weekly meal plan (assign, replace, skip or restore a day) now rebuilds the entire upcoming automatic plan from tomorrow, not just dates after the edited card. Today, past records, eaten plates, future manual choices and existing day-off markers are preserved.
- The replan recalculates each meal from its latest retained occurrence and configured eating interval. A meal is never suggested or manually accepted before its minimum gap, including when a future manual choice would be too close. Closed weekdays, occupied dates and Home-stock reservations can push it later, never earlier.
- “Not today” rollover now preserves the configured interval between repeats of the same meal, even when it must move around a closed or occupied date.
- Adding or removing a meal, changing its interval or weekday closures, changing its source, and changing Home stock rebuilds the upcoming automatic suggestions. The explicit Reset button replaces all pending choices from tomorrow onward and clears future day-offs; it never replaces today’s plate.
- Telegram's persistent menu keeps daily **Today** and **Meals** actions, explicit **I ate it** and **Not today** check-ins, and the fitness shortcuts. The Fitness week summary remains fitness-only; meal selection and check-ins use the shared scheduler.
- Added 31 automated regression tests covering cross-week replanning, repeat-interval conflicts, rollover, skip/restore, and all seven Telegram keyboard actions (in the 0.4.13 release).

## 0.4.11 release

- Fixed repeat scheduling after a manual choice. If a meal is set to **every 3 days** and was automatically planned for tomorrow, choosing it today now restarts its interval from today; the old automatic reservation is removed and the next plan is rebuilt from the saved rule.
- Editing a meal's repeat interval now rebuilds future automatic suggestions from the latest kept occurrence. Future manual choices and eaten history are preserved.
- Weekly edits and Telegram use the same re-anchoring rule. Telegram `/choosemeal` can replace today's pending plate, offers uniquely identifiable meal buttons, and asks before moving a later Home-plate reservation.
- Telegram Home-meal availability now uses the same future-reservation count as the website, including legacy automatic reservations.
- Added standard-library regression tests for repeat intervals, date closures, Home stock reservations, manual-choice protection, Telegram selection, and the one-plate-per-date database constraint.
- Replaced the long application guide with a short explanation of the app's decision rules and shared data flow.
- Telegram delivery now requires the Bot API's explicit `ok: true` acknowledgement before a reminder is marked sent; polling uses capped retry backoff during connection failures.

## 0.4.9 release

- Added a seven-day meal-plan card directly below Meal options, showing Monday through Sunday, meal name, source, calories and whether each day is planned, eaten, free or awaiting a choice.
- Added day-by-day editing: change a pending meal, schedule an option on an empty day, intentionally skip a day, or restore a skipped day. Eaten history is locked from accidental replacement.
- Added weekday exclusions to each meal option, so an outside restaurant can be marked unavailable on Sunday (or any selected weekday). The rule is enforced by automatic scheduling and manual selection, not just hidden in the UI.
- Editing a meal to exclude a weekday cancels its conflicting future pending reservations; it does not mark them eaten or consume Home stock. Existing records are preserved through an additive SQLite migration.
- A deliberate skipped day is stored separately so automatic schedule maintenance does not immediately refill that day. Choosing a meal or restoring the day clears the skip.
- Corrected pending-plate removal so it persists as a day-off choice instead of being immediately refilled by the automatic scheduler.
- Added `APP_LOGIC_GUIDE.md`, describing page behavior, shared records, scheduling, stock reservations, Telegram notifications, and known limitations.
- Updated sequence rollover so NO responses and overdue plates move past excluded weekdays, explicit day-offs, and occupied dates. Browser refreshes now discard stale responses, and API health/version reporting identifies this release as 0.4.9.

## 0.4.8 release

- Expanded the desktop workspace to use the full available browser width instead of stopping at a fixed maximum content width. Sidebar and content widths now stay coordinated across desktop and tablet breakpoints.
- Kept the workspace responsive on iPhone-sized screens, tablets and Windows browsers, including viewport safe areas, touch-sized controls and narrow-screen layouts. XROB remains a responsive web app rather than a native platform binary.
- Removed the meal-choice selector from the Overview card. When no plate is scheduled, Overview now links directly to Meals; the actual meal selector appears only on the Meals page, avoiding duplicate selector IDs and confusing split state.
- Updated Telegram **Today plate**, `/meals` and **Choose meal** so that when no plate exists for today, the bot opens a guided choice with available Outside meals and Home meals that have unreserved stock instead of showing a dead-end “no plate scheduled” message.
- Added a persistent **⬅️ Back** action to leave the meal-selection flow and return to the main Telegram menu. `/back` is also supported.
- Selecting a meal from Telegram or the Meals page creates the same daily plate record and invokes the same scheduler; Home stock is reserved consistently and is decremented only when the plate is confirmed eaten.
- The web app refreshes its current date, date prefill fields and data when the page becomes visible again, so it does not remain on yesterday after being left open overnight or switched away from on a phone.
- Restored the visible **Remove plate** action for a pending daily plate. It uses the existing safe cancellation endpoint: a pending plate is removed without marking it eaten or consuming Home stock; confirmed history cannot be deleted.
- Kept the existing automatic 14:00 suggestion and 22:00 check-in behavior. No new database tables or external dependencies are required for this release.

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
- **Bills:** edit bill details and payment records; Telegram reminders start 3 days before due, then repeat every 4 hours from 1 day before until marked paid. Recurring bills create the next occurrence when marked paid.
- **Routines:** repeat intervals, target reminders, optional late reminders and completion history.
- **Home Assistant:** optional native integration, summary sensors, calendar/todo support and service calls.

## Meals and automatic meal routines

1. Open **Meals** and add the meals you regularly cook.
2. Choose Home or Outside, then enter the name, kcal per plate, ingredients and eating interval. Mark any weekdays when the restaurant or meal is unavailable. For a Home meal, also enter how many plates are ready in your kitchen now.
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
- **Don’t schedule this meal on:** select closed or unavailable weekdays (for example, Sunday). The scheduler and manual selectors both enforce this rule.

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

Telegram only accepts your configured chat ID. The Telegram action engine remains a deterministic command-and-guided-flow interface: Mikasa adds a consistent AI secretary persona, supportive social replies and warmer phrasing, but this release does not connect a generative language model. The notification times use the add-on configured timezone (default `Africa/Casablanca`). Meal schedules are created from your saved meals; nutrition values are not invented.

## Install / update

1. Back up your Home Assistant add-on configuration and `/config/xrob_assistant.db` before upgrading.
2. Copy this folder to your Home Assistant local add-ons directory, for example `/addons/xrob-assistant/`.
3. Reload/check local add-ons in the Add-on Store.
4. Install/update XROB Assistant to **0.4.23**, review the add-on options and start it.
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
