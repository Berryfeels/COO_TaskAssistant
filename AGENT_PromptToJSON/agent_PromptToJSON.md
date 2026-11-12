{
  "name": "create_card",
  "description": "Create a new task. Use when the user asks to add, create, note down, or remind about something to do.",
  "input_schema": {
    "properties": {
      "title": {"type": "string"},
      "list": {"type": "string", "description": "List name as the user said it. Omit if not stated."},
      "members": {"type": "array", "items": {"type": "string"}, "description": "People assigned, as named by the user."},
      "due": {"type": "string", "description": "ISO 8601 datetime, resolved from the current date given in the prompt."},
      "labels": {"type": "array", "items": {"type": "string"}},
      "description": {"type": "string"}
    },
    "required": ["title"]
  }
}


rules for your conventions and edge cases:

Defaults: no list mentioned → “Inbox”; no board mentioned → the default board.
Ambiguity policy: when a card reference matches several cards, or a name matches no member, the agent asks a clarifying question instead of guessing.
Destructive actions: archive/delete require explicit confirmation.
Vocabulary: “done” means move to the “Done” list and set dueComplete; “blocked” means add the “Blocked” label. Every team has its own meanings, and these are the rules that earn their tokens.
Multiple actions in one message: allow several tool calls per message (“create X and assign it to Marco, due Friday” may be one create call or create plus assign).