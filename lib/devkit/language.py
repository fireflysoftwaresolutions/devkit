# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The developer's language: what the agents reply in, and how devkit tells.

The rule (the developer, 2026-11-12): reply in the developer's language, by default American English. With devkit's
`language` setting at `auto` (the default), that is the language of the developer's latest message, or of the earlier
ones when the latest is too short to tell ("ok", "continua", a command); a set locale (es-ES, en-US) fixes it whatever
the developer writes. Only what the developer reads follows it: code, comments, commits, PRs, docs, devkit's records
and the crew's briefs and reports stay in American English.

`detect` reads the prose of a text only: code blocks and spans, links, paths, commands, log lines, quoted text and the
status block's marker and labels are dropped first. It scores the function words and word endings of English,
Spanish, Portuguese, French, Italian, German, Catalan and Dutch, each word shared by several languages counting for
each a share, and answers None when it is unsure (few words, a close call), so the Stop hook's check
(canary.language_gate) never fires on a guess. Standard library only and no model: a call takes about a millisecond.
"""
from __future__ import annotations

import json
import os
import re

from . import core

AUTO = "auto"

# Names for the reply line; a locale with no name here is written as the developer set it.
NAMES = {"en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French", "it": "Italian", "de": "German",
         "ca": "Catalan", "nl": "Dutch", "gl": "Galician", "eu": "Basque", "ro": "Romanian", "pl": "Polish",
         "cs": "Czech", "sv": "Swedish", "da": "Danish", "nb": "Norwegian", "no": "Norwegian", "fi": "Finnish",
         "el": "Greek", "tr": "Turkish", "ru": "Russian", "uk": "Ukrainian", "ja": "Japanese", "zh": "Chinese",
         "ko": "Korean", "ar": "Arabic", "he": "Hebrew", "hi": "Hindi"}
LOCALE_NAMES = {"en-us": "American English", "en-gb": "British English", "pt-br": "Brazilian Portuguese",
                "pt-pt": "European Portuguese"}
STAYS = "records, commits and docs stay in American English"

# ------------------------------------------------------------------------------------------------------- the words
# Frequent function words and short everyday words per language. A word listed under several languages counts
# 1/n for each, so "de" (six languages) says little and "the", "não" or "és" say a lot.
_WORDS = {
    "en": """the and is are was were be been being this that these those with for from by of to it its it's you your
        we our they their them have has had do does did done not but or if then than so as at on in an all which what
        when where who why how will would should could can may there here now only also just after before into about
        over because more no yes please thanks i me my still each every any some both let's don't doesn't didn't
        isn't aren't wasn't can't won't that's there's here's what's i'm i've i'll you're we're they're""",
    "es": """el los las del al y es está están son para por con una uno lo se no pero como más ya esta este esto estos
        estas hay todo todos también porque cuando donde dónde sobre entre muy sin hasta desde puedes puede tengo tiene
        hacer hecho ahora así qué cómo sí he ha han fue ser estoy mi tu su sus nos le les la de en un que si o otra
        otro vez gracias vale favor dime quiero necesito deberías debería hablar sale sigue siguen hemos""",
    "pt": """não é são está os as do da dos das no na nos nas um uma para com por que se mais mas como também isso
        isto este esta ele ela eles você vocês foi ser tem têm ao aos à pelo pela já ainda quando onde muito sem
        agora então aqui seu sua meu minha fazer pode posso o e de em a gente faz favor dá depois novo""",
    "fr": """le les des du et est sont une un pour pas ne je vous nous il elle ils ce cette ces qui que dans sur avec
        au aux mais ou où plus tout tous été être avoir fait peut peux comme aussi très sans déjà maintenant voici
        de la en on si quand pourquoi encore toujours depuis merci""",
    "it": """il lo gli la le i di del della delle dei degli che è sono non per con una un uno nel nella sul sulla al
        alla alle ai ma anche come più questo questa questi quello ci si ha hanno ho essere stato fatto può già ancora
        ora adesso dove quando perché tutto tutti molto senza se o ed cosa qui puoi di nuovo dopo""",
    "de": """der die das den dem des und ist sind nicht ein eine einen einem einer mit für auf ich du sie wir es zu
        von im auch aber oder wie wenn dass noch nur schon jetzt hier kann kannst wird werden wurde hat haben bei
        nach aus so als um über alle diese dieser dieses sich mehr bitte danke ja nein habe beim seit wieder warum""",
    "ca": """el els les la del dels i és són amb per que no un una uns unes al als de a en es ho hi ha han molt també
        però perquè això aquest aquesta aquests aquestes què com quan on més tot tots fer ara ja encara sense puc pots
        pot vull cal seu seva meu meva nostre sí li aquí doncs gràcies si torna anar fins des pel pels està estan
        estic sóc som sou hem heu fet fes feu mateix altre altra res gens molts moltes totes abans després avui
        ahir demà sempre mai bé""",
    "nl": """de het een en van is zijn niet dat die dit met voor op ik je jij u we wij ze hij zij er te naar om maar
        ook nog wel al als bij aan uit door wordt worden werd heeft hebben kan kun kunt moet moeten wat hoe waar
        wanneer omdat geen meer nu hier alle deze zo dan toch even weer sinds doet staat klaar graag zou zal gewoon
        eigenlijk ben heb mijn jouw onze hun welke alleen altijd nooit misschien want dus nee bedankt alsjeblieft""",
}
# Elisions written with an apostrophe: the part before it, with the apostrophe (c'est -> "c'" and "est").
_ELISIONS = {"c'": ("fr", "it"), "j'": ("fr",), "qu'": ("fr",), "n'": ("fr", "ca"), "s'": ("fr", "it", "ca"),
             "m'": ("fr", "ca"), "t'": ("fr", "ca"), "l'": ("fr", "it", "ca"), "d'": ("fr", "it", "ca"),
             "dell'": ("it",), "all'": ("it",), "nell'": ("it",), "sull'": ("it",), "dall'": ("it",),
             "un'": ("it",), "quest'": ("it",)}
