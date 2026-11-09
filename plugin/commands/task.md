---
description: Manage the project's tasks (Now / Next / Later / Done)
---

Manage the project's work items with the `dev task` command: `dev task` lists them; `dev task add "…" --now|--later`; `dev task start|done|release <id>`; `dev task move <id> --to now|next|later --why "…"`. Name each item by its id (`T-…`, `F-…`, `B-…`, `S-…`, or a unique prefix), never by the number in front of it in the list: devkit refuses a number once the list it came from changed. Apply what the user asks: $ARGUMENTS. Then show the updated list.
