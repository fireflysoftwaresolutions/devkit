---
name: backend
description: Backend engineering checklists for Java/Spring, Python, PHP, Node, .NET, Go, Rust and Ruby services—API contract design, domain layering, database migrations, testing strategy with Testcontainers, resilience, security and operability. Use when designing or changing an API, a database schema, a service's domain logic, integration with external systems, or when reviewing backend code.
---

# Backend methodology

Apply what fits the change; follow the repository's existing conventions where they exist.

## API changes (contract first)

- Update the contract first (OpenAPI / GraphQL schema / AsyncAPI or message schema), then the code; lint it
  (`redocly lint` or the project's tool).
- Backward compatible by default: add fields, never repurpose; removing or renaming = new version or a deprecation
  period. Consumers of messages must tolerate unknown fields.
- Resource naming and HTTP semantics: nouns, correct verbs and status codes, idempotency keys for unsafe retries,
  pagination (cursor for large or live collections), filtering, and sorting explicit.
- Errors: one shape (RFC 9457 problem details unless the project has its own); never leak stack traces or SQL.

## Domain and layering

- Business rules live in the domain/service layer, free of HTTP, ORM, and framework types.
- Controllers/handlers: parse, validate, call one use case, map the result. Persistence behind repositories.
- DTOs at the edges; never expose ORM entities over the API.
- Transactions at the use-case boundary; no remote calls inside a database transaction.

## Database

- Every schema change is a migration (Flyway/Liquibase, Alembic, Laravel/Doctrine migrations, EF Core migrations,
  Rails migrations, sqlx or Diesel, goose or golang-migrate), reviewed like code.
- Never edit a migration that has run anywhere shared; add a new one.
- Zero-downtime: expand (add nullable column / new table) → deploy code that writes both → backfill → switch reads →
  contract (drop old) in a later release.
- Indexes for new query patterns; check plans (`EXPLAIN`) for queries on large tables; avoid N+1 (fetch joins,
  batch loading).
- Large data migrations run in batches, are resumable and are separate from schema migrations.

## Testing

| Layer | What | Tools |
| --- | --- | --- |
| Unit | domain rules, edge cases | JUnit 5 + AssertJ · pytest · PHPUnit/Pest · Vitest/Jest · xUnit/NUnit/MSTest · `go test` · `cargo test` · RSpec/Minitest |
| Integration | repositories, migrations, messaging against real services | Testcontainers (Postgres, MySQL, Kafka, Redis…), with libraries for Java, Python, Node, PHP, .NET, Go, Rust, and Ruby |
| API / contract | request/response shape and status codes | Spring MockMvc/WebTestClient · FastAPI TestClient · Laravel HTTP tests · supertest · `WebApplicationFactory` · `httptest` · axum `oneshot` · Rails request specs |
| End-to-end | a few critical journeys | Playwright |

- Bug fix = failing test first.
- Deterministic: inject clocks and random sources, no sleeps (await conditions), no shared mutable fixtures, no
  test-order dependence.
- Do not mock what you do not own; wrap third-party clients and fake the wrapper, or use Testcontainers / WireMock.
- Docker for Testcontainers: `dev docker up` (Colima; `DOCKER_HOST` and the socket override are preset).

## Resilience

- Timeouts on every outbound call (HTTP, DB, queues). Bounded retries with backoff and jitter, only for idempotent
  operations. Circuit breakers where a dependency can fail slowly.
- Idempotent consumers (dedupe keys) and the outbox pattern when a DB write and an event must both happen.
- Graceful shutdown; health (liveness) and readiness endpoints that reflect real dependencies.

## Security

- Validate and normalise input at the boundary (bean validation / pydantic / form requests / zod).
- Parameterised queries only; no string-built SQL. Escape output for its context.
- AuthN at the edge, AuthZ per operation and per resource (check ownership, not just roles).
- Secrets from environment or the secret manager; never in code, logs, tests, or the handoff note.
- Dependencies: pinned via lockfile; check advisories (`npm audit`, `pip-audit`, `composer audit`,
  OWASP dependency-check / `mvn versions`, NuGet's audit on restore and `dotnet list package --vulnerable
  --include-transitive`, `govulncheck ./...`, `cargo audit`, `bundle-audit check --update`) when you add or bump one.

## Operability

- Structured logs (JSON in production) with a correlation/trace id; no personal data or secrets; log at boundaries
  and on decisions, not in loops.
- Metrics for rate, errors and duration of each endpoint/consumer; traces across service calls (OpenTelemetry where
  the project uses it).
- Configuration through environment variables with validated defaults (12-factor); feature flags for risky changes.

## Stack notes

- **Spring Boot:** constructor injection, `@ConfigurationProperties` over `@Value`, `@Transactional` on services,
  slices (`@WebMvcTest`, `@DataJpaTest`) plus `@SpringBootTest` with Testcontainers (`@ServiceConnection`).
- **Python (FastAPI/Django):** `uv`, pydantic models at the edges, async only end to end, Alembic/Django migrations,
  `ruff` + `pyright`/`mypy`, pytest fixtures with Testcontainers.
- **PHP (Laravel/Symfony):** form requests / validators, policies/voters for authorisation, queued jobs idempotent,
  migrations per release, PHPStan at the project's level.
- **Node/TypeScript (NestJS/Express):** strict TS, zod/class-validator at the edge, no unhandled promises, typed
  config module, Prisma/TypeORM migrations committed.
- **.NET (ASP.NET Core):** load `dotnet-webapi` for endpoints (the `aspnetcore` rule requires it); errors as Problem
  Details (`AddProblemDetails`, `IExceptionHandler`); a `CancellationToken` on every endpoint, forwarded to every
  async call; paginate list endpoints, which `dotnet-webapi` leaves out. Outbound calls through `IHttpClientFactory`
  with a timeout chosen per dependency (`HttpClient` waits 100 seconds by default). EF Core: `AsNoTracking` for
  reads, `dotnet ef migrations has-pending-model-changes` in CI, and migrations applied by a bundle or a reviewed
  idempotent script (`dotnet ef migrations script --idempotent`), not by every replica at startup. Tests:
  `WebApplicationFactory<Program>` (`Microsoft.AspNetCore.Mvc.Testing`) with Testcontainers for .NET. NuGet audits
  on restore; treat `NU1903` and `NU1904` (high, critical) as errors in CI.
- **Go:** `context.Context` first, passed through and honored for cancellation; wrap errors with `%w` and test them
  with `errors.Is`/`errors.As`; no `panic` across package boundaries. `http.Client` needs a `Timeout` (zero means
  none), and `http.Server` needs `ReadHeaderTimeout` and the other timeouts set; routes with Go 1.22 patterns
  (`"GET /orders/{id}"`, `r.PathValue`) or the project's router. Tests: table-driven, `httptest` for handlers,
  `go test -race ./...`, Testcontainers for Go; `go vet` and `govulncheck ./...` in CI.
- **Rust (axum, actix-web, tonic):** errors as typed enums mapped to Problem Details in one `IntoResponse` (axum) or
  `ResponseError` (actix) implementation; no `unwrap()` or `expect()` on input or I/O paths; no blocking calls on the
  async runtime (`spawn_blocking`). `reqwest` has no request or connect timeout by default: set both. axum 0.8 routes
  use `{param}`, not `:param`. Migrations with sqlx or Diesel, committed. Tests: `cargo test`, handlers through
  `tower::ServiceExt::oneshot`, Testcontainers for Rust; `cargo fmt --check`, `cargo clippy -- -D warnings` and
  `cargo audit` in CI.
- **Ruby (Rails, Sinatra):** strong parameters with `params.expect(...)` (Rails 8); authorization per action
  (Pundit or the project's policy objects); avoid N+1 with `includes` and catch it with `strict_loading`; Active Job
  and Sidekiq jobs idempotent; explicit timeouts on outbound HTTP. Migrations reversible, one concern each, data
  backfills apart from schema changes. Tests: RSpec request specs or Minitest integration tests, Testcontainers for
  Ruby; Brakeman, RuboCop and `bundle-audit check --update` in CI (new Rails apps ship the first two).
