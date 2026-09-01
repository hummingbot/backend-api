---
id: FEAT-001
title: Executor performance is snapshotted over time, and one route serves it alongside controllers
status: done
effort: L
risk: medium
new_files:
  - database/repositories/executor_performance_repository.py
  - models/performance.py
  - routers/performance.py
  - test/test_executor_performance_snapshots.py
touched_files:
  - database/models.py:443
  - database/models.py:456
  - database/__init__.py
  - database/repositories/__init__.py
  - services/executor_service.py:313
  - services/executor_service.py:249
  - services/executor_service.py:1385
  - database/repositories/executor_repository.py:556
  - config.py:15
  - main.py:244
  - main.py:468
  - test/test_executor_volume_is_the_filled_amount.py
depends_on: []
commits:
  - "27b1e42 feat(performance): snapshot a live executor's performance over time"
  - "1c09a4b feat(performance): serve both performance series from one route"
created: 2026-09-01
---

## Objective

An executor's performance is recorded **over time**, not only at the two instants it is
recorded today, and a single route serves that series under the same shape as the
controller series — so any consumer (the MCP server, a dashboard, Condor) writes one
client against one contract instead of two, and a **running** executor can draw its own
curve.

Observable outcomes:

- A live executor accumulates a row per snapshot interval; the row carries its PnL, fees
  and filled amount at that moment.
- A finished executor's series ends on a **terminal row** written at completion, so
  "the series of a closed executor" is a single-table query with no join and no
  special-casing.
- `GET /performance/history?subject=executor|controller` returns both populations under
  one normalized row shape, paged, cursored and interval-sampled exactly like the
  existing controller route.
- **An API restart stops destroying the accounting of every executor that was live.**
  Today it does — see premise 2 under "Context and constraints".