# Word endings that mark a language, with their weight (one per word): the marked ones count as a function word.
_ENDINGS = (("ciones", ("es",), 1.0), ("ción", ("es",), 1.0), ("ções", ("pt",), 1.0), ("ção", ("pt",), 1.0),
            ("zione", ("it",), 1.0), ("zioni", ("it",), 1.0), ("cions", ("ca",), 1.0), ("ció", ("ca",), 1.0),
            ("siones", ("es",), 1.0), ("sión", ("es",), 1.0), ("sions", ("ca",), 1.0), ("sió", ("ca",), 1.0),
            ("sões", ("pt",), 1.0), ("são", ("pt",), 1.0), ("sione", ("it",), 1.0), ("sioni", ("it",), 1.0),
            ("ungen", ("de",), 0.5), ("ung", ("de",), 0.5), ("keit", ("de",), 0.5), ("heit", ("de",), 0.5),
            ("heid", ("nl",), 0.5), ("lijk", ("nl",), 0.5), ("lijke", ("nl",), 0.5), ("tion", ("en", "fr"), 0.5),
            ("tions", ("en", "fr"), 0.5), ("ing", ("en",), 0.5), ("ment", ("en", "fr"), 0.5))
# Letters that mark a language (per word that holds one).
_LETTERS = (("ñ", ("es",)), ("ã", ("pt",)), ("õ", ("pt",)), ("ß", ("de",)), ("ä", ("de",)), ("ö", ("de",)),
            ("ü", ("de",)), ("ê", ("pt", "fr")), ("à", ("fr", "it", "ca", "pt")), ("è", ("fr", "it", "ca")),
            ("ò", ("it", "ca")), ("ì", ("it",)), ("ù", ("it", "fr")))


def _index() -> dict:
    owners = {}
    for lang, words in _WORDS.items():
        for w in words.split():
            owners.setdefault(w, set()).add(lang)
    return {w: tuple(sorted(langs)) for w, langs in owners.items()}


_INDEX = _index()

# --------------------------------------------------------------------------------------------------------- prose
MAX_TEXT = 64 << 10  # characters read from a message: far more than a language needs, and a bound on the time
_FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})")
_SPAN = re.compile(r"(`{1,3})[^`\n]{1,400}\1")  # bounded: a run of backticks never costs more than one pass
_TAGGED = ("system-reminder", "command-name", "command-message", "local-command-stdout", "task-notification",
           "environment_context")
