# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The evaluation set of the reply-language rule (lib/devkit/language.py), shared by tests/test_language.py and
# tests/test_canary_language.py: real-looking developer messages and final replies per language, the unsure cases that
# must give no answer, and the mixed texts that must not flip it. Data only, with no import of devkit, so a test that
# uses it still loads, and fails, without the change it covers.
import json
from pathlib import Path

# ---------------------------------------------------------------------------------------------------- the eval set
# Developer messages, as typed: questions, requests, short decisions.
PROMPTS = {
    "en": ["Can you check why the login test fails on CI and fix it?",
           "Please add a retry with backoff to the webhook sender, and make sure the tests still pass.",
           "What does the guard do when a branch name has a slash in it?",
           "Let's ship the hotfix once the review is done.",
           "The dashboard is slow again after the last release, can you take a look?"],
    "es": ["¿Puedes revisar por qué falla el test de login en CI y arreglarlo?",
           "Añade un reintento con backoff al envío de webhooks y asegúrate de que los tests siguen pasando.",
           "¿Qué hace el guard cuando el nombre de la rama tiene una barra?",
           "Vale, cuando termine la revisión publicamos el hotfix.",
           "El dashboard va lento otra vez desde la última release, échale un vistazo por favor.",
           "si el usuario habla en inglés deberías hablar en inglés, en español español, etc."],
    "pt": ["Você pode ver por que o teste de login falha no CI e corrigir?",
           "Adiciona um retry com backoff no envio de webhooks e garante que os testes continuam passando.",
           "O que o guard faz quando o nome da branch tem uma barra?",
           "Quando a revisão terminar, a gente publica o hotfix.",
           "O dashboard está lento de novo depois do último release, dá uma olhada por favor."],
    "fr": ["Tu peux regarder pourquoi le test de login échoue sur la CI et le corriger ?",
           "Ajoute un retry avec backoff à l'envoi des webhooks et vérifie que les tests passent toujours.",
           "Qu'est-ce que fait le guard quand le nom de la branche contient un slash ?",
           "Quand la revue est terminée, on publie le hotfix.",
           "Le dashboard est encore lent depuis la dernière release, tu peux jeter un œil ?"],
    "it": ["Puoi controllare perché il test di login fallisce in CI e sistemarlo?",
           "Aggiungi un retry con backoff all'invio dei webhook e assicurati che i test passino ancora.",
           "Cosa fa il guard quando il nome del branch contiene uno slash?",
           "Quando la revisione è finita, pubblichiamo l'hotfix.",
           "La dashboard è di nuovo lenta dopo l'ultimo rilascio, puoi darci un'occhiata?"],
    "de": ["Kannst du prüfen, warum der Login-Test in der CI fehlschlägt, und ihn reparieren?",
           "Füge beim Versand der Webhooks einen Retry mit Backoff hinzu und stelle sicher, dass die Tests noch "
           "laufen.",
           "Was macht der Guard, wenn der Branch-Name einen Schrägstrich enthält?",
           "Wenn das Review fertig ist, veröffentlichen wir den Hotfix.",
           "Das Dashboard ist seit dem letzten Release wieder langsam, kannst du es dir ansehen?"],
    "ca": ["Pots revisar per què falla el test de login a la CI i arreglar-ho?",
           "Afegeix un reintent amb backoff a l'enviament dels webhooks i assegura't que els tests continuen passant.",
           "Què fa el guard quan el nom de la branca té una barra?",
           "Quan acabi la revisió, publiquem el hotfix.",
           "El dashboard torna a anar lent des de l'última versió, hi pots fer un cop d'ull?"],
    "nl": ["Kun je kijken waarom de login-test faalt in de CI en het oplossen?",
           "Voeg een retry met backoff toe aan het versturen van webhooks en zorg dat de tests nog slagen.",
           "Wat doet de guard als de naam van de branch een slash bevat?",
           "Als de review klaar is, publiceren we de hotfix.",
           "Het dashboard is weer traag sinds de laatste release, kun je er even naar kijken?"],
}

# Too short or not prose: no answer, whatever the language may be.
UNSURE = ["ok", "continua", "continúa", "sí", "yes", "dale", "go", "lgtm", "ship it", "do it", "hazlo", "merci",
          "👍", "1", "PR 12", "/devkit:status", "dev test", "`dev progress`", "~/Development/acme/api/app.py",
          "https://github.com/acme/api/pull/12", "```\nTraceback (most recent call last):\n  boom\n```", ""]


def reply(lang):
    return REPLIES[lang]


