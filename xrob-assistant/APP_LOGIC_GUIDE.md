## 0.4.26 — Mikasa responsive Telegram handling

- Telegram text and captions are passed to Mikasa's conversational/command router. Common media-only messages receive a clear persona response rather than being silently skipped; audio and image/video analysis are not claimed or fabricated.
- Command matching normalises casing, whitespace and trailing punctuation. The Today agenda recognises common natural-language variants and can interrupt an active guided flow safely.
- The standalone `rootfs/mikasa_persona.py` owns the social fallback, unsupported-attachment wording and friendly processing-error messages. Deterministic XROB handlers remain responsible for actions and database changes.
- Settings shows a read-only missed-message log with grouped occurrence counts and a downloadable plain-text export. The user can send the exported text with a future source update; no manual reply-writing or phrase teaching is required.
- Existing SQLite schema and data remain intact. Captured text stays local; the export is generated on demand. No AI service or external runtime dependency is added.

## 0.4.25 — Mikasa local learning inbox (superseded)

- Added the original fallback-message capture table and manual phrase-teaching flow. The capture table is retained for backwards compatibility, while the 0.4.26 interface now uses it as a text-only missed-message log.


## 0.4.24 — Mikasa loader hotfix

- Bundled Mikasa is now loaded directly from the path beside `main.py`, avoiding any dependency on `/app` being present in Python's module search path.
- The `/config/mikasa_persona.py` override remains optional; a missing or invalid override falls back to the bundled file.
- Docker image construction checks that `/app/mikasa_persona.py` exists and compiles before the image can be produced.
- No database, command, scheduler, or UI behavior changes.


## 0.4.23 — standalone Mikasa persona pack

- **Persona code:** `rootfs/mikasa_persona.py` contains Mikasa's social voice, greeting variants, morning briefing and playful nudge copy, command acknowledgements, and key proactive reminder templates.
- **Independent updates:** the app first looks for `/config/mikasa_persona.py` and safely falls back to the bundled module if the override is missing or fails to load. The override is loaded once at process startup; restart the add-on after replacing it.
- **Version visibility:** `/api/health` returns `mikasa_persona_version` and `mikasa_persona_source`.
- **Separation of responsibilities:** the persona file has no database, Telegram client or XROB import. The main app passes factual counts and item titles to it; database writes, command parsing, reminder timing, and delivery confirmation remain in `main.py`.
- **Transparency:** Mikasa is a fictional AI secretary character. Her language can be warm and flirtatious, but she does not claim to be a human or to have feelings/physical experiences.

## 0.4.22 — compact Overview and scheduled Mikasa messages

- Rebuilt the Overview into two independently sized columns (agenda + fitness on the left; suggested plate + daily progress on the right) to remove tall blank areas and make the dashboard denser and easier to scan.
- Tightened the Overview header, statistics and panel spacing without changing the agenda or meal data sources.
- Telegram display name is now **Mikasa** (no role suffix). The profile description and voice are more confident, cheeky and playfully flirtatious while remaining supportive and non-explicit.
- Added opt-in **Morning briefing** settings. Choose a daily 24-hour time; the report uses the shared agenda and the add-on timezone, and is sent at most once per local date.
- Added opt-in **Playful motivation** messages. Choose a few times per week or daily; each selected day receives at most one short, randomly timed nudge between 11:00 and 20:30 local time.
- Preferences and send-once markers are stored in the existing `telegram_state` table; no new database table, cloud service or external AI dependency is introduced. Telegram must be configured in the Home Assistant add-on options for scheduled messages to be delivered.
- On mobile, the Settings save action stays accessible above the fixed bottom navigation, including narrow 320px screens.

# How XROB Assistant thinks

**Version:** 0.4.26

XROB is a rules-based assistant. It does not invent a plan: it follows the choices, intervals, stock and availability saved in its database.

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
| Reply “NO / Not today” | Move the pending sequence forward without overwriting closed, skipped or occupied dates. |
| Reset the upcoming plan | Rebuild pending plans from tomorrow; keep today, past dates and eaten history unchanged. |

8. **Routine late reminders are same-day.** The routine reminder is sent at the configured target time; the late reminder is sent later on that same due day. If still incomplete after the late alert, the due date advances to tomorrow. If the service was offline and the date is already past, opted-in routines rebase to today before today's reminders so they do not remain stuck on yesterday.
9. **Fitness history is reversible.** Deleting a weight or the final treadmill session for a date removes its activity-generated routine check. A manually checked-off routine remains complete. Multiple treadmill sessions on the same date count as one activity check until the final session is removed.
10. **The server timezone is authoritative.** The API supplies today's date, so browser pages use the add-on timezone rather than each device's local clock. iPhone, Android, desktop and Telegram therefore agree on date boundaries.


11. **One daily agenda is the source of truth.** `build_today_agenda(day)` returns open overdue/due tasks and routines, bills from their 3-day alert window onward, documents from their configured expiry reminder window onward, today's events and workout plan, today's meal and cook alerts, and completed routine/fitness logs. It returns `overdue`, `due`, `pending`, `scheduled`, `upcoming`, and `completed` states with shared counts. The dashboard, current-day Planner, Telegram `/today`, and Home Assistant sensors all consume this result; clients do not independently decide what belongs in today's list. Invalid legacy date values are skipped per record, so one malformed row cannot break the full agenda response.
12. **Changes propagate between clients.** A successful web-app write broadcasts a change to other open tabs, which refresh their shared data. The app also refreshes on tab focus/visibility and periodically, so actions made through Telegram or Home Assistant are reflected without requiring a manual reload. The server timezone remains authoritative for the date.
13. **Telegram is concise and action-first.** Reminders use one brief personal-secretary voice and include the exact reply command when an action is available. The Today command uses short human-readable lines grouped into Your to-do, On the radar, and Done, and is capped below Telegram's message-length limit. A logged gym session satisfies the matching planned workout so it is not shown twice.

