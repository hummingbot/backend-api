# Features

Design backlog produced by the `/design-feature` skill (design only, read-only on code).
Each `.md` is **one atomic feature**: an objective, the alternatives that were weighed,
the decision, the design, and an implementation plan a developer can execute without
re-deciding the architecture.

## Convention

- `todo/` — designed, pending implementation. `done/` — implemented, with commits annotated.
- File name: `FEAT-{NNN}-{short-slug}.md`.
  - `NNN`: 3-digit counter, **unique across `todo/` + `done/`**, never reused.
- The frontmatter is the card; the body is the spec. Do not edit `status` by hand —
  `/ship-feature` moves the file and fills in `commits`.

## Flow

1. `/design-feature <what to build>` — investigates, decides, drops a doc in `todo/` (touches no code).
2. `/ship-feature <id>` — implements one doc, commits, moves it to `done/` annotating the commits.

Sibling backlog: `improvements/` (code audit findings) via `/improvements` and `/ship-improvement`.