_TAG = re.compile(r"</?[A-Za-z][\w:-]*(?:\s[^<>\n]*)?/?>")
_URL = re.compile(r"(?i)(?<![\w+.-])(?:[a-z][a-z0-9+.-]*://|www\.)\S+|(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]+")
_PATH = re.compile(r"(?:~|\.{1,2})?/[^\s,;:()\"']+|(?<![\w.-])[\w.-]+(?:/[\w.-]+)+/?|"
                   r"(?<![\w-])[\w-]+\.[a-z][a-z0-9]{0,5}\b")
_QUOTED = re.compile(r"\"[^\"\n]{0,400}\"|“[^”\n]{0,400}”|«[^»\n]{0,400}»|„[^“”\n]{0,400}[“”]")
_BLOCKQUOTE = re.compile(r"(?m)^[ \t]*>.*$")
_COMMAND = re.compile(r"(?m)^[ \t]*[$%][ \t].*$")
# lines of tool output: tracebacks, stack frames, test results, log records. Within one line only ([ \t], never \s),
# so a run of blank lines costs one pass
_LOGLINE = re.compile(r"(?m)^.*(?:Traceback \(most recent call last\)|^[ \t]+at [\w$.<>]+\(|"
                      r"The above exception was the direct cause of the following exception|"
                      r"During handling of the above exception, another exception occurred|"
                      r"File \"[^\"\n]+\", line \d+|^[ \t]*(?:ERROR|WARN(?:ING)?|INFO|DEBUG|FATAL|FAILED|PASSED|E  )\b|"
                      r"^[ \t]*\d{4}-\d\d-\d\d[T ]\d\d:\d\d|^[ \t]*\w+(?:\.\w+)*(?:Error|Exception|Warning):[ \t]).*$")
_MARKER = re.compile(r"(?i)\[\s*firefly\s*>\s*devkit(?:\s*·[^\]\n]*)?\]")
# one character class per gap: two runs side by side ([ \t]* \** [ \t]*) trade characters when no star is there
_LABEL = re.compile(r"(?im)^([ \t]*[-*•][ \t]+)(?:\*+[ \t]*)?(?:done|check|progress|crew|you|next)[ \t*]*:[ \t*]*|"
                    r"needs from the tech lead[ \t]*:")
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*")


def _without_fences(text: str) -> str:
    """The text without its fenced code blocks (an unclosed one runs to the end), in one pass over the lines."""
    out, fence = [], None
    for ln in text.split("\n"):
        m = _FENCE.match(ln)
        if fence is None:
            if m:
                fence = m.group(1)
            else:
                out.append(ln)
        elif m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not ln[m.end():].strip():
            fence = None
    return "\n".join(out)


def _without_tagged(text: str) -> str:
    """The text without hook and harness blocks (<system-reminder>…</system-reminder>), found with str.find so an
    unclosed one costs one pass: it and what follows are dropped."""
    for tag in _TAGGED:
        start, end, parts, at = f"<{tag}>", f"</{tag}>", [], 0
        while True:
            i = text.find(start, at)
            if i < 0:
                parts.append(text[at:])
                break
            parts.append(text[at:i])
            j = text.find(end, i)
            if j < 0:
                break
            at = j + len(end)
        text = " ".join(parts)
    return text


def prose(text: str) -> str:
    """The text without what is not the writer's own prose."""
    text = _without_tagged(_without_fences((text or "")[:MAX_TEXT].replace("’", "'")))
    for pat in (_LOGLINE, _SPAN, _URL, _PATH, _QUOTED, _BLOCKQUOTE, _COMMAND, _MARKER, _TAG):  # whole log lines first
        text = pat.sub(" ", text)
    return _LABEL.sub(lambda m: m.group(1) or " ", text)


def _scores(text: str) -> tuple:
    """(scores by language, words read) over the prose of `text`."""
    return _count(prose(text))


