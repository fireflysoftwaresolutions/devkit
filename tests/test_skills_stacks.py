# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The skill rules of the Vue, Svelte, Next.js, .NET, Rust, Ruby, Go, iOS and Android stacks (config/skill-rules.json
# through skillguard): each fixture path with its content and the rule ids it must trigger, and the near misses that
# must trigger none, such as a command-line tool's Cargo.toml or a server-side Swift file. Tests carry comments, not
# docstrings: `dev test prove` leaves out tests with a docstring. Run: python3 -m unittest tests.test_skills_stacks
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import skillguard as sg  # noqa: E402
from tests.test_skillguard import make_repo  # noqa: E402

# One repository per stack, so a manifest at the top (Cargo.toml, Gemfile, go.mod) is the stack's own.
STACKS = {
    "vue": {
        "web/src/App.vue": "<script setup lang=\"ts\">\nconst n = 1\n</script>\n<template><p>{{ n }}</p></template>\n",
        "web/src/stores/cart.ts": "import { defineStore } from 'pinia'\nexport const useCart = defineStore('cart', {})\n",
        "web/src/composables/useCount.ts": "import { ref } from 'vue'\nexport const useCount = () => ref(0)\n",
        "nuxt.config.ts": "export default defineNuxtConfig({ devtools: { enabled: true } })\n",
        "plugins/api.ts": "import {\n  defineNuxtPlugin,\n  useRuntimeConfig,\n} from '#app'\n"
                          "export default defineNuxtPlugin(() => useRuntimeConfig())\n",
        "web/src/hooks/useToggle.ts": "import { useState } from 'react'\nexport const useToggle = () => useState(false)\n",
        "web/src/utils/sum.ts": "export const sum = (a: number, b: number) => a + b\n",
    },
    "svelte": {
        "src/routes/+page.svelte": "<script>\n  let count = $state(0);\n</script>\n<button onclick={() => count++}>{count}</button>\n",
        "src/lib/counter.svelte.ts": "export const counter = $state({ value: 0 });\n",
        "svelte.config.js": "import adapter from '@sveltejs/adapter-auto';\nexport default { kit: { adapter: adapter() } };\n",
        "src/routes/+page.ts": "import { error } from '@sveltejs/kit';\nexport const load = () => { error(404); };\n",
        "src/lib/format.ts": "export const format = (n: number) => n.toFixed(2);\n",
        "src/lib/viewport.ts": "import { innerWidth } from 'svelte/reactivity/window';\nexport const wide = () => innerWidth.current > 800;\n",
    },
    "next": {
        "next.config.ts": "const nextConfig = { reactStrictMode: true };\nexport default nextConfig;\n",
        "app/api/orders/route.ts": "import { NextResponse } from 'next/server';\n"
                                   "export async function GET() { return NextResponse.json([]); }\n",
        "app/page.tsx": "export default function Page() { return <main className=\"p-4\">Hi</main>; }\n",
        "lib/money.ts": "export const cents = (n: number) => Math.round(n * 100);\n",
    },
    "dotnet": {
        "Api/Api.csproj": '<Project Sdk="Microsoft.NET.Sdk.Web">\n  <PropertyGroup><TargetFramework>net10.0</TargetFramework>'
                          "</PropertyGroup>\n</Project>\n",
        "Api/Controllers/OrdersController.cs": "[ApiController]\n[Route(\"orders\")]\n"
                                               "public class OrdersController : ControllerBase { }\n",
        "Api/Program.cs": "var app = WebApplication.CreateBuilder(args).Build();\n"
                          "app.MapGet(\"/orders/{id}\", (int id) => TypedResults.Ok(id));\napp.Run();\n",
        "Api/Migrations/20261122093000_Init.cs": "public partial class Init : Migration { }\n",
        "Api/Domain/Money.cs": "public sealed record Money(decimal Amount, string Currency);\n",
        "Api.Tests/OrdersApiTests.cs": "public class OrdersApiTests : IClassFixture<WebApplicationFactory<Program>> {\n"
                                       "  // app.MapGet(\"/fake\", () => 1) in a test host\n}\n",
        "Desktop/Desktop.csproj": '<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup><UseMaui>true</UseMaui>'
                                  "</PropertyGroup>\n</Project>\n",
    },
    "node": {
        "package.json": '{"name": "tool", "imports": {"#app": "./src/app.js"}}\n',
        "src/main.js": "import config from '#app';\nimport { start } from '#app';\nstart(config);\n",
        "src/registry.ts": "const all: Array<ApplicationController> = [];\nlet current: ControllerBase;\n",
        "src/cache.js": "const v = m.mapGet(key);\nconst verbs = [httpGet, httpPost];\n",
        "src/game.ts": "class Level<ApplicationController> { }\nscene.mapGroup('enemies');\n",
        "tools/bind.py": "def bind(c: controller):\n    return c\n",
        "tools/routes.py": "class Routes(object):\n    Controller = None\n",
    },
    "csharp": {
        "Assets/Scripts/PlayerController.cs": "using UnityEngine;\n\npublic class PlayerController : MonoBehaviour\n{\n"
                                              "    void Update() { }\n}\n",
        "Platforms/iOS/ViewController.cs": "using UIKit;\n\npublic class ViewController : UIViewController { }\n",
        "Web/Controllers/HomeController.cs": "using Microsoft.AspNetCore.Mvc;\n\npublic class HomeController : Controller"
                                             "\n{\n    public IActionResult Index() => View();\n}\n",
        "Web/Controllers/CartController.cs": "public sealed class CartController(ICart cart) : ControllerBase\n{\n"
                                             "    public IActionResult Get() => Ok(cart.Lines);\n}\n",
        "Web/Controllers/ShopController.cs": "public class ShopController\n    : Controller\n{\n}\n",
    },
    "rust": {
        "Cargo.toml": '[package]\nname = "orders"\n\n[dependencies]\naxum = "0.8"\ntokio = { version = "1", '
                      'features = ["full"] }\n',
        "src/routes.rs": "pub fn app() -> Router {\n    Router::new().route(\"/orders/{id}\", get(show))\n}\n",
        "src/health.rs": "#[get(\"/health\")]\nasync fn health() -> impl Responder { \"ok\" }\n",
        "src/money.rs": "pub struct Money { pub cents: i64 }\n",
        "migrations/20261122093000_init.sql": "create table orders (id bigint primary key);\n",
        "tools/cli/Cargo.toml": '[package]\nname = "cli"\n\n[dependencies]\nclap = "4"\n',
        "services/api/Cargo.toml": '[package]\nname = "api"\n\n[dependencies.axum]\nversion = "0.8"\nfeatures = ["macros"]\n',
    },
    "ruby": {
        "Gemfile": 'source "https://rubygems.org"\ngem "rails", "~> 8.1"\ngem "puma"\n',
        "config/routes.rb": "Rails.application.routes.draw do\n  resources :orders\nend\n",
        "app/controllers/orders_controller.rb": "class OrdersController < ApplicationController\nend\n",
        "service/app.rb": "require 'sinatra'\n\nget '/health' do\n  'ok'\nend\n",
        "db/migrate/20261122093000_create_orders.rb": "class CreateOrders < ActiveRecord::Migration[8.1]\nend\n",
        "lib/money.rb": "class Money\n  def initialize(cents) = @cents = cents\nend\n",
        "app/admin/dashboard.rb": "module Admin\n  class Dashboard < Admin::ApplicationController # the admin area\n"
                                  "  end\nend\n",
        "spec/models/order_spec.rb": "RSpec.describe Order do\n  it { expect(Order.new).to be_valid }\nend\n",
        "spec/rails_helper.rb": "require 'spec_helper'\nActionController::Base.allow_forgery_protection = false\n",
        "spec/support/factories.rb": "FactoryBot.define do\n  factory(:order) { total { 10 } }\nend\n",
        "gems/tool/Gemfile": 'source "https://rubygems.org"\ngem "rake"\n',
    },
    "rails_pg": {
        "Gemfile": 'source "https://rubygems.org"\ngem "rails", "~> 8.1"\ngem "pg", "~> 1.5"\n',
        "db/migrate/20261122093000_create_orders.rb": "class CreateOrders < ActiveRecord::Migration[8.1]\nend\n",
    },
    "efcore_pg": {
        "src/Api/Api.csproj": '<Project Sdk="Microsoft.NET.Sdk.Web">\n  <ItemGroup>\n'
                              '    <PackageReference Include="Npgsql" Version="10.0.0" />\n  </ItemGroup>\n</Project>\n',
        "src/Api/Migrations/20261122093000_Init.cs": "public partial class Init : Migration { }\n",
    },
    "contracts": {
        "spec/openapi.yaml": "openapi: 3.1.0\ninfo: { title: Orders, version: '1' }\npaths: {}\n",
        "spec/examples/order.json": '{"id": 1, "total": 10}\n',
        "spec/README.md": "# Orders API contract\n",
    },
    "go": {
        "go.mod": "module example.com/orders\n\ngo 1.27\n\nrequire github.com/jackc/pgx/v5 v5.7.0\n",
        "cmd/api/main.go": "package main\n\nfunc routes(m *Mux) {\n\tm.HandleFunc(\"GET /orders/{id}\", show)\n}\n",
        "internal/money/money.go": "package money\n\ntype Money struct{ Cents int64 }\n",
        "tools/cli/go.mod": "module example.com/cli\n\ngo 1.27\n\nrequire (\n\tgithub.com/spf13/cobra v1.9.0\n"
                            "\tgoogle.golang.org/grpc v1.70.0 // indirect\n\tgithub.com/lib/pq v1.10.9 //indirect\n)\n",
        "services/billing/go.mod": "module example.com/billing\n\ngo 1.27\n\nrequire (\n"
                                   "\tgolang.org/x/text v0.20.0 // indirect\n"
                                   "\tgithub.com/go-chi/chi/v5 v5.2.0 // routes, see ADR 7\n)\n",
    },
    "ios": {
        "App/ContentView.swift": "import SwiftUI\n\nstruct ContentView: View {\n    var body: some View { Text(\"Hi\") }\n}\n",
        "App/LegacyViewController.swift": "import UIKit\n\nfinal class LegacyViewController: UIViewController {}\n",
        "App/PrivacyInfo.xcprivacy": "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<plist version=\"1.0\"><dict/></plist>\n",
        "App/Info.plist": "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<plist version=\"1.0\"><dict/></plist>\n",
        "AppTests/ContentViewTests.swift": "import Testing\n@testable import App\n\n@Test func renders() {}\n",
        "Server/Sources/App/routes.swift": "import Vapor\n\nfunc routes(_ app: Application) throws {}\n",
    },
    "android": {
        "app/src/main/AndroidManifest.xml": "<manifest xmlns:android=\"http://schemas.android.com/apk/res/android\" />\n",
        "app/src/main/java/acme/ui/Home.kt": "package acme.ui\n\nimport androidx.compose.material3.Text\n",
        "app/build.gradle.kts": "plugins {\n    id(\"com.android.application\")\n}\n",
        "server/build.gradle.kts": "plugins {\n    kotlin(\"jvm\")\n}\n",
        "legacy/build.gradle": "plugins {\n    id 'com.android.application'\n}\n",
        "shared/build.gradle": "plugins {\n    id 'java-library'\n}\n",
        "server/src/main/kotlin/acme/Api.kt": "package acme\n\nimport org.springframework.web.bind.annotation.RestController\n",
    },
}