This is the upstream half of a downstream request (Condor's `FEAT-087`). That proposal is
reviewed critically here — several of its premises turned out not to hold against this
codebase, and the design departs from it where they don't.

## Context and constraints

### What exists today

**Controllers are snapshotted, from MQTT.** `ControllerPerformanceSnapshot`
(`database/models.py:443`) stores `timestamp, bot_name, controller_id, status,
performance (JSON Text), custom_info (JSON Text)`. `BotsOrchestrator._performance_dump_loop`
(`services/bots_orchestrator.py:378`) calls `dump_controller_performance`
(`:388`) every `performance_dump_interval` minutes (default 5, `config.py:15`,
`BROKER_PERFORMANCE_DUMP_INTERVAL`), folding the MQTT status reports through
`determine_controller_performance` (`:250`). It is read through
`ControllerPerformanceRepository.get_performance_history`, which does cursor pagination
plus interval sampling, and exposed at `GET /bot-orchestration/controller-performance-latest`
(`routers/bot_orchestration.py:66`) and `GET /bot-orchestration/controller-performance-history`
(`:83`). **Note the real paths are hyphenated**, not the `/controller-performance/history`
sub-path the downstream proposal assumed.

The `performance` blob is opaque to this API because it is defined by the core:
it is `hummingbot.strategy_v2.models.executors_info.PerformanceReport` —
`realized_pnl_quote, unrealized_pnl_quote, realized_pnl_pct, unrealized_pnl_pct,
global_pnl_quote, global_pnl_pct, volume_traded, open_order_volume,
inventory_imbalance, positions_summary, close_type_counts`. **There is no fees field.**

**Executors are not snapshotted, and they are a different population entirely.**
`ExecutorRecord` (`database/models.py:456`) is one mutable row per executor. It is written
by exactly **two** call sites, both in `ExecutorService`:

- `_persist_executor_created` (`services/executor_service.py:1358`) — INSERT at creation,
  with `net_pnl_quote/net_pnl_pct/cum_fees_quote/filled_amount_quote` at their column
  defaults of `0`.
- `_persist_executor_completed` (`:1385`) — UPDATE at completion, with the real figures
  read off `executor.executor_info`.

Nothing writes between those two. **A running executor's database row says its PnL is
zero for its entire life.** The live figures exist only in memory, which is why
`get_executors` (`:718`) and `get_performance_report` (`:1231`) merge
`self._active_executors` on top of the database on every read.

**These executors have nothing to do with MQTT or Docker bots.** They are created and
driven in-process by `ExecutorService` (`main.py:244`), ticked by `_control_loop`
(`services/executor_service.py:313`) at 1 Hz. `BotsOrchestrator` never sees them, and
`ExecutorRecord` never contains a Docker bot's executors.

### Three premises of the downstream proposal that do not hold

1. **"Executor state has the same origin — `bots_orchestrator` receives it over MQTT."**
   False. A dump loop hung off `BotsOrchestrator` would have nothing to write. The only
   place a live executor's performance exists is `ExecutorService._active_executors`, read
   as `executor.executor_info`. That is *better* than the proposal assumed: the data is
   already in hand at 1 Hz, exactly typed, no new polling and no MQTT round-trip.

2. **"A closed executor's terminal `ExecutorRecord` row is its last snapshot."** Only for a
   clean completion. It is wrong on two paths:
   - **The reap path.** `cleanup_orphaned_executors`
     (`database/repositories/executor_repository.py:556`), run at every startup from
     `services/executor_service.py:249`, flips every `status == "RUNNING"` row to
     `TERMINATED`/`SYSTEM_CLEANUP` and sets `closed_at` — and **touches none of the PnL
     columns**, leaving the creation-time zeros. Every executor that was live when the API
     went down is permanently booked at 0 PnL, 0 fees, 0 volume, and
     `/executors/performance` sums those zeros forever.
   - **The exception path.** `_persist_executor_completed` reads `executor_info` inside a
     `try/except` that logs at DEBUG and substitutes `Decimal("0")` — the same silent-zeros
     failure `utils/core_compatibility.py` was written to guard against.

   So the terminal row is not a reliable series endpoint, and building the read path on
   "join the snapshot table to `ExecutorRecord` for the last point" inherits both holes.

3. **"`filled_amount_quote` is deliberately not volume for an LP executor."** Out of date.
   That split existed and **was removed**: `lp_executor.filled_amount_quote` now derives
   the volume that crossed the position from the fees it earned, so one field means the
   same thing on every executor type. `test/test_executor_volume_is_the_filled_amount.py`
   pins this, and `utils/core_compatibility.py` documents `volume_traded_quote` as the
   field that went away. The comment quoted from `database/models.py:485` is the orphaned
   explanation of the **deleted** column, sitting under `filled_amount_quote`. Any design
   that re-introduces a second volume column breaks a pinned test on purpose.

### Constraints

- **Wire compatibility is absolute.** `/bot-orchestration/controller-performance-latest`
  and `-history` are consumed externally. New surface only; those two responses do not
  change by one byte, and `ControllerPerformanceRepository` is not modified.
- **Schema creation is `create_all` + a lightweight ALTER list** (`database/connection.py:42`
  and `:59`). `Base.metadata.create_all` creates *missing tables* by itself; the migration
  list exists only for **columns added to existing tables**. A brand-new table therefore
  needs **no** migration entry. Nothing in this feature adds a column to an existing table.
- **Interval sampling in `ControllerPerformanceRepository` hard-codes a 5-minute grain**
  in two places (`if interval_minutes <= 5: return history`, and
  `sampling_multiplier = interval_minutes // 5`). A finer executor grain cannot reuse it
  unchanged, and that repository must not be touched.
- **`custom_info` is heavy on purpose.** `_strip_heavy_fields`
  (`services/executor_service.py:1138`) and the grid-field stripping in
  `_persist_executor_completed` both exist because these payloads carry `fill_events`,
  `levels_by_state`, `filled_orders`… A per-minute row is the last place to put them.

**Out of scope:** backfilling history for executors that already closed; snapshotting a
Docker bot's individual executors (they are not in this API's memory or database);
changing `/executors/performance`, `/executors/summary` or `/executors/`; downsampling old
rows to a coarser grain; anything client-side.

## Alternatives considered