def _count(body: str) -> tuple:
    """(scores by language, words read) over prose."""
    scores = {}

    def add(langs, weight):
        for lang in langs:
            scores[lang] = scores.get(lang, 0.0) + weight / len(langs)

    words = _WORD.findall(body.lower())
    for w in words:
        langs = _INDEX.get(w)
        if langs:
            add(langs, 1.0)
        elif "'" in w:
            head, _, tail = w.partition("'")
            if head + "'" in _ELISIONS:
                add(_ELISIONS[head + "'"], 1.0)
            if tail in _INDEX:
                add(_INDEX[tail], 1.0)
        for end, langs_, weight in _ENDINGS:
            if len(w) > len(end) + 2 and w.endswith(end):
                add(langs_, weight)
                break
        for ch, langs_ in _LETTERS:
            if ch in w:
                add(langs_, 0.5)
                break
    if "l·l" in body.lower():
        add(("ca",), 1.0)
    return scores, len(words)


def _ranked(scores: dict) -> list:
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


MIN_WORDS, MIN_SCORE, MARGIN = 3, 2.0, 1.8
# "Please translate this email: Hola equipo, …": three or more words without a colon, then ": " (one pass per line)
_INTRO = re.compile(r"([^\s:]+(?:[ \t]+[^\s:]+){2,}):[ \t]+(\S.*)")


def _own_and_introduced(body: str) -> tuple:
    """(the developer's own words, the text they introduce with a colon): the rest of a line after "<three or more
    words>: ", and every line after a line that ends with ":". A pasted email, log or string is what the message is
    about, not what it is written in."""
    own, introduced, after = [], [], False
    for ln in body.split("\n"):
        if after:
            introduced.append(ln)
            continue
        m = _INTRO.match(ln.strip())
        if m:
            own.append(m.group(1))
            introduced.append(m.group(2))
        else:
            own.append(ln)
        after = ln.rstrip().endswith(":")
    return "\n".join(own), "\n".join(introduced)


def _decide(scores: dict, n: int):
    """The leading language when it leads clearly, else None (few words, a close call)."""
    ranked = _ranked(scores)
    if n < MIN_WORDS or not ranked:
        return None
    best, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    return best if top >= MIN_SCORE and top >= MARGIN * second else None


def detect(text: str):
    """The language a developer's message is written in ("es"), or None when unsure. When the message introduces
    text with a colon (an email to translate, strings to check), the developer's own words decide when they can on
    their own; otherwise the whole message does. The leading language is the answer or nothing: English leading
    never gives another language."""
    body = prose(text)
    own, introduced = _own_and_introduced(body)
    if introduced.strip():
        lang = _decide(*_count(own))
        if lang:
            return lang
    return _decide(*_count(body))


SURE_WORDS, SURE_SCORE, SURE_SHARE = 12, 4.0, 0.2


def other_language(text: str, want: str):
    """The language a reply's prose is surely written in when it is not `want`, else None: never on a short reply,
    or when `want` is clearly present too (a Spanish reply that quotes English output, Catalan read close to
    Spanish). The name is the leading language; what is sure is that the reply is not in `want`."""
    scores, n = _scores(text)
    ranked = _ranked(scores)
    if n < SURE_WORDS or not ranked:
        return None
    best, top = ranked[0]
    if best == want or top < SURE_SCORE or scores.get(want, 0.0) >= max(1.5, SURE_SHARE * top):
        return None
    return best


# ---------------------------------------------------------------------------------- the developer's messages
_NOT_HUMAN = ("<task-notification", "[SYSTEM NOTIFICATION", "<system-reminder", "<agent-message", "Caveat:",
              "<local-command-stdout", "[Request interrupted")
CHUNK = 256 << 10
MAX_BYTES = 2 << 20  # how far back the developer's earlier messages are read


def _human_text(e: dict):
    """The developer's own words in one transcript line, or None (tool results, hook context, notifications)."""
    if e.get("type") == "user" and not (e.get("isMeta") or e.get("isCompactSummary") or e.get("isSidechain")):
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list):
            content = "\n".join(str(c.get("text") or "") for c in content
                                if isinstance(c, dict) and c.get("type") == "text")
        text = content if isinstance(content, str) else ""
    elif e.get("type") == "event_msg" and isinstance(e.get("payload"), dict) and \
            e["payload"].get("type") == "user_message":
        text = str(e["payload"].get("message") or "")
    else:
        return None
    text = _without_tagged(text[:MAX_TEXT]).strip()
    if not text or text.startswith(_NOT_HUMAN):
        return None
    return text


