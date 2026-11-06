# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The crew's RACI works the same in any language: a step's kind of work rests on language-neutral anchors (an
# explicit activity id, the assigned member's Responsible activity, the files in its scope), never on the developer's
# words. The Oracle's crew gap review (2026-11-06), findings F3, F8 and F22, and requirement C13.
# Run: python3 -m unittest tests.test_multilingual
import io
import json
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import cli, crew, raci  # noqa: E402

# Every row of The Oracle's misroutes table (29 tasks: 21 written in English, 8 in Spanish), each in both languages,
# with the kind of work the model declares for it and the member who should do it.
MISROUTES = [
    ("upgrade the app from Spring Boot 2.7 to 3.3", "actualiza la app de Spring Boot 2.7 a 3.3", "migration",
     "trainman"),
    ("add Terraform for a new S3 bucket with lifecycle rules",
     "añade Terraform para un bucket S3 nuevo con reglas de ciclo de vida", "infrastructure", "niobe"),
    ("set up Dependabot for the repo", "configura Dependabot en el repositorio", "dependencies", "link"),
    ("rotate the leaked Stripe API key and purge it from git history",
     "rota la clave de API de Stripe filtrada y bórrala del historial de git", "security", "keymaker"),
    ("make the checkout form accessible for screen readers",
     "haz el formulario de pago accesible para lectores de pantalla", "accessibility", "trinity"),
    ("translate the app into Spanish (i18n)", "traduce la app al inglés (i18n)", "i18n", "trinity"),
    ("add SSO with Okta to the admin portal", "añade SSO con Okta al portal de administración", "sso", "keymaker"),
    ("estimate how long the invoice export will take", "estima cuánto tardará la exportación de facturas", "plan",
     "morpheus"),
    ("plan the work for the new billing feature", "planifica el trabajo de la nueva funcionalidad de facturación",
     "plan", "morpheus"),
    ("resolve the merge conflicts and integrate the branches", "resuelve los conflictos de merge e integra las ramas",
     "plan", "morpheus"),
    ("create a design system with tokens for the web app", "crea un sistema de diseño con tokens para la app web",
     "design-system", "sati"),
    ("set up a new agent feature flag", "configura un feature flag nuevo para el agente", "feature", "neo"),
    ("build the approval workflow for invoices", "construye el flujo de aprobación de facturas", "feature", "neo"),
    ("update all npm dependencies", "actualiza todas las dependencias de npm", "dependencies", "link"),
    ("bump lodash to fix CVE-2026-1234", "sube lodash para corregir el CVE-2026-1234", "security", "keymaker"),
    ("optimize the slow orders query", "optimiza la consulta lenta de pedidos", "performance", "merovingian"),
    ("explain how the payment flow works", "explica cómo funciona el flujo de pago", "codebase", "sparks"),
    ("explain how the cache works", "explica cómo funciona la caché", "codebase", "sparks"),
    ("set up a canary deployment for the API", "configura un despliegue canary para la API", "infrastructure",
     "niobe"),
    ("write the ETL job that loads orders into the warehouse",
     "escribe el job de ETL que carga los pedidos en el almacén de datos", "database", "dozer"),
    ("create a new devkit skill for Terraform", "crea una skill nueva de devkit para Terraform", "tooling", "kid"),
    ("the login page is blank in Safari", "la página de login sale en blanco en Safari", "frontend", "trinity"),
    ("fix the database error when the cart is empty",
     "arregla el error de la base de datos cuando el carrito está vacío", "feature", "neo"),
    ("write the acceptance criteria for the returns flow",
     "escribe los criterios de aceptación del flujo de devoluciones", "requirements", "oracle"),
    ("design the user sign-up screens", "diseña las pantallas de alta de usuarios", "ux", "sati"),
    ("the orders query is very slow", "la consulta de pedidos es muy lenta", "performance", "merovingian"),
    ("migrate the app from Angular 15 to 18", "migra la app de Angular 15 a 18", "migration", "trainman"),
    ("which library do we use to generate PDFs?", "¿qué librería usamos para generar PDFs?", "research", "roland"),
    ("where is the retry policy configured?", "¿dónde se configura la política de reintentos?", "lookup", "sparks"),
]

# The kinds of work no activity covered (F22).
NEW_WORK = ("i18n", "dependencies", "accessibility", "sso", "design-system", "codebase", "backup")


def _roster():
    return crew.load(team=False)