EXPECT = {
    "vue": {
        "web/src/App.vue": ["ui", "vue"],
        "web/src/stores/cart.ts": ["vue"],
        "web/src/composables/useCount.ts": ["vue"],
        "nuxt.config.ts": ["vue"],
        "plugins/api.ts": ["vue"],
        "web/src/hooks/useToggle.ts": [],
        "web/src/utils/sum.ts": [],
    },
    "svelte": {
        "src/routes/+page.svelte": ["svelte", "ui"],
        "src/lib/counter.svelte.ts": ["svelte"],
        "svelte.config.js": ["svelte"],
        "src/routes/+page.ts": ["svelte"],
        "src/lib/format.ts": [],
        "src/lib/viewport.ts": ["svelte"],
    },
    "next": {
        "next.config.ts": ["nextjs"],
        "app/api/orders/route.ts": ["nextjs"],
        "app/page.tsx": ["react", "ui"],
        "lib/money.ts": [],
    },
    "dotnet": {
        "Api/Api.csproj": ["dotnet"],
        "Api/Controllers/OrdersController.cs": ["api", "aspnetcore"],
        "Api/Program.cs": ["api", "aspnetcore"],
        "Api/Migrations/20261122093000_Init.cs": ["database"],
        "Api/Domain/Money.cs": [],
        "Api.Tests/OrdersApiTests.cs": ["testing"],
        "Desktop/Desktop.csproj": [],
    },
    "node": {
        "package.json": [],
        "src/main.js": [],
        "src/registry.ts": [],
        "src/cache.js": [],
        "src/game.ts": [],
        "tools/bind.py": [],
        "tools/routes.py": [],
    },
    "csharp": {
        "Assets/Scripts/PlayerController.cs": [],
        "Platforms/iOS/ViewController.cs": [],
        "Web/Controllers/HomeController.cs": ["api", "aspnetcore"],
        "Web/Controllers/CartController.cs": ["api", "aspnetcore"],
        "Web/Controllers/ShopController.cs": ["api", "aspnetcore"],
    },
    "rust": {
        "Cargo.toml": ["rust"],
        "src/routes.rs": ["api"],
        "src/health.rs": ["api"],
        "src/money.rs": [],
        "migrations/20261122093000_init.sql": ["database"],
        "tools/cli/Cargo.toml": [],
        "services/api/Cargo.toml": ["rust"],
    },
    "ruby": {
        "Gemfile": ["ruby"],
        "config/routes.rb": ["api"],
        "app/controllers/orders_controller.rb": ["api"],
        "service/app.rb": ["api"],
        "db/migrate/20261122093000_create_orders.rb": ["database"],
        "lib/money.rb": [],
        "app/admin/dashboard.rb": ["api"],
        "spec/models/order_spec.rb": ["testing"],
        "spec/rails_helper.rb": ["testing"],
        "spec/support/factories.rb": ["testing"],
        "gems/tool/Gemfile": [],
    },
    "rails_pg": {
        "Gemfile": ["ruby"],
        "db/migrate/20261122093000_create_orders.rb": ["database", "postgres"],
    },
    "efcore_pg": {
        "src/Api/Api.csproj": ["dotnet"],
        "src/Api/Migrations/20261122093000_Init.cs": ["database", "postgres"],
    },
    "contracts": {
        "spec/openapi.yaml": ["api"],
        "spec/examples/order.json": [],
        "spec/README.md": ["docs"],
    },
    "go": {
        "go.mod": ["go"],
        "cmd/api/main.go": ["api"],
        "internal/money/money.go": [],
        "tools/cli/go.mod": [],
        "services/billing/go.mod": ["go"],
    },
    "ios": {
        "App/ContentView.swift": ["mobile", "swiftui"],
        "App/LegacyViewController.swift": ["mobile"],
        "App/PrivacyInfo.xcprivacy": ["mobile"],
        "App/Info.plist": ["mobile"],
        "AppTests/ContentViewTests.swift": ["testing"],
        "Server/Sources/App/routes.swift": [],
    },
    "android": {
        "app/src/main/AndroidManifest.xml": ["android-manifest", "mobile"],
        "app/src/main/java/acme/ui/Home.kt": ["mobile"],
        "app/build.gradle.kts": ["mobile"],
        "server/build.gradle.kts": [],
        "legacy/build.gradle": ["mobile"],
        "shared/build.gradle": [],
        "server/src/main/kotlin/acme/Api.kt": ["spring"],
    },
}


class StackRuleCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.roots = {name: make_repo(Path(self.tmp.name) / name, files) for name, files in STACKS.items()}

    def tearDown(self):
        self.tmp.cleanup()

    def ids_by_file(self, stack: str) -> dict:
        root = self.roots[stack]
        out = {rel: [] for rel in STACKS[stack]}
        for rule, files in sg.rules_for_files([str(root / rel) for rel in STACKS[stack]]):
            for f in files:
                out[str(Path(f).relative_to(root))].append(rule["id"])
        return {rel: sorted(ids) for rel, ids in out.items()}

    def assert_stack(self, stack: str):
        got = self.ids_by_file(stack)
        wrong = {k: f"got {got[k]}, want {sorted(v)}" for k, v in EXPECT[stack].items() if got[k] != sorted(v)}
        self.assertEqual(wrong, {})

    def skills_for(self, stack: str, rel: str) -> list:
        path = str(self.roots[stack] / rel)
        return sorted({s for r, _ in sg.rules_for_files([path]) for s in sg.required(r, "claude")})


class FrontendFrameworkRules(StackRuleCase):
    # Vue single-file components, Vue modules (Pinia, composables) and the Nuxt config need the Vue skill; a React
    # hook or a plain module of the same web app does not
    def test_vue_rule_fires_on_vue_code_only(self):
        self.assert_stack("vue")

    # Svelte components, rune modules (.svelte.ts), SvelteKit modules, modules that import a nested Svelte entry
    # (svelte/reactivity/window) and the config need the Svelte skill
    def test_svelte_rule_fires_on_svelte_code_only(self):
        self.assert_stack("svelte")

    # the Next.js config and modules that import next need the React skill through the nextjs rule; a page in TSX
    # stays with the react and ui rules
    def test_nextjs_rule_fires_on_next_code_only(self):
        self.assert_stack("next")

    def test_frameworks_require_their_own_skills(self):
        self.assertEqual(self.skills_for("vue", "web/src/stores/cart.ts"), ["vue-best-practices"])
        self.assertEqual(self.skills_for("svelte", "src/lib/counter.svelte.ts"), ["svelte-core-bestpractices"])
        self.assertEqual(self.skills_for("next", "next.config.ts"), ["react-best-practices"])

    # Next.js commands belong to the nextjs rule, also behind a wrapper such as npx; a plain build does not
    def test_next_commands_need_the_nextjs_rule(self):
        def ids(cmd):
            return sorted(r["id"] for r, _ in sg.rules_for_commands([cmd]))
        self.assertEqual(ids("npx next build"), ["nextjs"])
        self.assertEqual(ids("next dev --turbopack"), ["nextjs"])
        self.assertEqual(ids("npm run build"), [])


