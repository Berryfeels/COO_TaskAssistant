"""Notification builder: events -> user-facing message text (English only)."""
from __future__ import annotations

from ..schema import Card

ACTION_VERB = {
    "add_card": "Add card", "update": "Update card",
    "complete": "Mark as done", "move_date": "Move card",
    "archive": "Archive card", "add_list": "Add column",
    "query": "Show cards",
}


def _due(card: Card) -> str:
    return f", due {card.due}" if card.due else ""


def render_confirmation(pending_id: str, card: Card,
                        similar: list[str] | None = None) -> str:
    """Confirmation message always carries the pending reference (spec 5.3).

    `similar` carries near-duplicate hits (pending or existing cards) found
    by the pipeline guard: appended as a heads-up, never blocking.
    """
    if card.action.value == "add_card":
        who = f" for {', '.join(card.assignees)}" if card.assignees else ""
        msg = (f"{ACTION_VERB[card.action.value]} '{card.title}'{who}"
               f"{_due(card)}. Reply \"OK {pending_id}\" to confirm. "
               f"[{pending_id}]")
    else:
        msg = (f"{ACTION_VERB[card.action.value]} "
               f"'{card.title or card.target_card_id}'{_due(card)}. "
               f"Reply \"OK {pending_id}\" to confirm. [{pending_id}]")
    if similar:
        msg += ("\nHeads-up: similar card(s) found: "
                + "; ".join(similar)
                + ". Confirm only if this is a different card.")
    return msg


def render_result(card: Card, api_card: dict,
                  similar: list[str] | None = None) -> str:
    """Post-write confirmation: the action is already on the board."""
    name = api_card.get("name", card.title)
    if card.action.value == "add_list":
        msg = f"Done: column '{name}' added to the board in Trello."
    else:
        verb = "created" if card.action.value == "add_card" else "updated"
        msg = f"Done: '{name}' {verb} in Trello."
    if similar:
        msg += ("\nHeads-up: similar card(s) found: "
                + "; ".join(similar)
                + ". Check whether this is a duplicate.")
    return msg


def render_write_error(card: Card, error: Exception) -> str:
    return (f"Sorry, I could not write "
            f"'{card.title or card.action.value}' to Trello ({error}). "
            f"It was NOT created - please send the card again.")


def render_invalid(card: Card, error: ValueError) -> str:
    return (f"Sorry, I could not build a valid {card.action.value} action "
            f"from that message ({error}). Please send it again with "
            f"the missing details.")


def render_error(error: Exception) -> str:
    return (f"Sorry, something went wrong handling that message ({error}). "
            f"Nothing was written - please try again.")


def render_chat(card: Card) -> str:
    """Free-text conversational reply chosen by the LLM."""
    return card.reply.strip() or "I'm here - what would you like me to do?"


def render_clarification(question: str) -> str:
    return question


def render_reminder(pending_id: str, card: Card, hours_left: int) -> str:
    return (f"Reminder [{pending_id}]: '{card.title or card.target_card_id}' "
            f"is still awaiting your confirmation. It will be cancelled in "
            f"about {hours_left} h. Reply \"OK {pending_id}\" to confirm.")


def render_cancelled(pending_id: str, card: Card) -> str:
    return (f"Your pending card [{pending_id}] "
            f"('{card.title or card.target_card_id}') was not confirmed "
            f"within 24 hours and has been cancelled.")


def render_ambiguous(active: list) -> str:
    lines = [f"{p.pending_id}: {p.card.action.value} "
             f"'{p.card.title or p.card.target_card_id}'"
             for p in active]
    return ("You have several pending actions. Which one do you mean?\n"
            + "\n".join(lines) + '\nReply e.g. "OK ' + active[0].pending_id + '".')