- **A — Mirror the controller table literally** (JSON `performance`/`custom_info` Text
  columns, loop on `BotsOrchestrator`, "snapshot every executor"). Maximum symmetry, and
  what the downstream proposal transcribed. Fails three ways: the loop's owner has no
  executor data; a JSON blob re-imports exactly the payload weight two existing code paths
  strip out; and "every executor" is not implementable — only live ones exist anywhere to
  be sampled. The controller table blobs because the payload is an *opaque, core-versioned*
  report; the executor payload is `ExecutorInfo`, whose fields this repo already names in
  four places. Symmetry of storage is not the goal; symmetry of the **read contract** is.

- **B — The downstream proposal: snapshot only live executors; the terminal `ExecutorRecord`
  row stands for the closed one.** Right instinct, wrong endpoint. "Only live" is not a
  filter that needs designing — it is the only thing that *can* be sampled, since
  `_active_executors` is by construction exactly the live set (`_handle_executor_completion`
  removes from it). But making `ExecutorRecord` the last point forces every reader to join
  two tables of different shapes, and inherits both holes in premise 2 above: the reaped
  executor and the silently-zeroed one both end their series on a fabricated 0.

- **C (chosen) — B, with the terminal row written into the snapshot table itself.**
  See Decision.

- **D — Event-sourced: append a row on every executor state change.** The most faithful
  record and free of sampling artefacts. Rejected: write volume becomes a function of
  market activity rather than of time, so it has no bound an operator can reason about; it
  cannot answer an `interval=` query without building its own downsampler; and the only
  thing it buys over C is sub-interval resolution on a single executor's life. Noted as the
  successor if that resolution is ever wanted.

- **E — No new table: periodically UPDATE the live `ExecutorRecord` row.** By far the
  cheapest, no schema change, and it fixes the reap bug outright. Rejected because it
  yields no series at all, which is the objective — and it is *subsumed* by C, which keeps
  every intermediate value instead of overwriting it. C then reuses the last snapshot at
  reap time to get E's benefit as well.

## Decision

**C: a narrow, typed `executor_performance_snapshots` table, written by `ExecutorService`
from `_control_loop` on its own interval, plus one terminal row written at completion; and
a new `GET /performance/history` that normalizes both tables into one row shape while the
two existing controller routes stay untouched.**

Why this is the obvious shape here, point by point:

- **`ExecutorService` owns the write** because it is the only object in the process that
  holds a live executor. The loop is not a new asyncio task: `_control_loop` already ticks
  at 1 Hz and already awaits a database call inside the tick
  (`_record_lp_position_rent`), so the snapshot is one `if now - last_dump >= interval`
  guard in a loop that exists. No new task, no new service, no scheduling to reason about,
  and no chance of interleaving with `_handle_executor_completion`.

- **Typed columns, not a JSON blob.** The controller table stores JSON because
  `PerformanceReport` is defined by the core and versioned outside this repo. `ExecutorInfo`
  is not: this API already reads its four metric fields by name in
  `_persist_executor_completed`, `_format_db_record`, `get_performance_report` and the
  repository aggregates. Typing them means the executor branch of the unified route needs
  **no parsing at all** (only the controller branch does), retention and future aggregates
  can be done in SQL, and none of the heavy `custom_info` fields can leak into a per-minute
  row. This is a deliberate asymmetry with the controller table, and it costs nothing
  because what has to be symmetric is the response, not the storage.

- **The terminal row goes in the snapshot table.** This is the one real improvement over
  the downstream proposal. `_persist_executor_completed` already computes the exact final
  figures; writing them as a snapshot row with `is_terminal=True` costs one extra INSERT
  per executor lifetime and makes `subject=executor` a **pure single-table query** —
  no join to `ExecutorRecord`, no "and then append the final value" rule that every future
  reader would have to re-learn, and no exposure to the reap and exception paths.

- **The reaper adopts the last snapshot.** Once the series exists, the fix for the restart
  bug is a few lines: `cleanup_orphaned_executors` copies the most recent snapshot's
  figures into the row it is terminating instead of leaving the creation-time zeros. This
  is what turns the feature from "a chart" into "the accounting stops being wrong", and it
  is the strongest reason to build it.

- **A separate, finer cadence knob.** `BROKER_PERFORMANCE_DUMP_INTERVAL` is minutes, lives
  under `BrokerSettings`, and is semantically about MQTT bots — executors touch neither.
  More importantly the right value differs: a position executor can live three minutes and
  would get one point at a 5-minute grain. Default the executor grain to **60 seconds**.

