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


def render_confirmation(pending_id: str, task: Task) -> str:
    """Confirmation message always carries the pending reference (spec 5.3)."""
    if task.action.value == "create":
        who = f" for {', '.join(task.assignees)}" if task.assignees else ""
        return (f"{ACTION_VERB[task.action.value]} '{task.title}'{who}"
                f"{_due(task)}. Reply \"OK {pending_id}\" to confirm. [{pending_id}]")
    return (f"{ACTION_VERB[task.action.value]} "
            f"'{task.title or task.target_card_id}'{_due(task)}. "
            f"Reply \"OK {pending_id}\" to confirm. [{pending_id}]")


def render_result(task: Task, card: dict) -> str:
    name = card.get("name", task.title)
    return f"Done: '{name}' updated in Trello."


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