class BackendStackRules(StackRuleCase):
    # an ASP.NET Core project, its controllers and minimal APIs need the backend skill and the .NET team's Web API
    # skill; a domain record, a test that holds route text, and a desktop app's project file do not
    def test_dotnet_rules_fire_on_web_projects_and_endpoints(self):
        self.assert_stack("dotnet")

    # a C# class is a web controller by what it derives from, not by its name: a Unity script and a .NET iOS view
    # controller need neither rule; an MVC controller and an API controller with a primary constructor need both
    def test_csharp_controllers_match_by_their_base_class_not_their_name(self):
        self.assert_stack("csharp")

    # a Rust service's manifest, with its crates as keys or as tables ([dependencies.axum]), and its axum or actix
    # routes need the backend skill; a CLI's manifest does not
    def test_rust_rules_fire_on_service_code(self):
        self.assert_stack("rust")

    # a Rails or Sinatra app's Gemfile, routes and controllers need the backend skill, its migrations the database
    # rule; a gem's Gemfile, a plain class and a spec do not
    def test_ruby_rules_fire_on_service_code(self):
        self.assert_stack("ruby")

    # the .NET, Rust and Ruby route patterns are read in every backend language's files: they match as those languages
    # write them, so a TypeScript generic, a JavaScript map call or a Node subpath import (#app) matches none
    def test_other_languages_do_not_match_the_new_stacks_patterns(self):
        self.assert_stack("node")

    # spec/ is where RSpec keeps its specs and helpers, but other projects keep their API contract or examples there:
    # only Ruby files under spec/ are tests
    def test_a_spec_folder_holds_tests_only_in_ruby(self):
        self.assert_stack("contracts")

    # a Go service's module and its Go 1.22 method routes need the backend skill; a CLI's module does not, even when
    # a library it uses brings gRPC or a database driver in as an indirect requirement
    def test_go_rules_fire_on_service_code(self):
        self.assert_stack("go")

    def test_backend_stacks_require_their_skills(self):
        self.assertEqual(self.skills_for("dotnet", "Api/Program.cs"), ["backend", "dotnet-webapi"])
        self.assertEqual(self.skills_for("dotnet", "Api/Api.csproj"), ["backend"])
        for stack, rel in (("rust", "Cargo.toml"), ("ruby", "Gemfile"), ("go", "go.mod"), ("go", "cmd/api/main.go")):
            with self.subTest(stack=stack, file=rel):
                self.assertEqual(self.skills_for(stack, rel), ["backend"])

    # a Rails or EF Core migration of a project that uses Postgres (the pg gem, Npgsql) needs the Postgres skill; the
    # same migrations of the ruby and dotnet fixtures, which use no Postgres, stay with the database rule
    def test_postgres_rule_knows_rails_and_ef_core_migrations(self):
        self.assert_stack("rails_pg")
        self.assert_stack("efcore_pg")
        self.assert_stack("ruby")
        self.assert_stack("dotnet")

    # applying migrations is database work in every stack; goose takes its options before the driver, and its fix,
    # validate and version commands work on the migrations or the database too
    def test_migration_commands_need_the_database_rule(self):
        for cmd in ("dotnet ef migrations add Init", "bin/rails db:migrate", "bundle exec rake db:migrate", "sqlx migrate run",
                    "diesel migration run", "goose up", "GOOSE_DRIVER=postgres goose status", "goose create add_orders sql",
                    "goose postgres \"user=app dbname=orders\" up", "goose -dir db/migrations sqlite3 ./app.db down",
                    "goose -table schema_versions postgres \"dbname=orders\" up", "goose -allow-missing up",
                    "goose -env .env.test status", "goose -s create add_orders sql", "goose fix", "goose validate",
                    "goose version", "goose redshift \"postgres://u@h:5439/db\" status", "goose tidb \"u@/db\" up",
                    "goose vertica \"vertica://u@h:5433/db\" up", "goose ydb \"grpcs://h:2135/local\" up",
                    "goose turso \"libsql://db.turso.io\" up", "goose spanner \"projects/p/instances/i/databases/d\" up",
                    "goose azuresql \"sqlserver://h?database=db\" up", "goose starrocks \"u@/db\" up"):
            with self.subTest(cmd=cmd):
                self.assertIn("database", [r["id"] for r, _ in sg.rules_for_commands([cmd])])

    # an option written with its value after '=' is the same option as with its value after a space: goose
    # -dir=db/migrations runs goose -dir, and the value's path does not become a command word
    def test_an_option_with_its_value_after_an_equals_sign_counts_as_the_option(self):
        for cmd in ("goose -dir=db/migrations up", "goose -dir=migrations sqlite3 ./app.db status",
                    "goose -table=schema_versions postgres \"dbname=orders\" up"):
            with self.subTest(cmd=cmd):
                self.assertIn("database", [r["id"] for r, _ in sg.rules_for_commands([cmd])])

    # goose is also the name of Block's coding agent: its commands are no database work
    def test_the_goose_agents_commands_need_no_rule(self):
        for cmd in ("goose session", "goose run -t \"fix the tests\"", "goose configure", "goose info -v"):
            with self.subTest(cmd=cmd):
                self.assertEqual([r["id"] for r, _ in sg.rules_for_commands([cmd])], [])


