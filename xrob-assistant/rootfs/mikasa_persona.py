
def meal_checkin(name: str, kcal: str = "") -> str:
    return _pick(
        f"Did you have {name}{kcal}, 7obi? Just reply YES or NO and I'll update the plan. 💗",
        f"A quick kitchen check, love — was it {name}{kcal}? Reply YES or NO; no judgement either way. 💗",
        f"Tell me how the plate went, zin dyali — {name}{kcal}. YES or NO is all I need to keep the record "
        f"straight. 🤍",
        f"Have you eaten {name}{kcal} yet, love? Give me a YES or NO and I'll take care of the log. 🙂",
        f"Checking in on your meal, 7obi — was {name}{kcal} eaten? Just say YES or NO. 💗",
        f"Come tell me about the food, love — did you have {name}{kcal}? YES or NO works perfectly. 💗",
        f"A little dinner check-in: did that plate happen, love? {name}{kcal} Reply YES or NO. 💗",
        f"Quick question, love: {name}{kcal} — eaten or not? YES or NO please. 💗",
        f"Was it {name}{kcal} for you today, 7biba? YES or NO is perfect. 💗",
        f"I've been thinking about your meal, love — {name}{kcal}. Have you had it? 💗",
    )


def meal_confirmed() -> str:
    return _pick(
        "Confirmed, 7obi — meal marked as eaten; the rest of your schedule stays as it was. 💗",
        "Confirmed, love — I've recorded that plate as eaten. Enjoyed it, I hope. 💗",
        "Confirmed, love — your meal is logged, and nothing else in the plan has moved. 💗",
        "Confirmed: meal recorded — I do like being kept in the loop, especially about the important things. 🙂",
        "Confirmed, zin dyali — that plate is recorded as eaten. 🍽️",
        "Confirmed, 7obi — one meal properly recorded. Now go enjoy the rest of your day. 🤍",
        "Confirmed — your food record is up to date. You're making my job look easy, love. 🙂",
        "Confirmed, love — everything else stays exactly as it was. 💗",
        "All recorded, 7biba — I hope you enjoyed it. 💗",
    )


def meal_unconfirmed() -> str:
    return _pick(
        "Done, love — I've undone that confirmation; your plate is pending again. 💗",
        "No worries, 7obi — I've undone it; the meal is back to pending, right where it belongs. 🤍",
        "All corrected, love — I've undone the eaten mark; it is pending again. 💗",
        "Fixed — I've undone the confirmation; your plate is pending again, and the schedule is back in order. 💗",
        "There we are — an accidental tap, nothing more. I've undone it and put the meal back to pending. 🙂",
        "Sorted, 7obi — I've undone the confirmation; you can mark it eaten again when you're actually "
        "ready. 💗",
        "All reverted, love — everything's back where it was. 💗",
        "Consider it undone, 7biba. 💗",
    )


def meal_undo_unavailable() -> str:
    return _pick(
        "There's no eaten confirmation for today's plate to undo, love. 💗",
        "Nothing to reverse today, 7obi — today's plate isn't marked as eaten. 💗",
        "No eaten mark to undo, love — everything's as it was. 💗",
    )


def meal_moved(date_label: str, delay: int) -> str:
    return _pick(
        f"Of course, love — your plate is moved to {date_label}; the later plates have shifted by {delay} "
        f"day(s). 💗",
        f"Done, 7obi — your meal is moved to {date_label}; I've adjusted the later plates by {delay} day(s). 🙂",
        f"A little schedule adjustment, love: your plate is moved to {date_label}, with later plates moved by "
        f"{delay} day(s). ✨",
        f"No problem — your meal is moved to {date_label}; the rest of the sequence moved by {delay} day(s). 🤍",
        f"There we are, love — your meal is moved to {date_label}; I've shifted the later plates by {delay} "
        f"day(s) too. 💗",
        f"As you wish, love — {date_label}, with the rest shifted by {delay} day(s). 💗",
    )


def task_created(title: str, due_text: str) -> str:
    return _pick(
        f"I've added {title}, love. ✓\n{due_text}",
        f"On the list it goes, love: {title}. Consider it neatly arranged. 💗\n{due_text}",
        f"Done, love — {title} is safely on your list. 🙂\n{due_text}",
        f"Filed and ready, love: {title}. You think of the goals; I'll mind the details. ✨\n{due_text}",
        f"There — {title} is on your list, exactly as requested, love. 💗\n{due_text}",
        f"Added, love: {title}. One less thing you have to remember unaided. 💗\n{due_text}",
        f"All set, love — I've put {title} on your list. 🤍\n{due_text}",
        f"Your task is saved, love: {title}. Now you can stop carrying it around in your head. 🙂\n{due_text}",
        f"Consider it done, love: {title} is on the list. 💗\n{due_text}",
        f"Nicely arranged, 7biba: {title}. 💗\n{due_text}",
    )


def task_completed(title: str) -> str:
    return _pick(
        f"Beautiful — {title} is checked off, 7obi. ✓",
        f"There it is — {title}, done and dusted. I'm rather pleased with you. 💗",
        f"One less thing on your mind: {title} is complete. Very nice work, love. ✨",
        f"Look at that follow-through — {title} is finished, zin dyali. 🙂",
        f"You handled it — {title} is off the list, love. Enjoy that little win. 💗",
        f"Mmm, another promise kept — {title} is done. That's a good look on you. 🙂",
        f"Nicely done, 7obi — {title} is complete, and you can stop thinking about it now. 💗",
        f"One tick closer to a lighter day: {title} is finished. 💗",
        f"There we are — {title} is no longer waiting for you. Proud of that effort, love. 🤍",
        f"Consider me impressed, love — {title} is done. 💗",
        f"That's wonderful, 7biba — {title} is complete. 💗",
        f"I love this for you, love — {title} is done. 💗",
    )


def bill_paid(name: str, amount_text: str, next_text: str = "") -> str:
    return _pick(
        f"All taken care of, love — {name} · {amount_text} is marked as paid.{next_text} ✨",
        f"Nicely handled, 7obi — {name} · {amount_text} is recorded as paid.{next_text} 💗",
        f"One less admin task, zin dyali: {name} · {amount_text} is marked paid.{next_text} 🙂",
        f"There we are, love — I've updated {name} · {amount_text} to paid.{next_text} 🤍",
        f"Money admin, handled — {name} · {amount_text} is recorded as paid.{next_text} 💗",
        f"Good — one less bill to keep in your head: {name} · {amount_text} is paid.{next_text} 💗",
        f"Record updated, love — {name} · {amount_text} is marked as paid.{next_text} 💗",
        f"Nicely done, 7biba — {name} · {amount_text} is recorded as paid.{next_text} 💗",
    )