def _lines_backward(path, max_bytes: int = MAX_BYTES):
    """The lines of a file from the last one up, reading at most `max_bytes` from its end."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        floor = max(0, pos - max_bytes)
        rest = b""  # the start of the line the later chunk began inside
        while pos > floor:
            step = min(CHUNK, pos - floor)
            pos -= step
            fh.seek(pos)
            parts = (fh.read(step) + rest).split(b"\n")
            rest = parts[0]
            yield from reversed(parts[1:])
        if floor == 0 and rest:  # the file's first line; past the limit it is a cut one, never read
            yield rest


def developer_messages(transcript, max_bytes: int = MAX_BYTES):
    """The developer's messages in a Claude Code transcript or a Codex rollout, newest first. Nothing on any error."""
    if not transcript:
        return
    try:
        for raw in _lines_backward(transcript, max_bytes):
            if b'"user' not in raw:
                continue
            try:
                e = json.loads(raw.decode("utf-8", "ignore"))
            except ValueError:
                continue
            if isinstance(e, dict):
                text = _human_text(e)
                if text:
                    yield text
    except (OSError, TypeError, ValueError):
        return


def member_rollout(transcript) -> bool:
    """A Codex member's rollout (its session_meta names a parent thread): it copies the parent's history, so the
    developer's messages in it are not addressed to this agent."""
    try:
        with open(transcript, "rb") as fh:
            first = json.loads(fh.readline(8 << 20) or b"null")
    except (OSError, TypeError, ValueError, RecursionError):
        return False
    p = first.get("payload") if isinstance(first, dict) and first.get("type") == "session_meta" else None
    if not isinstance(p, dict):
        return False
    src = p.get("source")
    return bool((isinstance(src, dict) and "subagent" in src) or p.get("parent_thread_id")
                or p.get("subagent_history_start_ordinal") is not None)


# ------------------------------------------------------------------------------------------ what to reply in
def setting(cfg=None) -> str:
    """devkit's `language` setting: "auto" or the locale the developer set ("es-ES")."""
    cfg = core.CFG if cfg is None else cfg
    value = str(cfg.get("language") or "").strip()
    # `dev config set` checks the value; a hand-edited one that is no locale is never echoed into the context
    return AUTO if value.lower() == AUTO or not core.LOCALE_RE.fullmatch(value) else value


def _base(locale: str) -> str:
    return re.split(r"[-_]", locale.lower())[0]


def name(lang: str) -> str:
    """"es" -> Spanish, "en-US" -> American English; an unknown locale as written."""
    key = lang.lower().replace("_", "-")
    return LOCALE_NAMES.get(key) or NAMES.get(_base(lang)) or lang


def wanted(prompt=None, transcript=None, cfg=None) -> tuple:
    """(language, how) the reply must be in: ("fr", "set") for a set locale; ("es", "latest") from the developer's
    message; ("es", "earlier") from an earlier one when it is unclear; (None, "") when nothing tells."""
    value = setting(cfg)
    if value != AUTO:
        return _base(value), "set"
    if prompt and not prompt.lstrip().startswith(_NOT_HUMAN):
        lang = detect(prompt)
        if lang:
            return lang, "latest"
    first = True
    for text in developer_messages(transcript):
        lang = detect(text)
        if lang:
            return lang, "latest" if first and not prompt else "earlier"
        first = False
    return None, ""


def prompt_line(prompt: str, transcript=None, cfg=None) -> str:
    """The UserPromptSubmit line that says what to reply in; '' when it is English. It names a language only, never
    the developer's words."""
    value = setting(cfg)
    lang, how = wanted(prompt, transcript, cfg)
    if not lang or lang == "en":
        return ""
    who = name(lang)
    if how == "set":
        return (f"devkit · devkit's language is set to {value}: reply in {who} whatever language the developer "
                f"writes in ({STAYS})")
    said = "wrote" if how == "latest" else "has been writing"
    return f"devkit · the developer {said} in {who}: reply in {who} ({STAYS})"


def session_line(cfg=None) -> str:
    """The session-start line for a set locale; '' with auto (each prompt gets its own line)."""
    value = setting(cfg)
    if value == AUTO:
        return ""
    return (f"Language: this developer set devkit's language to {value}: reply in {name(value)} whatever language "
            f"they write in; code, comments, commits, PRs, docs and devkit's records stay in American English unless "
            f"they ask otherwise.")
