# DevKit macOS notification helper

DevKit builds this Objective-C source into `~/Applications/DevKit.app` during the `cli` step of `dev install`.
It uses Apple's installed Command Line Tools and frameworks; it adds no Python packages. The bundle has the
`DevKit` display name, identifier `com.fireflysoftwaresolutions.devkit.notifications`, and an ICNS icon generated
from `plugin/assets/icon.png`. Installation registers the signed app with Launch Services without launching it.

## Delivery contract

Call `devkit.notifications.notify_event(event)` after committing the canonical event and releasing its locks.
Supply nonempty `event_id` and `recipient` strings. Supported categories are `crew`, `peer`, `decision`, `security`,
and `progress`. The helper displays fixed safe text for the category. Supplied titles, summaries, bodies, and
references never enter the alert. Clicking an alert has no command handler and cannot acknowledge or answer a
message, approve a decision, or start work.

The Boolean result means accepted by Notification Center, including an already accepted event, or retryable
failure. It does not prove that the alert appeared or was read. macOS settings and Focus can suppress its display.
Dedupe uses the event and recipient together. Attempt records live separately in
`~/.local/state/devkit/notification-attempts/`; they contain only hashed identifiers, lease tokens, and attempt
status. A 15-second lease recovers an interrupted attempt; each native process has a six-second timeout.
The helper also checks pending and delivered native identifiers before submitting again. A crash after native
acceptance followed by dismissal before the retry can still produce a repeated alert; message delivery and
acknowledgement are independent and remain recoverable.

The compatibility `notify(title, text, subtitle="")` function sends fixed generic progress text without retaining
an event ledger. The caller applies progress notification preferences. Message events should use the stable-event
API instead.

## Permission and recovery

The first alert requests macOS notification permission. If permission is denied, enable **DevKit** in
**System Settings > Notifications**. The helper exits after four seconds, including time waiting for a permission
response; the Python adapter enforces a six-second process timeout. A late response cannot resume the exited
helper or submit its original alert. The adapter returns failure and leaves the event retryable; the caller must
make a new attempt with the same event and recipient after permission is available. The adapter does not schedule
that retry itself. Whether macOS keeps the permission prompt visible and saves a response after process exit
requires live host validation. If permission is not saved, use Notifications settings before retrying.
Missing bundles, missing icons, denied permission, and native errors return failure without changing canonical
messages or their acknowledgement. Read pending messages with `dev crew inbox` or `dev session inbox` even when
no desktop alert appears. Failed delivery does not fall back to a generic Python or AppleScript identity.

If building fails, check the Xcode Command Line Tools installation and rerun `dev install --only cli` through the
supported installation workflow. An unsuccessful staged build preserves the previous app. An unchanged signed
bundle is reused. Signing is ad hoc: permission persistence across upgrades and actual notification display need
live acceptance on each supported macOS host; a bundle check alone does not establish them.

Live acceptance must include waiting more than four seconds before responding to the first permission prompt,
then retrying the same event and confirming the branded alert appears without changing message acknowledgement.
Also check denial followed by enabling permission in settings, and permission retention after a signed upgrade.

## Validation without notifications

Run `dev test run tests/test_notifications.py tests/test_setup_flow.py`. On macOS the tests compile, create the
icon, sign, and validate a temporary bundle. They invoke its `--check` mode, which reports bundle identity and
icon availability without initializing Notification Center. All delivery tests replace the external native
process boundary and use isolated temporary state. No test registers or installs an app in the developer's home.

Native exit codes are `0` for accepted or already present, `2` for denied permission, `3` for a native error or
timeout, and `64` for invalid arguments or input. `--notify` accepts at most 4,096 bytes of JSON on standard input;
the adapter sends only a hashed identifier and category. `--check` is the build validation interface.

Apple's [bundle structure reference](https://developer.apple.com/library/archive/documentation/CoreFoundation/Conceptual/CFBundles/BundleTypes/BundleTypes.html)
explains app identity. Its [notification authorization documentation](https://developer.apple.com/documentation/usernotifications/asking-permission-to-use-notifications)
describes the permission request.