class MobileRules(StackRuleCase):
    # app code that imports an Apple UI framework, and the app's Info.plist and privacy manifest, need the mobile
    # skill; a test target stays with the testing rule and server-side Swift with none
    def test_ios_rules_fire_on_app_code_only(self):
        self.assert_stack("ios")

    # Android manifests, app code and the app module's build file, in Kotlin or Groovy, need the mobile skill, the
    # manifest the permissions skill too; a JVM server's or library's build file and its Spring code do not
    def test_android_rules_fire_on_app_code_only(self):
        self.assert_stack("android")

    def test_platforms_require_their_skills(self):
        self.assertEqual(self.skills_for("ios", "App/ContentView.swift"), ["mobile", "swiftui-pro"])
        self.assertEqual(self.skills_for("ios", "App/LegacyViewController.swift"), ["mobile"])
        self.assertEqual(self.skills_for("android", "app/src/main/AndroidManifest.xml"),
                         ["android-permissions-security", "mobile"])

    # a step declared as mobile work gets the mobile skill whatever files it touches
    def test_mobile_work_needs_the_mobile_skill(self):
        need = {s for r in sg.rules_for_activity("mobile") for s in sg.required(r, "codex")}
        self.assertIn("mobile", need)


# skillguard reads up to 400 KB of each file a session changes and searches it with the content pattern of every rule
# that reads that file, at each edit. Each text below is a shape that makes one rule's pattern backtrack: a run that a
# quantifier crosses without finding the text that ends the match, alone, or repeated so that every start crosses the
# rest of the file (many unclosed `import {`, many `class A<`, a run of blank lines under a `^\s*`).
READ = 400_000
BACKTRACKING = {
    "ui": ["styled.a<" * (READ // 9), "@Component(" + " " * READ],
    "nextjs": ["from 'next" + "/a" * (READ // 2)],
    "vue": ["import { " + "useFetch, " * (READ // 10), "import { a" + ", useFetch" * (READ // 10) + " } from 'x'",
            "import { a\n" * (READ // 11), "import { useFetch\n" * (READ // 18)],
    "svelte": ["from 'svelte" + "/a" * (READ // 2)],
    "spring": ["org." * (READ // 4)],
    "e2e": ["'@playwright/" * (READ // 13)],
    "mobile": ["import" + "\n" * READ, "import " * (READ // 7)],
    "swiftui": ["import" + " " * READ],
    "api": ["class A<" * (READ // 8), "class A(" * (READ // 8), "class A<T>(" * (READ // 11), "\n" * READ,
            "\n " * (READ // 2), "\n" * (READ // 2) + "get '/" + "x" * (READ // 2)],
    "aspnetcore": ["class A<" * (READ // 8), "class A(" * (READ // 8)],
    "dotnet": ["Microsoft." * (READ // 10)],
    "rust": ["\n" * READ, "\n" * (READ // 2) + "[" + "x" * (READ // 2), "[" + "dependencies." * (READ // 13)],
    "ruby": ["\n" * READ, "add_dependency" + " " * READ, "add_dependency" + " " * (READ // 2) + "(" + " " * (READ // 2)],
    "go": ["github.com/lib/pq" + " a" * (READ // 2), "github.com/lib/pq v1" + " " * READ + "x", "\n\t" * (READ // 2)],
    "postgres": ['"pg"' + " " * READ],
}
# a child process searches one text with one pattern as skillguard does and prints the process time the search took;
# the limit on the child ends a pattern that backtracks for minutes, which Python cannot stop inside one search
SEARCH = ("import re, sys, time\nfrom pathlib import Path\nrx = re.compile(sys.argv[1], re.I)\n"
          "text = Path(sys.argv[2]).read_text(errors='ignore')[:400_000]\n"
          "start = time.process_time()\nrx.search(text)\nprint(time.process_time() - start)")


class ContentPatternTime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rules = json.loads((Path(__file__).resolve().parents[1] / "config" / "skill-rules.json").read_text())["rules"]
        found = {r["id"]: r.get("content") or (r.get("project") or {}).get("content") for r in rules}
        self.patterns = {rule: pattern for rule, pattern in found.items() if pattern}

    def tearDown(self):
        self.tmp.cleanup()

    def seconds(self, pattern: str, text: str) -> float:
        path = Path(self.tmp.name) / "text"
        path.write_text(text)
        try:
            out = subprocess.run([sys.executable, "-I", "-S", "-c", SEARCH, pattern, str(path)], capture_output=True,
                                 text=True, timeout=5, check=True)
        except subprocess.TimeoutExpired:
            return float("inf")
        return float(out.stdout)

    # the vue rule's Nuxt check: one import list that never closes, or many of them, is searched in under half a
    # second, and a Nuxt import from #app or #imports still matches while an import of another name or module does not
    def test_the_nuxt_import_check_searches_400_kb_in_under_half_a_second(self):
        rx = re.compile(self.patterns["vue"], re.I)
        for text in ("import { useFetch } from '#app'", "import type { useNuxtApp } from '#imports'",
                     "import {\n  ref,\n  useLazyAsyncData as load,\n} from \"#imports\""):
            with self.subTest(text=text):
                self.assertTrue(rx.search(text))
        for text in ("import { start } from '#app'", "import { useFetch } from 'ofetch'",
                     "import { useFetchy } from '#app'"):
            with self.subTest(text=text):
                self.assertIsNone(rx.search(text))
        for i, text in enumerate(BACKTRACKING["vue"]):
            with self.subTest(shape=i):
                self.assertLess(self.seconds(self.patterns["vue"], text), 0.5)

    # every content pattern devkit ships, on the 400 KB shapes that make it backtrack; a new pattern names its shapes
    # in BACKTRACKING
    def test_every_content_pattern_searches_400_kb_in_under_half_a_second(self):
        self.assertEqual(sorted(BACKTRACKING), sorted(self.patterns))
        for rule, texts in BACKTRACKING.items():
            for i, text in enumerate(texts):
                with self.subTest(rule=rule, shape=i):
                    self.assertLess(self.seconds(self.patterns[rule], text), 0.5)


if __name__ == "__main__":
    unittest.main()
