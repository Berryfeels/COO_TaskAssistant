"""Notification builder: events -> user-facing message text (English only)."""
from __future__ import annotations

from ..schema import Task

ACTION_VERB = {
    "create": "Create task", "update": "Update task",
    "complete": "Mark as done", "move_date": "Move task",
    "archive": "Archive task", "query": "Show tasks",
}


def _due(task: Task) -> str:
    return f", due {task.due}" if task.due else ""


def render_confirmation(pending_id: str, task: Task,
                        similar: list[str] | None = None) -> str:
    """Confirmation message always carries the pending reference (spec 5.3).

    `similar` carries near-duplicate hits (pending or existing tasks) found
    by the pipeline guard: appended as a heads-up, never blocking.
    """
    if task.action.value == "create":
        who = f" for {', '.join(task.assignees)}" if task.assignees else ""
        msg = (f"{ACTION_VERB[task.action.value]} '{task.title}'{who}"
               f"{_due(task)}. Reply \"OK {pending_id}\" to confirm. "
               f"[{pending_id}]")
    else:
        msg = (f"{ACTION_VERB[task.action.value]} "
               f"'{task.title or task.target_card_id}'{_due(task)}. "
               f"Reply \"OK {pending_id}\" to confirm. [{pending_id}]")
    if similar:
        msg += ("\nHeads-up: similar task(s) found: "
                + "; ".join(similar)
                + ". Confirm only if this is a different task.")
    return msg


def render_result(task: Task, card: dict) -> str:
    name = card.get("name", task.title)
    return f"Done: '{name}' updated in Trello."


def render_write_error(task: Task, error: Exception) -> str:
    return (f"Sorry, I could not write "
            f"'{task.title or task.action.value}' to Trello ({error}). "
            f"It was NOT created - please send the task again.")


def render_clarification(question: str) -> str:
    return question


def render_reminder(pending_id: str, task: Task, hours_left: int) -> str:
    return (f"Reminder [{pending_id}]: '{task.title or task.target_card_id}' "
            f"is still awaiting your confirmation. It will be cancelled in "
            f"about {hours_left} h. Reply \"OK {pending_id}\" to confirm.")


def render_cancelled(pending_id: str, task: Task) -> str:
    return (f"Your pending task [{pending_id}] "
            f"('{task.title or task.target_card_id}') was not confirmed "
            f"within 24 hours and has been cancelled.")


def render_ambiguous(active: list) -> str:
    lines = [f"{p.pending_id}: {p.task.action.value} "
             f"'{p.task.title or p.task.target_card_id}'"
             for p in active]
    return ("You have several pending actions. Which one do you mean?\n"
            + "\n".join(lines) + '\nReply e.g. "OK ' + active[0].pending_id + '".')
