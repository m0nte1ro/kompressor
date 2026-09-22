# Kompressor

A compact, seed-backed media inventory and compression WebUI using FastAPI,
Jinja2, vanilla JavaScript and CSS. No frontend build step or external APIs.

See the [streaming preset report](docs/STREAMING_PRESETS_REPORT.md) for the five
intent-based defaults, their audio policies, the King of Comedy example and
real-media validation assumptions.

## Run locally

Requires Python 3.12 or newer:

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Run tests with `.venv/bin/python -m pytest`.
Run a **single application process**: one scheduler owns the CPU and QSV lanes.

Preferences, queue and history are stored in `data/kompressor.sqlite3` by default.
Set `KOMPRESSOR_DATABASE_PATH` (or the same variable in `.env`) to choose another
writable location. The database is created on first startup, not on import.
Stop the application before copying the database for a backup. Local databases
are excluded from Git. Tests use isolated databases in temporary directories.

## Seed workflows

- Movies and Shows display the existing JSON inventory. Episode tables are flat,
  with season separators, search, filters and mass selection.
- Review opens scoped presets and reads `/api/eligibility` for each selected item.
  Technical settings are read-only. Preserve Audio and container metadata are the
  only per-job options; the backend applies inherited protection tags.
- Queue submissions re-evaluate every item. Blocked, missing or already pending
  items are excluded individually. Encoder settings are copied from the preset,
  never accepted as client overrides.
- CPU and QSV are independent fake lanes. Each starts its next job on the next
  one-second scheduler tick. Encoding takes 180 simulated seconds, followed by
  five seconds of validation. Completed/skipped/blocked jobs appear in History.
- Default order is estimated bytes saved, descending. Manual priority overrides
  this order; Move next overrides priority within the same lane. Changing a job’s
  priority clears its Move next override. Active jobs require Stop & Skip.
- Queue/history survive restarts. Interrupted encoding/validation simulations are
  reset to zero, revalidated and queued again; downtime does not count as progress.
  No fixture or media file is written. Savings remain estimates; completing a fake
  job does not change the inventory.
- Settings separates movie/show presets and supports creating, editing,
  duplicating and enabling/disabling every preset. The initial presets are copied
  from the seed JSON once. Subsequent startups do not overwrite user edits.
  Built-in defaults are Movie Preserve Quality, Movie Streaming Quality, Show
  Preserve Quality, Show Streaming Quality and Show Streaming + Efficient Audio;
  all preserve source resolution.
  Existing jobs keep a complete preset snapshot, even if that preset is later
  edited, moved to another scope or disabled. Disable affects new submissions.
  The catalogue upgrade inserts missing current presets and disables untouched
  legacy defaults once; user edits and queued snapshots are preserved. Legacy
  custom presets need explicit source resolutions before they can be used again.
- Movies and episodes have Manage tags; episode pages also expose series and
  season tags. Select multiple rows to add/remove tags without replacing unrelated
  direct tags. The editor shows direct tags, inherited tags and their origin.

## Tag policies

Tag edits are stored separately from the seed inventory. An explicit empty
assignment removes direct seed tags; inherited tags can only be removed at their
origin. Changing tags does not bypass automatic REMUX, Dolby Vision, interlaced
or hardlink protections.

- **Preserve A/V / Preserve Video** block transcoding.
- **Preserve Audio** forces audio preservation, overriding the per-job checkbox.
- **Quality CPU** requires a CPU preset.
- **Quality Floor** requires explicit minimum output video bitrate and resolution.
  Inheritance takes the highest bitrate and resolution independently. A preset
  that would downscale below that resolution or target a lower bitrate is blocked.
  CRF/ICQ presets are also blocked because a planning range cannot guarantee a
  bitrate floor. The tag is not a perceptual-quality measurement.

## Quality modes and test output

Presets support CPU CRF, QSV ICQ and explicit ABR, along with encoder effort,
output bit depth, source resolutions and SDR/experimental-HDR10 applicability.
Quality modes carry planning bitrate ranges separately from encoder settings.
They do not return fake exact output sizes or savings; ranges are labeled as
planning estimates and may fall outside their bounds. Actual quality-mode savings
must be checked after encoding; pre-encode minimum-saving shortfalls produce a
warning.

Jobs default to `replace_source: false` (keep the original and plan a separate
test output). `true` records replacement-after-validation intent. Both remain
simulated; no source or output file is touched. Keeping originals reclaims zero
storage, and real test outputs will need additional free space. The future real
worker must validate streams/metadata and actual savings before replacing sources.

Tag writes and pending-job revalidation share a transaction. Queued jobs get
updated effective audio policy and savings. Jobs that become ineligible move to
History as **blocked**, with the reason; submit a new job after resolving it.
An active fake job also becomes blocked if protection or audio requirements
change. Before every fake worker tick, policies are rechecked against current
media/tags and the job's preset snapshot.

Tag targets currently use stable seed IDs in a seed namespace. A future scanner
must define its own stable identity and explicit reconciliation/migration before
applying these assignments to real files. Changing a path must not guess identity.

## Architecture

The existing `PolicyEngine` remains the source of eligibility decisions.
`CatalogService` shares media lookup and tag inheritance between the API, views
and scheduler. Server-rendered estimates use the first eligible scoped preset,
falling back to the first enabled preset to expose blockers. The modal opens the
suggested preset and calls the eligibility API again when inputs change.

`QueueService` coordinates ordering, state transitions and duplicate prevention
under a lock. SQLite repositories own persisted state; `FakeEncoderWorker` advances
progress independently of browser polling through the application lifespan.
Repository/worker protocols provide the future filesystem and encoder adapter
boundaries. There are no development-mode branches, authentication,
media scans, hardware checks or ffmpeg dependencies.

SQLite uses the Python standard library with parameterized statements and
transactions shared by repositories. Schema version 1 is recorded with
`PRAGMA user_version`; unknown future versions fail explicitly. `create_app`
accepts an isolated database path and media repository for tests. The fake queue
repository remains available for in-memory simulations, but is not the app's
default repository.

New endpoints: `GET/POST /api/queue`, `DELETE /api/queue/{id}`,
`PATCH /api/queue/{id}/priority`, `POST /api/queue/{id}/move-next`, and
`POST /api/queue/{id}/skip`. Interactive API documentation is at `/docs`.

Preset management: `POST /api/presets`, `PUT /api/presets/{id}`,
`POST /api/presets/{id}/duplicate` and `DELETE /api/presets/{id}` for custom
presets. Tag management: `GET /api/tags?kind=movie&id=...`
(kinds: movie, show, season, episode; season targets also need `season=...`) and
`PATCH /api/tags` with targets, tags, operation and optional quality_floor.
Bulk operations accept `add` or `remove`; single targets also accept `replace`.
