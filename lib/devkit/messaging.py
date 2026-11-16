# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Message values and bounded display. Adapters own identity and transactions.

Accepted bodies are immutable UTF-8. A notification is never a receipt. All
display representations redact the entire body before selecting a page.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import stat
import select
import sys
import time
import unicodedata
import uuid
from pathlib import Path

BODY_MAX = 65536
STORE_MAX = 16 * 1024 * 1024
OUTPUT_MAX = 6144
PREVIEW_MAX = 180  # characters of a crew message's displayed body that its delivered preview shows


class MessageError(ValueError):
    """A safe, machine-readable refusal; detail never contains the input body."""

    def __init__(self, code, detail):
        self.code = code
        super().__init__("%s: %s" % (code, detail))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# A value after a secret's key name (`password: required`, `id-token: write`) that is not a secret: a word that
# describes a setting (never any word: `password: letmein` is a password), a mask (*** as CI prints it, xxx, …, the
# redactor's own mark) or a reference to where the secret lives ($NAME, ${NAME}, ${{ … }}, {{ … }}, %NAME%,
# <placeholder>).
_ORDINARY_VALUE = re.compile(
    r"(?i:write|read|none|required|optional|true|false|yes|no|on|off|enabled|disabled|set|unset|default|auto|env"
    r"|null|empty|missing|masked|hidden|stdin|file|prompt|inherit)[.,;:!?)\]]*"
    r"|\*+|[xX]{3,}|…|\.{3}|…?\[redacted\]|<[^<>\s]{1,64}>"
    r"|\$\{\{.*|\{\{.*|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?[.,;)]*|%[A-Za-z_][A-Za-z0-9_]*%")


_COUNT_KEY = re.compile(r"(?i)(?:count|length|len|limit|max|min|size|ttl|age|days|hours|minutes|seconds)$")
_SMALL_NUMBER = re.compile(r"\d{1,6}[.,;)]*")


def secret_shaped(text) -> bool:
    """Does text an agent typed hold a secret: a token, a key, a credential in a URL, or a key name with a value of a
    secret's shape? A key name followed by an ordinary word or a mask is not one. Display still redacts both."""
    from . import audit
    for rx, _ in audit._COMPILED:
        keyed = "k" in rx.groupindex and "s" in rx.groupindex
        for m in rx.finditer(text):
            value = text[m.end("s"):m.end()] if keyed else ""
            # a short number is a setting only after a key named for a count (token_count: 5, password_min_length: 12)
            counted = keyed and _COUNT_KEY.search(m.group("k")) and _SMALL_NUMBER.fullmatch(value)
            if not (keyed and (_ORDINARY_VALUE.fullmatch(value) or counted)):
                return True
    return any(rx.search(text) for rx in audit._extra_rules(audit.settings()["redact"]["extra"]))


def validate_body(text, *, redact=False):
    """A body to store. Text an agent typed is refused when it holds a secret; devkit's own notices and CI results
    (`redact`) quote what they saw, so they are stored with every secret redacted instead, never dropped."""
    from . import audit
    if not isinstance(text, str) or not text.strip():
        raise MessageError("invalid_text", "provide a nonempty UTF-8 body")
    if redact:
        text = audit.redact(text)
    try:
        raw = text.encode("utf-8", errors="strict")
    except UnicodeError:
        raise MessageError("invalid_text", "body is not valid UTF-8") from None
    if len(raw) > BODY_MAX:
        raise MessageError("too_large", "body exceeds 65536 UTF-8 bytes")
    if not redact and secret_shaped(text):
        raise MessageError("secret", "body appears to contain a secret")
    return {"text": text, "body_bytes": len(raw), "body_sha256": hashlib.sha256(raw).hexdigest()}


def check_capacity(record):
    if len(encoded(record)) > STORE_MAX:
        raise MessageError("capacity", "message store exceeds 16 MiB; existing content was retained")