class DeclaredWorkTests(unittest.TestCase):
    def test_a_declared_kind_of_work_routes_every_misroute_the_same_in_english_and_spanish(self):
        r = _roster()
        ids = [m["id"] for m in crew.members(r)]
        for en, es, aid, want in MISROUTES:
            act = raci.activity(aid, r)
            with self.subTest(task=en, activity=aid):
                self.assertIsNotNone(act, "no activity for this kind of work")
                self.assertIn(want, act["R"])
                outsider = next(m for m in ids if m not in act["R"] + act["C"] + [act["A"]])
                for text in (en, es):
                    self.assertEqual(raci.anchor(text, member="neo", activity=aid, roster=r), (act, "explicit"))
                    self.assertIsNone(raci.check_assignment(want, text, r, activity=aid))
                    self.assertIsNotNone(raci.check_assignment(outsider, text, r, activity=aid))

    def test_an_explicit_activity_comes_first_and_an_unknown_one_is_refused(self):
        r = _roster()
        self.assertEqual(raci.step_activity({"name": "api", "label": "Tank", "activity": "database"}, r)["id"],
                         "database")
        refused = raci.check_assignment("kid", "paso 2", r, activity="backend")
        self.assertIn("Backend engineer (Tank) does it", refused)
        self.assertIn("--to Tank", refused)
        with self.assertRaises(ValueError) as e:
            raci.resolve_activity("nope", r)
        self.assertIn("backend", str(e.exception))
        self.assertEqual(raci.resolve_activity("Back-end services and APIs", r)["id"], "backend")
        self.assertEqual(raci.split_activity("api=1h after design [backend]"), ("api=1h after design", "backend"))
        self.assertEqual(raci.split_activity("docs=30m"), ("docs=30m", ""))

    def test_the_assign_command_takes_an_activity_and_refuses_an_unknown_one(self):
        a = cli.build_parser().parse_args(["progress", "assign", "api", "--to", "Tank", "--activity", "backend"])
        self.assertEqual(a.activity, "backend")
        bad = cli.build_parser().parse_args(["progress", "assign", "api", "--to", "Tank", "--activity", "nope"])
        err = io.StringIO()
        with redirect_stderr(err):
            code = bad.fn(bad)  # refused before any run is read or changed
        self.assertEqual(code, 2)
        self.assertIn("no kind of work “nope”", err.getvalue())


class AnchorTests(unittest.TestCase):
    def test_a_step_assigned_to_tank_is_backend_work_whatever_its_name_says(self):
        r = _roster()
        for name in ("api", "paso 2", "API de facturas"):
            with self.subTest(step=name):
                self.assertEqual(raci.step_activity({"name": name, "label": "Tank"}, r)["id"], "backend")
        run = {"plan": [{"name": "api", "label": "Tank", "state": "done"}]}
        self.assertEqual(raci.approvals_missing(run, lambda s: "tank", r), [("seraph", ["api"])])

    def test_the_files_in_scope_name_the_kind_of_work_when_nothing_else_does(self):
        r = _roster()
        cases = [("API de facturas", ["services/**"], "backend"), ("paso 3", ["db/migrations/**"], "database"),
                 ("tarea", [".github/workflows/ci.yml"], "cicd"), ("ajustes", ["web/settings.tsx"], "frontend")]
        for name, files, want in cases:
            with self.subTest(step=name, files=files):
                self.assertEqual(raci.step_activity({"name": name, "scope": files}, r)["id"], want)
                self.assertEqual(raci.anchor(name, files=files, roster=r), (raci.activity(want, r), "files"))

    def test_only_the_declared_kind_of_work_refuses_an_assignment_never_its_files_or_words(self):
        # since R2-2 (d60931e) the words refuse nothing in any language: backend work is declared, as in test_raci
        r = _roster()
        for text in ("build the JSON API server", "construye el servidor de la API JSON"):
            with self.subTest(text=text):
                self.assertIsNotNone(raci.check_assignment("kid", text, r, activity="backend"))
                self.assertIsNone(raci.check_assignment("kid", text, r, files=["bin/**"]))
                self.assertIsNotNone(raci.check_assignment("kid", text, r, activity="backend", files=["services/**"]))

    def test_an_override_keeps_the_kind_of_work_so_its_own_approver_signs(self):
        # the override names the kind of work it overrides (--activity cicd --why "…"), and that kind's approver signs
        r = _roster()
        for text in ("fix the failing GitHub Actions workflow", "arregla el workflow de GitHub Actions que falla"):
            with self.subTest(text=text):
                self.assertIsNotNone(raci.check_assignment("kid", text, r, activity="cicd"))
                a, source = raci.anchor(text, member="kid", activity="cicd", roster=r)
                self.assertEqual((a["id"], a["A"], source), ("cicd", "keymaker", "explicit"))

    def test_a_member_with_several_kinds_of_work_gets_the_one_its_step_names(self):
        r = _roster()
        self.assertEqual(raci.anchor("release", member="link", roster=r)[0]["id"], "release")
        self.assertEqual(raci.anchor("paso 4", member="link", roster=r), (raci.activity("cicd", r), "member"))


class RosterTests(unittest.TestCase):
    def test_the_shipped_roster_stays_valid_with_the_new_kinds_of_work(self):
        data = json.loads(crew.ROSTER.read_text())
        self.assertEqual(crew.check(data), ([], []))
        r = _roster()
        for aid in NEW_WORK:
            a = raci.activity(aid, r)
            with self.subTest(activity=aid):
                self.assertIsNotNone(a, "missing activity")
                self.assertTrue(a["R"] and a["A"] and isinstance(a["C"], list) and isinstance(a["I"], list))
                self.assertNotIn(a["A"], a["R"])  # nobody approves their own kind of work
                self.assertTrue(a["knowledge"]["skills"])


if __name__ == "__main__":
    unittest.main()
