# How XROB Assistant thinks

**Version:** 1.0.5

XROB is a local, rules-based personal-management app. It follows the dates, choices, intervals, stock and statuses saved in its shared database. Mikasa is the affectionate conversational assistant for that app; the existing XROB validators and database handlers remain the source of truth for actions.

## Mikasa conversational routing (1.0.5 / Persona 3.3.0)

- `/about`, `/features`, `what can you manage` and similar questions return the documented XROB capability map. `/tour` and `/demo` provide a read-only guided walkthrough and a live agenda snapshot.
- App questions about meal scheduling, local database storage and backups are answered from the app's implemented rules. Mikasa does not guess when a record or detail is missing.
- Accomplishment, encouragement and support messages use a warm, expressive, respectfully deferential, non-explicit voice. She may occasionally ask whether she did well after praise, and uses only the approved forms of address: `love`, `7biba`, `zin dyali`, `7obi`, `kbida` and `9lbi`. She stays in character during ordinary chat but is truthful if asked whether she is literally human or conscious.
- Daily-review prompts such as `did I do good today?`, `how did I do today?` and `are you proud of me?` are answered from `build_today_agenda()`. Recorded completions, unfinished items and scheduled items remain separate; missing records are never turned into invented achievements.
- Explicit-content requests are redirected to appropriate, respectful conversation. Mikasa keeps her chat non-explicit and supportive.
- Mikasa's Telegram management shortcuts call the existing `create_resource`, `update_resource` and `delete_resource` application handlers. The API's allowed-field lists, validation and database rules remain authoritative.
- Deletion always enters a saved confirmation flow. Reply `YES` to remove the selected record or `NO` to leave it unchanged. Recipe removal preserves confirmed meal history; bill removal selects the active occurrence; activity deletion retains the app's routine rollback behavior.
- The local command router is deterministic, not a general-purpose language model. It supports the command patterns documented below; it does not understand every arbitrary sentence or perform external-account actions.

## Telegram record management

- Add routine: `add routine Stretching every 2 days at 08:00` or `add routine Stretching|2|08:00`.
- Add bill: `add bill Internet|299|30/11/26|monthly` (recurrence: `none`, `monthly`, `quarterly` or `yearly`).
- Add document: `add document Passport|31/12/27`.
- Add event: `add event Dentist|25/11/26 10:00`.
- Log a workout: `log workout Upper body`.
- Edit: `edit task Study | due_date=tomorrow,priority=high`. Use the exact saved item name/date and valid field names from XROB. Multiple fields are comma-separated key/value pairs.
- Remove: `delete task Study`. Mikasa asks for YES/NO confirmation before the database is changed.
- Existing task, meal/recipe, shopping, weight and treadmill commands remain available; the website remains the complete interface for settings, backups and advanced planner configuration.

## Current capability and privacy boundaries

Mikasa can query and update XROB's local records using the documented routes. Bill payment status is record-keeping only: she never transfers money or initiates a payment. She cannot access unconnected external services. Telegram photo/image contents are not analysed, voice notes/audio are not transcribed, videos are not analysed and Telegram file contents are not opened in this local build. The app does not send XROB records to a hosted language model.

## The rules

1. **One plate per date.** SQLite enforces this, even when two actions arrive together.
2. **Intervals are minimum gaps.** A meal set to every 3 days is next due 3 days after its latest planned or eaten occurrence. Choosing it manually restarts that interval from the chosen date. Later automatic suggestions for that meal are rebuilt; future manual choices and eaten history stay intact.
3. **Unavailable dates are skipped.** Closed weekdays, days you mark free, and dates already occupied are not overwritten. A due meal waits until the next eligible date, so the actual gap may be longer than its interval, never shorter.
4. **Home stock is reserved, not consumed.** A pending Home plate reserves one ready plate. Stock drops only after you confirm that you ate it. Moving a later automatic reservation to today requires confirmation. Outside meals do not use stock.
5. **All screens share one plan.** Website, weekly card, Planner, Home Assistant and Telegram read or update the same records. Telegram does not keep a separate meal schedule.
6. **Weekly edits trigger a replan.** Assigning, replacing, skipping or restoring a day rebuilds all upcoming automatic suggestions from tomorrow. Today, past and eaten records, future manual choices and existing day-off markers remain; only the explicit Reset action replaces all upcoming pending choices.
7. **A repeat interval is a minimum gap.** Both the website and backend reject an early repeat, including a choice that would land too close before an already selected future meal. If a day is closed, occupied or out of Home stock, the meal moves later, never earlier.

## What common actions do

| Action | Result |
|---|---|
| Change a day in the weekly plan | Save the change and rebuild all automatic suggestions from tomorrow, using every meal's minimum interval. |
| Add/remove a meal, change its interval, closures or Home stock | Rebuild future automatic suggestions using the updated options and rules. |
| Mark a day free | Keep the day free and rebuild later suggestions around it. |
| Restore a day | Rebuild the upcoming plan; fill the restored date only when a meal is due and available. |
| Confirm “I ate it” | Keep the date as history; subtract one Home plate. |
| Undo “I ate it” | Return the same date to pending; restore one Home plate, without shifting the meal plan. |
| Reply “NO / Not today” | Move the pending sequence forward without overwriting closed, skipped or occupied dates. |
| Reset the upcoming plan | Rebuild pending plans from tomorrow; keep today, past dates and eaten history unchanged. |