- **One route with `?subject=`, not two routes.** The whole point of the request is that a
  client writes `seriesFor(scope)` once. Two URLs returning the same shape would move the
  branch from a parameter to a path and buy nothing. The cost is that FastAPI cannot
  express "`executor_id` is only legal when `subject=executor`" — that is ~6 lines of
  explicit `400`.

**Accepted trade-offs.** An executor shorter-lived than the snapshot interval gets exactly
one point, its terminal row; that is honest, there was nothing to sample. A reaped
executor's series ends at its last snapshot rather than at its true final value, which is
strictly better than today's zero but still approximate — record it in `close_type`
(`SYSTEM_CLEANUP`) so a reader can tell. And the snapshot loop shares the control-loop tick,
so a slow database blocks executor updates for the duration of one batch INSERT; the loop
already accepts that shape for `_record_lp_position_rent`.

**Rejected sub-decisions, deliberately:**

- *Change-detection ("skip the write when nothing changed").* Requested downstream;
  rejected. A gap in the series is indistinguishable from the API having been down, which
  is a worse ambiguity than the rows it saves — and the rows are cheap: the table is ~10
  narrow columns, so 20 concurrent executors at a 60s grain is 28,800 rows/day (~5 MB/year
  including indexes) and 200 concurrent is 288,000/day (~50 MB/year). The lever that
  actually matters is the interval and retention, not deduplication.
- *Downsampling old rows to a coarser grain.* Requested downstream; rejected as YAGNI at
  those volumes. Delete-only retention is enough; downsampling is the successor if a
  deployment ever proves it needs one.