def stored_body(item, field="text"):
    """Validate persisted content without applying new-send secret admission to history."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        raise MessageError("corrupt_state", "invalid message envelope")
    text = item.get(field, "")
    try:
        if not isinstance(text, str):
            raise ValueError()
        raw = text.encode("utf-8")
    except (ValueError, UnicodeError):
        raise MessageError("corrupt_state", "stored body is not UTF-8 text") from None
    digest = hashlib.sha256(raw).hexdigest()
    if item.get("schema") not in (None, 2):
        raise MessageError("corrupt_state", "unsupported message envelope schema")
    if item.get("schema") == 2 and (item.get("body_bytes") != len(raw) or item.get("body_sha256") != digest):
        raise MessageError("corrupt_state", "stored body does not match its digest; retained unchanged")
    return {"schema": 2, "body_bytes": len(raw), "body_sha256": digest}


def delivered_before_upgrade(item, flag="delivered") -> bool:
    """A message an earlier devkit delivered (`delivered`; a peer message: `told`) before messages had receipts:
    delivered once for good, so it is never delivered again as unread. A message of this schema has a `schema`."""
    return isinstance(item, dict) and bool(item.get("migrated_delivered") or
                                           (item.get("schema") is None and item.get(flag)))


def whole_in_preview(text, limit=PREVIEW_MAX) -> bool:
    """Does the delivered preview show the whole body (as display shows it, at most `limit` characters)?"""
    return len(display(str(text or ""))[0]) <= limit


def delivery_receipt(item) -> dict:
    """The receipt of a message read whole on delivery, for its exact body and generation."""
    digest = item.get("body_sha256") or hashlib.sha256(str(item.get("text") or "").encode("utf-8")).hexdigest()
    return {"digest": digest, "generation": item.get("generation", 1), "on": "delivery"}


def migrate_peer(record):
    doc = copy.deepcopy(record)
    if doc.get("messaging_schema", 2) != 2:
        raise MessageError("corrupt_state", "unsupported messaging schema; upgrade and restart")
    for group in ("inbox", "sent"):
        items = doc.get(group, [])
        if not isinstance(items, list):
            raise MessageError("corrupt_state", "invalid mailbox collection")
        for seq, item in enumerate(items, 1):
            if "text" not in item:  # legacy sent stubs never contained the full body
                item["needs_reconciliation"] = True
                continue
            if group == "inbox" and delivered_before_upgrade(item, "told"):
                item["migrated_delivered"] = True
            item.update(stored_body(item))
            item.setdefault("seq", seq)
            item.setdefault("created_at", item.get("ts"))
            item.setdefault("generation", 1)
            item.setdefault("receipts", {})
            if item.get("ref") and item["ref"] != item["id"]:
                item["related_ref"] = item["ref"]
            item["ref"] = item["id"]
    for intent in (doc.get("outbox") or {}).values():
        intent["message"].update(stored_body(intent["message"]))
    doc["messaging_schema"] = 2
    check_capacity(doc)
    return doc


def run_id(run):
    value = run.get("run_id")
    if value:
        try:
            if str(uuid.UUID(value)) != value:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise MessageError("corrupt_state", "invalid run UUID") from None
        return value
    # Deterministic migration survives an interrupted projection write and v18
    # writers that know nothing about the new field.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, encoded([
        "devkit-run", run.get("agent"), run.get("session"), run.get("started")]).decode()))


def migrate(run):
    """Copy legacy envelopes without inventing lost content or receipts."""
    doc = copy.deepcopy(run)
    version = doc.get("messaging_schema", 2)
    if version != 2:
        raise MessageError("corrupt_state", "unsupported messaging schema; upgrade and restart")
    doc["run_id"], doc["messaging_schema"] = run_id(doc), 2
    for group in ("messages", "decisions"):
        items = doc.setdefault(group, [])
        if not isinstance(items, list):
            raise MessageError("corrupt_state", "invalid message collection")
        for seq, item in enumerate(items, 1):
            if group == "messages" and delivered_before_upgrade(item):
                item["migrated_delivered"] = True
            item.update(stored_body(item, "text" if group == "messages" else "question"))
            item.setdefault("seq", seq)
            item.setdefault("run_id", doc["run_id"])
            item.setdefault("ref", "%s@r-%s" % (item["id"], doc["run_id"]))
            item.setdefault("generation", 1)
            item.setdefault("receipts", {})
            if group == "messages" and item.get("answered") and not item.get("answered_by"):
                linked = next((a for a in items if a.get("reply_to") in (item["id"], item["ref"])), None)
                if linked:
                    item["answered_by"] = "%s@r-%s" % (linked["id"], doc["run_id"])
                else:
                    item["needs_reconciliation"] = True
    check_capacity(doc)
    return doc


def append_message(record, envelope, *, request_id="", redact=False):
    """Validate quota and idempotency before changing the caller's record. `redact`: validate_body's, for devkit's
    own notices and CI results."""
    body = validate_body(envelope.get("text"), redact=redact)
    candidate = dict(envelope, **body)
    if request_id:
        for old in record.get("messages", []):
            if old.get("request_id") == request_id and old.get("from") == candidate.get("from"):
                fields = ("body_sha256", "to", "kind", "reply_to")
                if any(old.get(k) != candidate.get(k) for k in fields):
                    raise MessageError("idempotency_conflict", "request key already has different content or target")
                return old
        candidate["request_id"] = request_id
    proposed = dict(record, messages=list(record.get("messages", [])) + [candidate])
    check_capacity(proposed)
    record["messages"] = proposed["messages"]
    return candidate


def display(text):
    from . import audit
    redacted = audit.redact(text)
    output = []
    for char in redacted:
        code = ord(char)
        unsafe = ((code < 32 and char not in "\n\r\t") or 127 <= code <= 159
                  or unicodedata.category(char) == "Cf" or 0xE0000 <= code <= 0xE007F)
        output.append("\\u%04x" % code if unsafe else char)
    rendered = "".join(output)
    return rendered, rendered != text


def read_page(envelope, *, cursor="", output_budget=OUTPUT_MAX):
    original = envelope.get("text", envelope.get("question", ""))
    text, redacted = display(original)
    raw = text.encode("utf-8")
    ref = envelope.get("ref", envelope["id"])
    digest = envelope.get("body_sha256", hashlib.sha256(original.encode("utf-8")).hexdigest())
    generation = envelope.get("generation", 1)
    offset = 0
    if cursor:
        try:
            if len(cursor) > 1024:
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
            if value[:3] != [ref, digest, generation] or len(value) != 4:
                raise ValueError()
            offset = value[3]
            if type(offset) is not int or not 0 <= offset <= len(raw):
                raise ValueError()
            raw[:offset].decode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            raise MessageError("invalid_cursor", "cursor does not match this body and generation") from None

    def page(end):
        chunk = raw[offset:end].decode("utf-8", errors="ignore")
        end = offset + len(chunk.encode("utf-8"))
        complete = end == len(raw)
        token = "" if complete else base64.urlsafe_b64encode(encoded([ref, digest, generation, end])).decode("ascii")
        result = {"ref": ref, "digest": digest, "generation": generation,
                "total_bytes": len(raw), "offset": offset, "next_cursor": token,
                "complete": complete, "redacted": redacted, "text": chunk}
        if envelope.get("answer_ref"):
            result["answer_ref"] = envelope["answer_ref"]
        return result

    low, high = offset, min(len(raw), offset + output_budget)
    best = page(low)
    while low <= high:
        mid = (low + high) // 2
        candidate = page(mid)
        if len(encoded(candidate)) <= output_budget:
            best, low = candidate, mid + 1
        else:
            high = mid - 1
    if not best["complete"] and not best["text"]:
        raise MessageError("capacity", "output budget cannot fit a complete page frame")
    return best


def acknowledge(record, ref, *, recipient, digest, generation):
    msg = next((m for m in record.get("messages", []) if m.get("ref", m.get("id")) == ref), None)
    if not msg:
        raise MessageError("not_found", "message is unavailable")
    if msg.get("body_sha256") != digest or msg.get("generation", 1) != generation:
        raise MessageError("invalid_cursor", "digest or generation changed")
    who = recipient if isinstance(recipient, str) else recipient.get("lane", "main")
    if msg.get("to") != who:
        raise MessageError("not_recipient", "only the exact recipient may acknowledge")
    receipt = {"digest": digest, "generation": generation}
    candidate = copy.deepcopy(record)
    target = next(m for m in candidate["messages"] if m.get("ref", m.get("id")) == ref)
    target.setdefault("receipts", {})[who] = receipt
    check_capacity(candidate)
    msg.setdefault("receipts", {})[who] = receipt
    return receipt


def notification(envelope, *, remaining_bytes, read=False):
    """The frame that delivers a message. `read`: its delivery counts as reading it (a devkit notice, or a body the
    preview shows whole), so it asks for no acknowledgement."""
    ref = envelope.get("ref", envelope["id"])
    command = "dev session read" if str(envelope["id"]).startswith("S-") else "dev crew read"
    body, _ = display(envelope.get("text", ""))
    reply = " · answer to " + str(envelope["reply_to"]) if envelope.get("reply_to") else ""
    if read and len(body) <= PREVIEW_MAX:
        frame = "devkit message %s (complete; no acknowledgement needed)\n%s" % (ref + reply, body)
    else:
        frame = "devkit message %s (preview; %s)\n%s\nRead: %s %s" % (
            ref + reply, "no acknowledgement needed" if read else "not acknowledged", body[:PREVIEW_MAX], command, ref)
    return frame if len(frame.encode("utf-8")) <= remaining_bytes else ""


def input_options(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--text-file", help="read a regular UTF-8 file, at most 65536 bytes")
    group.add_argument("--stdin", action="store_true", help="read bounded UTF-8 stdin (10 second deadline)")
    parser.add_argument("--request-id", default="", help="stable retry key for this exact body and recipient")


def _open_regular(filename):
    """Pin parent descriptors, refusing symlinks before opening the authorized file."""
    path = Path(os.path.abspath(filename))
    parts = path.parts[1:]
    if parts and parts[0] in ("var", "tmp", "etc"):
        alias = Path("/") / parts[0]
        if alias.is_symlink() and alias.lstat().st_uid == 0:
            path = alias.resolve().joinpath(*parts[1:])
            parts = path.parts[1:]
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        if not parts:
            raise MessageError("invalid_text", "text-file must be a regular file")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        before = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise MessageError("invalid_text", "text-file must be a regular file, not a link or device")
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            os.close(fd)
            raise MessageError("invalid_text", "text-file changed while opening")
        return fd
    except OSError as error:
        raise MessageError("invalid_text", "text-file cannot be opened safely") from error
    finally:
        os.close(directory)


def input_text(args, authorize_file=None):
    """Read once before reserving an ID, rate slot, lock, or question state."""
    positional = " ".join(getattr(args, "text", None) or [])
    filename = getattr(args, "text_file", None)
    stdin = getattr(args, "stdin", False)
    if sum(bool(x) for x in (positional, filename, stdin)) != 1:
        raise MessageError("invalid_text", "choose exactly one of positional text, --text-file, or --stdin")
    if positional:
        return validate_body(positional)["text"]
    if filename:
        if authorize_file is None:
            raise MessageError("invalid_text", "authenticated identity is required for text-file input")
        authorize_file(filename)
        fd = _open_regular(filename)
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(BODY_MAX + 1)
    else:
        fd = sys.stdin.fileno()
        deadline, chunks, total = time.monotonic() + 10, [], 0
        while total <= BODY_MAX:
            timeout = max(0, deadline - time.monotonic())
            if not timeout or not select.select([fd], [], [], timeout)[0]:
                raise MessageError("busy", "stdin deadline expired without a complete body")
            chunk = os.read(fd, min(8192, BODY_MAX + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
    if len(raw) > BODY_MAX:
        raise MessageError("too_large", "body exceeds 65536 UTF-8 bytes")
    try:
        body = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        raise MessageError("invalid_text", "body is not valid UTF-8") from None
    return validate_body(body)["text"]


def index_page(items, cursor=""):
    """A bounded index; body reads and receipts are separate operations."""
    rows = [{"id": m["id"], "ref": m.get("ref") or m["id"], "kind": m.get("kind", "question"),
             "from_name": m.get("from_name", ""), "preview": display(m.get("text", ""))[0][:180],
             "digest": m.get("body_sha256", ""), "generation": m.get("generation", 1),
             "acknowledged": bool(m.get("receipts")), "answered": bool(m.get("answered"))} for m in items]
    stamp = hashlib.sha256(encoded(rows)).hexdigest()
    offset = 0
    if cursor:
        try:
            saved, offset = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if saved != stamp or type(offset) is not int or not 0 <= offset <= len(rows):
                raise ValueError()
        except (ValueError, UnicodeError, TypeError):
            raise MessageError("invalid_cursor", "inbox changed; read its first page again") from None
    page = {"items": [], "complete": False, "next_cursor": "", "total": len(rows)}
    while offset < len(rows):
        candidate = dict(page, items=page["items"] + [rows[offset]])
        if len(encoded(candidate)) > OUTPUT_MAX - 200:
            break
        page = candidate
        offset += 1
    page["complete"] = offset == len(rows)
    if not page["complete"]:
        page["next_cursor"] = base64.urlsafe_b64encode(encoded([stamp, offset])).decode()
    return page
