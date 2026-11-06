# Diagram spec reference

Every spec is JSON with these common keys:

| Key | Required | Meaning |
| --- | --- | --- |
| `kind` | yes | `flow` · `c4` · `state` · `sequence` · `er` · `timeline` |
| `name` | yes | file stem: `<name>-dark.svg`, `<name>-light.svg` |
| `title` | yes | the kicker at the top left (also the SVG `<title>`) |
| `desc` | yes | one or two sentences describing what the diagram shows (SVG `<desc>`, reuse as `alt`) |
| `legend` | no | `[{"label": "…", "family": "slate"}]` or `[{"label": "…", "style": "dashed"}]`, top right |
| `caption` | no | a sentence under the diagram (prefer putting it in the surrounding text) |

Families: `neutral` (default) · `firefly` · `steel` · `slate` · `olive` · `note`. Edge styles: `data` (default) ·
`api` · `dashed` · `async` · `optional` · `return` · `event` · `primary` (alias `thick`).

## flow (architecture, pipelines)

Nodes: `id`, `label`, `sub`, `col`, `row` (fractions allowed: `0.5` centres between two), `span` (`[cols, rows]`),
`shape` (`box` · `pill` · `store` · `actor` · `note` · `decision`), `family`, `emphasis` (the one focal node),
`width` (fixed; leave it out in most cases), `dashed`.
Groups: `id`, `title`, `nodes` (member ids), `dashed` (a boundary). Edges may start or end on a group id.
Edges: `from`, `to`, `label`, `style`, `both` (arrowheads at both ends), `from_side`/`to_side`
(`left` · `right` · `top` · `bottom`), `route` (`hv` · `vh` · `straight`), `via` (`[[col, row], …]` waypoints),
`label_at` (`source` · `target`), `arrow: "none"`.
Spec options: `grid` (`{"col": px, "row": px}` fixes the cell size; omit for the auto grid), `equal_widths`
(`true` per column, default · `"all"` · `false`).

```json
{"kind": "flow", "name": "checkout", "title": "Checkout", "desc": "The web app calls the API, which stores orders.",
 "nodes": [{"id": "web", "label": "Web app", "col": 0, "row": 0},
           {"id": "api", "label": "Orders API", "sub": "Spring Boot", "col": 1, "row": 0, "emphasis": true},
           {"id": "db", "label": "Orders", "shape": "store", "family": "slate", "col": 2, "row": 0}],
 "edges": [{"from": "web", "to": "api", "label": "POST /orders"}, {"from": "api", "to": "db", "label": "insert"}]}
```

## c4

Like `flow`; nodes take `type` (`person` · `system` · `container` · `component` · `external` · `database` · `queue`)
and optional `tech`, which set shape, family and a `[Container: tech]` line. Use a dashed group as the system boundary.

```json
{"kind": "c4", "name": "shop-containers", "title": "Shop · containers", "desc": "…",
 "nodes": [{"id": "user", "type": "person", "label": "Customer", "col": 0, "row": 0},
           {"id": "web", "type": "container", "label": "Web app", "tech": "Angular", "col": 1, "row": 0},
           {"id": "api", "type": "container", "label": "API", "tech": "Spring Boot", "col": 2, "row": 0},
           {"id": "pay", "type": "external", "label": "Payments", "col": 3, "row": 0}],
 "groups": [{"id": "shop", "title": "Shop", "nodes": ["web", "api"], "dashed": true}],
 "edges": [{"from": "user", "to": "web", "label": "uses"}, {"from": "web", "to": "api", "label": "JSON/HTTPS"},
           {"from": "api", "to": "pay", "label": "charges"}]}
```

## state

Like `flow` with pill-shaped states; `initial: true` / `final: true` nodes draw the start dot and the end bullseye.
Label every transition with its event or condition.

```json
{"kind": "state", "name": "order-states", "title": "Order", "desc": "…",
 "nodes": [{"id": "s", "initial": true, "col": 0, "row": 0}, {"id": "new", "label": "New", "col": 1, "row": 0},
           {"id": "paid", "label": "Paid", "col": 2, "row": 0}, {"id": "e", "final": true, "col": 3, "row": 0}],
 "edges": [{"from": "s", "to": "new"}, {"from": "new", "to": "paid", "label": "payment captured"},
           {"from": "paid", "to": "e", "label": "shipped"}]}
```

## sequence

`participants`: `id`, `label`, `sub`, `family`. `messages`, in order:
`{"from", "to", "label", "style"}` (`data` call · `async` · `return` response · `primary` the key message),
self-calls (`from` = `to`), `{"note": "…", "over": ["a", "b"]}`, and `{"divider": "Phase"}` to name phases.

```json
{"kind": "sequence", "name": "login", "title": "Login", "desc": "…",
 "participants": [{"id": "u", "label": "User"}, {"id": "app", "label": "App"}, {"id": "idp", "label": "Identity", "family": "steel"}],
 "messages": [{"from": "u", "to": "app", "label": "sign in"}, {"from": "app", "to": "idp", "label": "authorize (PKCE)"},
              {"from": "idp", "to": "app", "label": "code", "style": "return"}, {"from": "app", "to": "u", "label": "session", "style": "return"}]}
```

## er

`entities`: `id`, `name`, `col`, `row`, `fields` (`{"name", "type", "pk": true, "fk": true}`), `family`.
`relations`: `from`, `to`, `from_card`, `to_card` (`1` · `0..1` · `1..*` · `0..*`), `label`.

```json
{"kind": "er", "name": "orders-model", "title": "Orders model", "desc": "…",
 "entities": [{"id": "c", "name": "customer", "col": 0, "row": 0, "fields": [{"name": "id", "type": "uuid", "pk": true}]},
              {"id": "o", "name": "order", "col": 1, "row": 0,
               "fields": [{"name": "id", "type": "uuid", "pk": true}, {"name": "customer_id", "type": "uuid", "fk": true}]}],
 "relations": [{"from": "c", "to": "o", "from_card": "1", "to_card": "0..*", "label": "places"}]}
```

## timeline

`items`: `date`, `label`, `sub`, `emphasis` (the one milestone that matters). Cards alternate on either side of the axis.

```json
{"kind": "timeline", "name": "roadmap", "title": "Roadmap", "desc": "…",
 "items": [{"date": "2026-11", "label": "Pilot"}, {"date": "2026-12", "label": "General availability", "emphasis": true},
           {"date": "2027-02", "label": "Second region"}]}
```