- *A `bot_name` column.* In-process executors have no bot. It would always be `NULL`.
- *A `custom_info` column.* If a specific field (an LP position's in-range flag, say) turns
  out to be wanted on the curve, add that one narrow typed column then.
- *Storing win rate or Sharpe.* They are folds over the rows. Two stored answers can
  disagree; a fold cannot.

## Design

### 1. Storage — `database/models.py`

Add after `ControllerPerformanceSnapshot` (`:443`). Requires adding `Boolean` and `Index`
to the imports on line 1 (this is the first composite index in the file; the hot query is
`WHERE executor_id = ? ORDER BY timestamp DESC`, which wants one).

```python
class ExecutorPerformanceSnapshot(Base):
    """Periodic snapshot of a live executor's performance, plus one terminal row.

    Written by ExecutorService: on the snapshot tick for everything in
    _active_executors, and once more at completion with is_terminal=True. The terminal
    row is what makes a closed executor's series answerable from this table alone --
    see FEAT-001. Deliberately narrow and typed: ExecutorInfo is defined in this
    repo's dependency surface, unlike the core-versioned controller PerformanceReport
    that controller_performance_snapshots has to blob.
    """
    __tablename__ = "executor_performance_snapshots"
    __table_args__ = (
        Index("ix_exec_perf_executor_timestamp", "executor_id", "timestamp"),
    )

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now(),
                       nullable=False, index=True)

    # Identity, denormalized from ExecutorRecord so a series needs no join.
    executor_id = Column(String, nullable=False, index=True)
    executor_type = Column(String, nullable=False, index=True)
    account_name = Column(String, nullable=False, index=True)
    connector_name = Column(String, nullable=False)
    trading_pair = Column(String, nullable=False)
    controller_id = Column(String, nullable=False, default="main", index=True)

    status = Column(String, nullable=False)          # RunnableStatus name
    close_type = Column(String, nullable=True)       # only ever set on the terminal row
    is_terminal = Column(Boolean, nullable=False, default=False, index=True)

    # The four ExecutorInfo metrics, same precision as ExecutorRecord. There is NO
    # separate volume column: filled_amount_quote IS the volume traded, on every
    # executor type including LP -- see test_executor_volume_is_the_filled_amount.py.
    net_pnl_quote = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    net_pnl_pct = Column(Numeric(precision=10, scale=6), nullable=False, default=0)
    cum_fees_quote = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
    filled_amount_quote = Column(Numeric(precision=30, scale=18), nullable=False, default=0)
```

Export it from `database/__init__.py`. No entry in `_run_migrations`: `create_all` makes a
missing table on its own.

### 2. Repository — `database/repositories/executor_performance_repository.py`

`ExecutorPerformanceRepository(session)`, modelled on `ControllerPerformanceRepository`
but with a **parameterized grain**:

```python
GRAIN_MINUTES: float                      # constructor arg, from settings; default 1.0
save_snapshots(rows: List[Dict]) -> int           # add_all + flush, like save_controller_performances
get_latest_for(executor_ids: List[str]) -> Dict[str, Dict]   # max(timestamp) per executor_id
get_performance_history(
    executor_id=None, executor_type=None, controller_id=None, account_name=None,
    connector_name=None, trading_pair=None,
    start_time=None, end_time=None, interval="1m",
    limit=None, cursor=None,
) -> Tuple[List[Dict], Optional[str], bool]
prune_older_than(cutoff: datetime) -> Tuple[int, int]   # (executor rows, controller rows)
```

`_interval_to_minutes` gains `"1m": 1` on top of the existing map. `_sample_by_interval`
and the fetch multiplier take the grain as a parameter instead of assuming 5:

```python
if interval_minutes <= grain_minutes: return history
sampling_multiplier = max(1, int(interval_minutes // grain_minutes))
```

Cursor semantics are copied verbatim from `ControllerPerformanceRepository` — descending
`timestamp`, cursor is the last returned ISO timestamp, `has_more` from an over-fetch of
one — so a client pages both subjects with identical code.

`prune_older_than` issues two `DELETE ... WHERE timestamp < :cutoff`, one per snapshot
table. It lives here, next to the writer that generates the growth, and covers both
because "performance snapshot retention" is one policy, not two.

### 3. Writing — `services/executor_service.py`

Constructor gains `performance_snapshot_interval: float = 60.0` and
`performance_retention_days: int = 0`, plus `self._last_snapshot_at: float = 0.0` and
`self._last_prune_at: float = 0.0` (both `time.monotonic()`).

In `_control_loop` (`:313`), after the completion handling and inside the existing
`try`, add:

```python
now = time.monotonic()
if now - self._last_snapshot_at >= self.performance_snapshot_interval:
    self._last_snapshot_at = now
    await self._dump_executor_performance()
    if self.performance_retention_days > 0 and now - self._last_prune_at >= 3600:
        self._last_prune_at = now
        await self._prune_performance_snapshots()
```

`_dump_executor_performance()` builds one row per entry in `_active_executors` from
`executor.executor_info` and `self._executor_metadata[executor_id]` — the same two sources
`_format_executor_info` (`:1090`) already reads — with `is_terminal=False`, and hands the
batch to `save_snapshots`. It wraps its own `try/except` and logs, mirroring
`dump_controller_performance`; a failed dump must never break the control loop.

`_persist_executor_completed` (`:1385`) writes the terminal row in the **same session** as
the `ExecutorRecord` update, so the record and its final snapshot commit together:
`is_terminal=True`, `status`/`close_type`/metrics taken from the values it already computed.

Ordering note: the snapshot runs after completion handling in the tick, so a just-closed
executor is already out of `_active_executors` and gets its terminal row, not a duplicate
periodic one.

### 4. Reap adoption — `database/repositories/executor_repository.py:556`

`cleanup_orphaned_executors` currently sets `status`, `close_type` and `closed_at` in a
single bulk `UPDATE`. Change it to first select the orphaned ids, look up each one's latest
snapshot via `ExecutorPerformanceRepository.get_latest_for`, and apply the metrics per row
(`net_pnl_quote`, `net_pnl_pct`, `cum_fees_quote`, `filled_amount_quote`) alongside the
status change. Executors with no snapshot (created and orphaned inside one interval) keep
today's behaviour. This is the only change to an existing write path.

### 5. Reading — `models/performance.py` and `routers/performance.py`

One normalized row, a **superset** of what the existing controller row carries, so a client
migrating from `/bot-orchestration/controller-performance-history` loses nothing:

```jsonc
{
  "timestamp": "2026-09-01T12:00:00+00:00",
  "subject": "executor",              // "controller" | "executor"
  "scope_id": "abc123",               // executor_id, or controller_id for controllers
  "status": "RUNNING",
  "is_terminal": false,

  "realized_pnl_quote": 0.0,
  "unrealized_pnl_quote": 12.5,
  "global_pnl_quote": 12.5,
  "global_pnl_pct": 0.0125,
  "volume_quote": 4200.0,
  "cum_fees_quote": 1.8,              // null for controllers -- PerformanceReport has no fees field

  "bot_name": null,                   // controllers only
  "controller_id": "main",            // both
  "executor_id": "abc123",            // executors only
  "executor_type": "position_executor",
  "account_name": "master_account",
  "connector_name": "binance_perpetual",
  "trading_pair": "BTC-USDT",
  "close_type": null,

  "performance": {},                  // controllers: the raw PerformanceReport dict
  "custom_info": {}                   // controllers: the raw custom_info dict
}
```

Mapping, both directions explicit:

| unified field | controller (`PerformanceReport`) | executor (`ExecutorInfo`) |
|---|---|---|
| `realized_pnl_quote` | `realized_pnl_quote` | `net_pnl_quote` when settled, else `0` |
| `unrealized_pnl_quote` | `unrealized_pnl_quote` | `net_pnl_quote` when not settled, else `0` |
| `global_pnl_quote` | `global_pnl_quote` | `net_pnl_quote` |
| `global_pnl_pct` | `global_pnl_pct` | `net_pnl_pct` |
| `volume_quote` | `volume_traded` | `filled_amount_quote` |
| `cum_fees_quote` | `null` (not in the report) | `cum_fees_quote` |
| `performance` / `custom_info` | raw stored JSON | `{}` |

"Settled" means `is_terminal and close_type != "POSITION_HOLD"` — the same exclusion
`ExecutorRepository.get_performance_report` (`:376`) already applies so a held position is
not double-counted against `position_holds`. Everything the controller report carries that
has no executor counterpart (`open_order_volume`, `inventory_imbalance`,
`positions_summary`, `close_type_counts`) stays reachable in the `performance`
passthrough — the normalization is additive, never lossy.

Route, in a new `routers/performance.py` (`prefix="/performance"`, tag `"Performance"`,
registered in `main.py` next to the other routers with `Depends(auth_user)`):

```
GET /performance/history
    ?subject=controller|executor        (required)
    &bot_name= &controller_id=                        # controller filters
    &executor_id= &executor_type= &account_name=      # executor filters
    &connector_name= &trading_pair=
    &start_time= &end_time=
    &interval=1m|5m|15m|30m|1h|4h|12h|1d   (default 5m)
    &limit<=1000 (default 100) &cursor=
```

Response envelope matches the existing controller route's — `{"status", "data",
"pagination": {"next_cursor", "has_more", "limit", "interval"}}` — so only the row shape is
new. It returns `400` when a filter is passed that does not belong to the requested
subject.

`subject=controller` calls **`BotsOrchestrator.get_controller_performance_history`
unchanged** (`services/bots_orchestrator.py:424`) and maps the result rows; the two routes
therefore share one query path by construction and cannot drift.
`subject=executor` calls a thin `ExecutorService.get_executor_performance_history` that
delegates to the new repository.

`interval` is a floor, not a guarantee — asking for `1m` on the controller subject returns
its native 5-minute grain. This is already true of the existing route and the echoed
`interval` in `pagination` tells the caller what they asked for; the timestamps tell them
what they got.

### 6. Configuration — `config.py`

A new group, modelled on `BacktestingSettings`:

```python
class PerformanceSettings(BaseSettings):
    """Performance snapshot cadence and retention."""

    executor_snapshot_interval: int = Field(
        default=60,
        description="How often a live executor's performance is snapshotted, in seconds. "
                    "Finer than the controller dump because executors are short-lived: at "
                    "a 5-minute grain a three-minute position executor gets one point."
    )
    retention_days: int = Field(
        default=0,
        description="Delete performance snapshots (executor AND controller) older than "
                    "this many days. 0 keeps everything forever, which is what every "
                    "existing deployment does today -- an upgrade must not start deleting "
                    "an operator's history."
    )

    model_config = SettingsConfigDict(env_prefix="PERFORMANCE_", extra="ignore")