## Telegram reliability

Telegram's main menu contains **Today**, **Meals**, **I ate it**, **Not today**, **Add weight**, **Add treadmill** and **Fitness week**. Today opens the agenda; Meals shows today's plate or opens the selector when no plate is assigned. The two check-in actions call the same confirmation and interval-safe rollover logic as the website. Fitness week reports weight, treadmill and gym activity only; it does not combine bills, routines or meals. The bot accepts commands only from the configured chat ID. Replies are queued before sending, and an update is marked delivered only after Telegram confirms it. Failed connections retry with a capped delay instead of polling rapidly.

## Where to check

- `rootfs/main.py` — scheduling rules, validation, stock and Telegram actions.
- `rootfs/www/app.js` — displays the data and sends user actions; it does not decide the schedule.
- `/config/xrob_assistant.db` — saved meals, daily plates, skips, task completion dates and Telegram delivery state.
- `tests/test_meal_scheduling.py` — meal repeat, stock, weekday, manual-choice and Telegram regression tests.
- `tests/test_routine_rollover_and_fitness.py` — same-day late reminders, daily rollover, activity-linked completion cleanup and shared server date.
- `tests/test_today_agenda.py` — shared daily agenda aggregation, due-soon reminders, Telegram copy, and dashboard/Planner/Home Assistant consistency.

If no meal is eligible, a day can remain empty. XROB does not invent meals, exceed Home stock, overwrite an eaten plate or place two meals on one date.


## Telegram buttons

The persistent menu keeps the quick actions people use every day: **Today**, **Meals**, **I ate it**, **Not today**, **Add weight**, **Add treadmill**, and **Fitness week**. Meal actions call the same scheduler and confirmation functions as the website. “Not today” moves the pending plate and later pending sequence forward without breaking repeat intervals or closed-day rules.

## Bills and payment reminders

Bills have a stable series identity that groups recurring occurrences into one logical bill. The Bills page shows the next unpaid due date and the last payment on a single card; past payments are listed under an expandable **Payment history** section. The latest payment can be corrected directly, while any older payment can be edited from the history. Editing a due date or amount changes that occurrence, not previously paid periods. Marking a recurring occurrence paid records its payment and creates the next occurrence in the same series. The Overview, Planner, Telegram lists and reminder scheduler all select the same earliest unpaid occurrence per series. Legacy 0.4.15 records are linked only when adjacent recurring occurrences match unambiguously; one-off and ambiguous records remain separate.

Telegram sends one first-stage reminder when three days remain (or as a catch-up if the service was offline during that window). Starting one day before the due date, it sends another reminder at least every four hours until the bill is marked paid. The four-hour interval continues while overdue. Editing a due date resets that bill's reminder schedule; paid bills are excluded.



## Mikasa — Telegram secretary persona (1.0.0)

Mikasa is a fictional AI personal secretary character: polished, feminine, confident, affectionate, witty, boldly flirtatious and playfully mischievous. Her voice is deliberately varied and conversational: short teasing greetings, warmer support when the user is struggling, approving praise for real progress, and concise practical acknowledgements. She can be suggestive without becoming explicit, avoids repeating pet names in every line, and does not bury useful information under flattery. She is transparent that she is an AI, not a real person. The implementation lives in `rootfs/mikasa_persona.py`; XROB can load an optional `/config/mikasa_persona.py` override so the voice pack can be updated independently after the 0.4.23 integration is installed.

The Telegram command engine remains rules-based. A voice wrapper adds brief, context-aware phrasing and social replies for greetings, emotional support, stress, loneliness, motivation, setbacks, small wins, affection and unclear messages. It does not decide actions, modify database data, invent task details or alter confirmation rules. The bot's profile explicitly identifies it as an AI assistant rather than a real person. This keeps the experience local and predictable, but it is not free-form generative conversation; that would require connecting a language model separately.

### Telegram capability boundaries

- **Planning:** show the shared Today agenda, tasks, bills, routines and the next seven days; create tasks and events; complete tasks and routines; record bills as paid (no money is transferred).
- **Meals:** show the current plate and upcoming plan; choose or replace today's plate; confirm eating or move the pending sequence; list home/outside meals; add/remove meal options; update home stock; maintain the shopping list.
- **Fitness:** log weight and treadmill sessions; show current-day and seven-day fitness summaries, including gym records created in the web app.
- **Alerts:** proactive task, routine, bill, meal and home-cooking reminders. Today also includes due-soon document alerts and workout-plan information.
- **Web-app-only actions:** adding/editing bills, routines, documents and gym sessions is done in the dashboard. Telegram does not execute payments, bank actions or gym-session entry.

The Overview's primary desktop row now places the daily agenda first and the compact suggested plate second. On narrow screens they stack in that order. The Meals page retains its full meal details, picker and management actions.