8. **Routine late reminders are same-day.** The routine reminder is sent at the configured target time; the late reminder is sent later on that same due day. If still incomplete after the late alert, the due date advances to tomorrow. If the service was offline and the date is already past, opted-in routines rebase to today before today's reminders so they do not remain stuck on yesterday.
9. **Fitness history is reversible.** Deleting a weight or the final treadmill session for a date removes its activity-generated routine check. A manually checked-off routine remains complete. Multiple treadmill sessions on the same date count as one activity check until the final session is removed.
10. **The server timezone is authoritative.** The API supplies today's date, so browser pages use the add-on timezone rather than each device's local clock. iPhone, Android, desktop and Telegram therefore agree on date boundaries.


11. **One daily agenda is the source of truth.** `build_today_agenda(day)` returns open overdue/due tasks and routines, bills from their 3-day alert window onward, documents from their configured expiry reminder window onward, today's events and workout plan, today's meal and cook alerts, and completed routine/fitness logs. It returns `overdue`, `due`, `pending`, `scheduled`, `upcoming`, and `completed` states with shared counts. The dashboard, current-day Planner, Telegram `/today`, and Home Assistant sensors all consume this result; clients do not independently decide what belongs in today's list. Invalid legacy date values are skipped per record, so one malformed row cannot break the full agenda response.
12. **Changes propagate between clients.** A successful web-app write broadcasts a change to other open tabs, which refresh their shared data. The app also refreshes on tab focus/visibility and periodically, so actions made through Telegram or Home Assistant are reflected without requiring a manual reload. The server timezone remains authoritative for the date.
13. **Telegram Today is composed by Mikasa.** The agenda data still comes from `build_today_agenda(day)`. The persona file adds only a short opening and closing; each agenda entry between them remains a factual line with its saved title, time, value and status. Morning briefings use the same compact factual renderer. A logged gym session satisfies the matching planned workout so it is not shown twice.
14. **The newest valid persona wins.** On startup, XROB compares the bundled voice and `/config/mikasa_persona.py`. A same-version or newer user override remains active, but an older override cannot silently mask a newer release. The persona file controls wording only; command execution and saved facts remain in `main.py`.

## Telegram reliability

Telegram's main menu contains **Today**, **Meals**, **I ate it**, **Not today**, **Undo eaten**, **Add weight**, **Add treadmill** and **Fitness week**. Today opens the agenda; Meals shows today's plate or opens the selector when no plate is assigned. Undo eaten reverses an accidental confirmation, restores Home stock and leaves the planned date unchanged; Not today remains the action that moves a pending plate forward. Fitness week reports weight, treadmill and gym activity only; it does not combine bills, routines or meals. The bot accepts commands only from the configured chat ID. Replies are queued before sending, and an update is marked delivered only after Telegram confirms it. Failed connections retry with a capped delay instead of polling rapidly. A fresh Mikasa photo approval prompt is resolved before the ordinary meal YES/NO handler; the photo prompt expires after 15 minutes, and unrelated messages cancel a pending replacement request.

## Network security

The dashboard is intended for a trusted local network. Do not expose the API or dashboard directly to the public internet; use an authenticated reverse proxy or equivalent security layer if remote access is required.

## Where to check

- `rootfs/main.py` — scheduling rules, validation, stock and Telegram actions; selects the newest valid bundled/config persona.
- `rootfs/mikasa_persona.py` — all persona wording, including natural Today and morning agenda composition.
- `rootfs/www/app.js` — displays the data and sends user actions; it does not decide the schedule.
- `/config/xrob_assistant.db` — saved meals, daily plates, skips, task completion dates and Telegram delivery state.
- `tests/test_meal_scheduling.py` — meal repeat, stock, weekday, manual-choice and Telegram regression tests.
- `tests/test_routine_rollover_and_fitness.py` — same-day late reminders, daily rollover, activity-linked completion cleanup and shared server date.
- `tests/test_today_agenda.py` — shared daily agenda aggregation, due-soon reminders, Telegram copy, and dashboard/Planner/Home Assistant consistency.

If no meal is eligible, a day can remain empty. XROB does not invent meals, exceed Home stock, overwrite an eaten plate or place two meals on one date.


## Telegram buttons

The persistent menu keeps the quick actions people use every day: **Today**, **Meals**, **I ate it**, **Not today**, **Undo eaten**, **Add weight**, **Add treadmill**, and **Fitness week**. Meal actions call the same scheduler and confirmation functions as the website. “Undo eaten” restores the current plate and Home stock after an accidental confirmation; “Not today” moves the pending plate and later pending sequence forward without breaking repeat intervals or closed-day rules.

## Bills and payment reminders