```

Wired into `Settings` and passed to `ExecutorService` at `main.py:244`.
`BROKER_PERFORMANCE_DUMP_INTERVAL` is untouched.

## Implementation plan

Vertical slices; each is independently testable and commits coherently. Steps 1–5 are the
write side (useful on their own — they fix the restart bug), 6–8 the read side.

- [x] 1. `ExecutorPerformanceSnapshot` in `database/models.py` (+ `Boolean`, `Index`
      imports), exported from `database/__init__.py`. Confirm `create_tables` makes it with
      no migration entry: start against an existing database and check the table appears.
- [x] 2. `ExecutorPerformanceRepository` with `save_snapshots`, `get_latest_for` and the
      grain-parameterized `get_performance_history`; register in
      `database/repositories/__init__.py` and `database/__init__.py`.
- [x] 3. `PerformanceSettings` in `config.py`, wired through `Settings` and `main.py:244`
      into the `ExecutorService` constructor.
- [x] 4. `_dump_executor_performance` + the interval guard in `_control_loop`, and the
      terminal row in `_persist_executor_completed` (same session as the record update).
      Verify with a real short-lived executor that the series has intermediate points and
      ends on `is_terminal=True`.
- [x] 5. Reap adoption in `ExecutorRepository.cleanup_orphaned_executors`: the terminated
      row takes the latest snapshot's metrics instead of the creation-time zeros.
- [x] 6. `prune_older_than` + the hourly guard in the snapshot tick, gated on
      `retention_days > 0`.
- [x] 7. `models/performance.py` (the normalized row + the two mappers) and
      `routers/performance.py`; register in `main.py` with `Depends(auth_user)`.
- [x] 8. Tests (see below), including extending
      `test/test_executor_volume_is_the_filled_amount.py` to pin the new table.

## Acceptance criteria

- [x] An executor live for longer than `PERFORMANCE_EXECUTOR_SNAPSHOT_INTERVAL`
      accumulates one snapshot row per interval, and its `net_pnl_quote` moves between
      rows as the executor's does.
- [x] Every executor that completes cleanly has exactly one `is_terminal=True` row whose
      metrics equal the `ExecutorRecord` row's, written in the same transaction.
- [x] `GET /performance/history?subject=executor&executor_id=…` returns that executor's
      full series **from the snapshot table alone**, newest first, without querying
      `executors`.
- [x] `GET /performance/history?subject=controller&bot_name=…` returns the same rows the
      existing controller-history route returns, in the normalized shape, with the raw
      `performance`/`custom_info` still present and nothing dropped.
- [x] `GET /bot-orchestration/controller-performance-latest` and
      `-history` return byte-identical responses before and after the change; a test
      asserts `ControllerPerformanceRepository` and `routers/bot_orchestration.py` are
      untouched in behaviour.
- [x] Pagination on `subject=executor` matches the controller route's contract: same
      cursor format, same `has_more`, `limit` clamped at 1000; walking the cursor returns
      every row exactly once.
- [x] `interval=5m` against a 60-second grain returns roughly every fifth row, not every
      row (i.e. the grain parameterization works and the 5-minute assumption is gone).
- [x] Restart with executors live: their `ExecutorRecord` rows are terminated as
      `SYSTEM_CLEANUP` **carrying the last snapshot's PnL, fees and filled amount**, and
      `/executors/performance` no longer books them at zero.
- [x] `PERFORMANCE_RETENTION_DAYS=0` (the default) deletes nothing from either table; a
      positive value deletes only rows older than the cutoff, from both.
- [x] The snapshot table has no separate volume column, and nothing sums one —
      `test/test_executor_volume_is_the_filled_amount.py` extended and green.
- [x] A database failure inside the snapshot dump is logged and the executor control loop
      keeps ticking.

## Risks and notes

**Implementation notes (deviations from the design, and why).**

- **The terminal row is behind a SAVEPOINT.** The design said "same session as the
  `ExecutorRecord` update, so the record and its final snapshot commit together" — which
  is right, but the coupling has a direction the design did not state. A failing snapshot
  INSERT aborts the whole transaction and loses the completion: exactly the failure
  `60041a8` (`fix(executors): persist a completion whose creation row is not there yet`)
  went to some trouble to make impossible. `session.begin_nested()` keeps them in one
  transaction while letting a bad snapshot roll back only itself; the completion still
  commits and the loss is logged. Verified against Postgres by dropping the snapshot table
  out from under a completion — the `ExecutorRecord` still landed with its real figures.

- **A snapshot is skipped rather than zeroed when `executor_info` cannot be read.** The
  design was silent here. `_persist_executor_completed` substitutes `Decimal("0")` on that
  path and the record keeps taking those zeros (unchanged), but the series does not get a
  terminal row: a fabricated zero is what a reader takes for the executor's final value,
  and it is indistinguishable from an executor that genuinely made nothing. Same rule on
  the periodic path — a gap is honest, a fake point is not.

- **`prune_older_than` is not passed a grain** and `_prune_performance_snapshots` runs on
  its own hourly guard (`PERFORMANCE_PRUNE_INTERVAL_SECONDS`), nested inside the snapshot
  tick as designed.

**Testing.** The repo's suite needs `asyncio_mode=auto`, which is configured nowhere
in-tree (no `pytest.ini`, no `[tool.pytest.ini_options]`), and the conda env's
`pytest 9.1.1` + `pytest-asyncio 1.4.0` are mutually incompatible, so 82 async tests fail
before any change. Verification was done against a scratch venv with a compatible pair.
Baseline 573 passed / 1 pre-existing unrelated failure
(`test_controllers_instantiate.py::test_an_untyped_provider_is_refused_rather_than_guessed`);
after this feature, 618 passed / the same 1 failure. Worth an `/improvements` item.

**End-to-end verification.** 26 checks against a throwaway Postgres database covering
every criterion a unit test cannot answer: `create_all` making the table with no migration
entry, a live executor accumulating rows with its PnL moving between them, exactly one
terminal row matching the `ExecutorRecord`, the reap adopting the last snapshot (and
`/executors/performance` no longer booking it at zero), `interval=5m` thinning a 60s grain
to every fifth row, cursor pagination returning every row exactly once, and retention
deleting from both tables only past the cutoff. All 26 passed.

- **Two `controller_id` namespaces.** A `controller_id` on a Docker bot's MQTT report and a
  `controller_id` tagged on an in-process executor are *not* guaranteed to name the same
  thing. The unified route must not invite a join across subjects on that field; it is a
  filter within a subject only. Say so in the route's docstring.
- **`cum_fees_quote` is `null` for controllers**, because `PerformanceReport` genuinely has
  no fees field — the downstream shape assumed one. A consumer charting fees can only do it
  for the executor subject. Do not synthesize a zero: zero and unknown are different, the
  same distinction `_measured_rent` (`services/executor_service.py:348`) exists to preserve.
- **Grid and LP `custom_info` stay out of the table.** If a future consumer wants a live LP
  position's in-range flag on the curve, add one narrow typed column — never the blob.
- **The control loop now does a batch INSERT on one tick in sixty.** With hundreds of live
  executors that is a single multi-row insert; if it ever becomes visible in executor
  update latency, move the dump to its own task (the repository and the row builder do not
  change, only the caller).
- **Testing needs the real image.** The deployed API runs the published image plus PyPI
  `hummingbot`, not this checkout, so an end-to-end check means building and running that
  image — and a restart reaps executors, which is now itself part of what is under test.
  Use a throwaway fleet.
- **Downstream sequencing.** Condor's `FEAT-087` ships a client-side fallback and a
  capability probe so it works against an API without this route. Nothing here should be
  scheduled around that; this feature stands on its own for the restart-accounting fix
  alone.
- **`ControllerPerformanceRepository` is deliberately duplicated, not refactored.** The new
  repository re-implements the sampler with a grain parameter rather than generalizing the
  old one, because the old one is on a wire-compatible path that this feature must not
  touch. Collapsing the two is a fair follow-up once the new route has consumers.