REPLIES = {
    "en": """**[Firefly> DevKit]** devkit · `hotfix/26.11.17` · Reply language
- **Done:** added the language detection and the check at the end of the turn.
- **Check:** ✓ `python3 -m unittest tests.test_language` passes, 42 tests
- **Next:** the Code reviewer (Seraph) reviews the change.

The change is in `lib/devkit/language.py`. To try it:

```bash
dev test run tests/test_language.py
```

When the developer writes in Spanish, the reply is in Spanish; commits stay in English.""",
    "es": """**[Firefly> DevKit]** devkit · `hotfix/26.11.17` · Idioma de las respuestas
- **Done:** añadí la detección del idioma y la comprobación al final del turno.
- **Check:** ✓ `python3 -m unittest tests.test_language` pasa, 42 tests
- **Next:** el Revisor de código (Seraph) revisa el cambio.

El cambio está en `lib/devkit/language.py`. Para probarlo:

```bash
dev test run tests/test_language.py
```

Si el desarrollador escribe en español, la respuesta sale en español; los commits siguen en inglés.""",
    "pt": """**[Firefly> DevKit]** devkit · `hotfix/26.11.17` · Idioma das respostas
- **Done:** adicionei a detecção do idioma e a verificação no fim do turno.
- **Check:** ✓ `python3 -m unittest tests.test_language` passa, 42 testes
- **Next:** o Revisor de código (Seraph) revisa a mudança.

A mudança está em `lib/devkit/language.py`. Quando o desenvolvedor escreve em português, a resposta sai em
português; os commits continuam em inglês.""",
    "fr": """**[Firefly> DevKit]** devkit · `hotfix/26.11.17` · Langue des réponses
- **Done:** j'ai ajouté la détection de la langue et la vérification à la fin du tour.
- **Check:** ✓ `python3 -m unittest tests.test_language` passe, 42 tests
- **Next:** le Relecteur de code (Seraph) relit le changement.

Le changement est dans `lib/devkit/language.py`. Quand le développeur écrit en français, la réponse est en
français ; les commits restent en anglais.""",
    "it": """**[Firefly> DevKit]** devkit · `hotfix/26.11.17` · Lingua delle risposte
- **Done:** ho aggiunto il rilevamento della lingua e il controllo alla fine del turno.
- **Check:** ✓ `python3 -m unittest tests.test_language` passa, 42 test
- **Next:** il Revisore del codice (Seraph) controlla la modifica.

La modifica è in `lib/devkit/language.py`. Quando lo sviluppatore scrive in italiano, la risposta è in italiano; i
commit restano in inglese.""",
    "de": """**[Firefly> DevKit]** devkit · `hotfix/26.11.17` · Sprache der Antworten
- **Done:** ich habe die Spracherkennung und die Prüfung am Ende der Runde hinzugefügt.
- **Check:** ✓ `python3 -m unittest tests.test_language` läuft durch, 42 Tests
- **Next:** der Code-Reviewer (Seraph) prüft die Änderung.

Die Änderung ist in `lib/devkit/language.py`. Wenn der Entwickler auf Deutsch schreibt, ist die Antwort auf
Deutsch; die Commits bleiben auf Englisch.""",
    "ca": """**[Firefly> DevKit]** devkit · `hotfix/26.11.17` · Idioma de les respostes
- **Done:** he afegit la detecció de l'idioma i la comprovació al final del torn.
- **Check:** ✓ `python3 -m unittest tests.test_language` passa, 42 tests
- **Next:** el Revisor de codi (Seraph) revisa el canvi.

El canvi és a `lib/devkit/language.py`. Quan el desenvolupador escriu en català, la resposta surt en català; els
commits continuen en anglès.""",
    "nl": """**[Firefly> DevKit]** devkit · `hotfix/26.11.17` · Taal van de antwoorden
- **Done:** ik heb de taaldetectie en de controle aan het einde van de beurt toegevoegd.
- **Check:** ✓ `python3 -m unittest tests.test_language` slaagt, 42 tests
- **Next:** de Codereviewer (Seraph) bekijkt de wijziging.

De wijziging staat in `lib/devkit/language.py`. Als de ontwikkelaar in het Nederlands schrijft, is het antwoord in
het Nederlands; de commits blijven in het Engels.""",
}

# A Spanish reply that quotes English command output three ways: a code block, a block quote and a quoted line.
SPANISH_QUOTING_ENGLISH = """**[Firefly> DevKit]** api · `fix/login-timeout` · Timeout del login
- **Done:** encontré la causa del fallo: el pool de conexiones se agota.
- **Check:** ⚠ el test sigue fallando en CI
- **Next:** el Ingeniero de backend (Tank) sube el límite del pool.

El job de CI termina con este error:

```
FAILED tests/test_login.py::test_timeout - AssertionError: expected the request to finish in 2s but it took 31s
The connection pool is exhausted and there are no idle connections left in the pool for this worker
```

> Error: the connection pool is exhausted, all of the connections are in use and the worker has to wait for one

Y el log dice "the pool was closed while the request was still waiting for a connection to the database".
Lo arreglo subiendo el límite y añadiendo un timeout explícito."""

ENGLISH_WITH_SPANISH_NAMES = """**[Firefly> DevKit]** api · `main` · Mexico City export
- **Done:** fixed the export for the Ciudad de México office.
- **Check:** ✓ 18 tests pass
- **Next:** María José de la Fuente and José Luis del Valle check the report from Guadalajara.

The report now groups the rows by the office's city, so Ciudad de México and San Luis Potosí each get their own
sheet. I sent the file to Juan Carlos de los Santos, who owns the export."""


def claude_transcript(path, prompts):
    # a Claude Code transcript: the developer's prompts among tool results, hook context and notifications
    rows = []
    for p in prompts:
        rows.append({"type": "user", "message": {"role": "user", "content": p}})
        rows.append({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working on it."}]}})
        rows.append({"type": "user", "message": {"content": [{"type": "tool_result", "content":
                                                               "The tests passed and the build is green now."}]}})
    Path(path).write_text("".join(json.dumps(r) + "\n" for r in rows))
