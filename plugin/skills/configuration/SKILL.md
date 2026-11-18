---
name: configuration
description: Use when changing devkit settings, adding a configuration key or writer, diagnosing invalid configuration, or repairing a failed config edit.
---

# Devkit configuration

Use the shared schema and supported writers. Preserve saved choices and failed edits.

## Inspect and change

- `dev config show` returns the fresh effective configuration and `errors`; `dev config validate` checks the same
  schema. Invalid saved JSON or an unknown key is a repair task. A saved unknown key that a writer leaves as it was
  does not block that write, the install, or an update; `dev doctor` lists it and runs its other checks, and
  `dev config edit` removes it. The retired `safeguards` key is no error: `dev config validate` and `dev doctor` say
  it is ignored, and the next configuration writer saves the file without it.
- `dev config set <key> <value>` validates a complete candidate. Dotted paths reach nested keys; leading, trailing,
  or doubled dots are invalid. JSON values retain their types. Unknown keys, duplicate JSON keys, nonfinite numbers,
  and booleans used as numbers are refused. Whole-object replacement validates every child.
- `dev config edit` edits a private candidate. Success requires a zero editor exit and valid configuration.
  Any saved-file change while editing refuses the edit and preserves the other writer's values.
  Failure leaves saved bytes unchanged and
  reports the private recovery path. Never replace saved configuration with an invalid recovery file.
- `$EDITOR` is parsed as arguments, without a shell. VS Code and Code Insiders get `--wait`; macOS `open` gets `-W`.
  A missing, nonregular, or symlinked candidate cannot overwrite the saved file.

## Mode examples

`dev config set work.mode guided` sets the saved mode; `dev config set work.mode auto` selects the default behavior
again. The values are lowercase: `Guided` is invalid, and while the configuration is invalid the mode is unknown,
so `dev crew mode` and `dev crew ask --decision` refuse until `dev config validate` passes. Only inherited runs
follow that setting at their next decision. `dev crew mode inherit` clears an explicit run override. The mode
decides who answers the crew's decisions, plus three command behaviors (`crew` skill, section 7); it holds no
command for approval, and any caller can run the setting commands. Changing the developer's settings is their
decision, so in guided mode ask first. Host sandbox, account login, trust, and sudo requirements remain independent.

## Source changes

`lib/devkit/configschema.py` owns the closed key/type contract and defaults. Add keys there, document them in
`docs/configuration.md`, and test the actual reader and writer. Custom names belong only in declared extension
maps. Use `core.config_snapshot()` for fresh effective values and errors, and the shared validated transactional
writers for updates. Validate the whole candidate under the configuration lock before saving; never write saved
JSON directly or decide from cached `CFG`. Every writer drops retired top-level keys (`configschema.RETIRED`) from
what it saves.

Keep explicit `false`, empty lists, and absent values distinct. Saved empty branch/commit lists remain literal,
while their existing readers inherit defaults. Preserve old saved guided values on upgrade; invalid mode values
must not enable auto. Verification uses isolated config/state fixtures, never the developer's live settings.