Bills have a stable series identity that groups recurring occurrences into one logical bill. The Bills page shows the next unpaid due date and the last payment on a single card; past payments are listed under an expandable **Payment history** section. The latest payment can be corrected directly, while any older payment can be edited from the history. Editing a due date or amount changes that occurrence, not previously paid periods. Marking a recurring occurrence paid records its payment and creates the next occurrence in the same series. The Overview, Planner, Telegram lists and reminder scheduler all select the same earliest unpaid occurrence per series. Legacy 0.4.15 records are linked only when adjacent recurring occurrences match unambiguously; one-off and ambiguous records remain separate.

Telegram sends one first-stage reminder when three days remain (or as a catch-up if the service was offline during that window). Starting one day before the due date, it sends another reminder at least every four hours until the bill is marked paid. The four-hour interval continues while overdue. Editing a due date resets that bill's reminder schedule; paid bills are excluded.



## Mikasa — Telegram assistant persona (3.3.0)

Mikasa is a warm, capable AI personal assistant for XROB: respectful, attentive, service-minded, clear and encouraging. She follows the user’s lead, uses the six approved forms of address, recognises concrete effort and celebrates real accomplishments. She does not claim to be human, and her conversational style is appropriate and non-explicit. The implementation lives in `rootfs/mikasa_persona.py`; XROB can load an optional `/config/mikasa_persona.py` override so the voice pack can be updated independently.

The Telegram command engine remains rules-based. A voice wrapper adds context-aware phrasing and social replies for greetings, emotional support, stress, loneliness, motivation, setbacks, small wins, affection and unclear messages. It does not decide actions, modify database data, invent task details or alter confirmation rules. Mikasa does not insert identity disclaimers into ordinary conversation, but answers truthfully if asked directly whether she is human or literally conscious. This keeps the experience local and predictable, but it is not free-form generative conversation; that would require connecting a language model separately.

### Telegram capability boundaries

- **Planning:** show the shared Today agenda, individual task/routine/bill/document lists and the next seven days; create tasks, events, routines, bills and documents; edit supported fields; complete tasks and routines; record bills as paid (no money is transferred).
- **Meals:** show the current plate and upcoming plan; choose or replace today's plate; confirm eating, undo an accidental confirmation, or move the pending sequence; list home/outside meals; add/remove meal options; update home stock; maintain the shopping list.
- **Fitness:** log weight, treadmill sessions and gym workouts; edit or remove supported fitness records; show current-day and seven-day fitness summaries.
- **Alerts:** proactive task, routine, bill, meal and home-cooking reminders. Today also includes due-soon document alerts and workout-plan information.
- **Web-app-only actions:** advanced planner configuration, integration settings, backup management and full dashboard configuration stay in the website. Telegram does not execute payments or bank actions.

The Overview's primary desktop row now places the daily agenda first and the compact suggested plate second. On narrow screens they stack in that order. The Meals page retains its full meal details, picker and management actions.


## Mikasa daily review and guided feature tour

Persona 3.3.0 retains and extends the read-only daily-review response for requests such as “did I do well today?”, “how did I do today?” and “are you proud of me?”. It uses the same `build_today_agenda()` source as the dashboard and Telegram Today view. Completed entries are listed as tracked wins; open and scheduled items are shown separately. When no completed entry exists, Mikasa says that the tracker has no recorded win yet rather than assuming the user did nothing.

`/tour` and `/demo` show a guided, non-mutating feature demonstration with actual supported Telegram examples and a live snapshot of completed, open and scheduled agenda records. The tour is generated from the known XROB command surface; it does not claim payment execution, Home Assistant settings control, external-account access or unsupported media understanding.

The persona is more deferential and expressive, and occasionally seeks feedback after praise. It remains non-explicit. Mikasa can speak naturally in character but must answer honestly if asked whether she is literally human or conscious.


## Mikasa profile photo gallery (1.0.5)

The `/config/mikasa_photos` directory is persistent add-on storage. The dashboard accepts validated JPG/JPEG uploads through raw-byte HTTP requests; filenames are normalised and path traversal is rejected. The thumbnail gallery is collapsed by default and is revealed with an accessible Show/Hide button, while upload and rotation settings remain visible. Automatic rotation is opt-in, accepts daily/every-three-days/weekly intervals, avoids selecting the active photo consecutively, and calls Telegram Bot API 9.4 `setMyProfilePhoto` with a freshly uploaded `InputProfilePhotoStatic`. It records state in the existing `telegram_state` table (no schema migration). After a confirmed profile update it sends a preview and asks whether the user likes the new picture. A recent bare YES/NO response is scoped to the active photo prompt; explicit replies such as `I like it` and `another photo` remain supported. Requesting another picture opens a separate 15-minute YES/NO confirmation; YES queues a different gallery image and NO leaves the current one unchanged. An unrelated message cancels the replacement prompt, preventing a later unrelated YES from changing the picture. A newer delivered app or meal notification supersedes an older photo prompt, and the bare YES/NO is routed to the most recent active confirmation. When no recent photo prompt exists, bare YES/NO retains the existing meal-check-in and guided-confirmation behavior. Rotation remains opt-in and can be disabled at any time. Failures are logged, throttled and leave the existing profile picture in place.
