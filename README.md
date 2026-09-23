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

## Real library and CPU encoding

Set `KOMPRESSOR_MEDIA_BACKEND=filesystem` and configure movie/show roots in Settings
(or `KOMPRESSOR_MOVIES_ROOT` / `KOMPRESSOR_SHOWS_ROOT` as defaults). Roots default to
unconfigured. Use **Settings → Scan library** to discover and probe actual files;
scans run in the background and results update automatically without reloading the
page. Seed mode still needs no media tools and keeps its deterministic fake queue.

Filesystem mode can also run one real CPU/libx265 encode at a time when ffmpeg,
ffprobe, libx265 and the dedicated writable workspace are available. It only accepts
confirmed SDR progressive sources, copies audio, keeps resolution, and writes a
separate MKV output under the workspace. Source files remain untouched. QSV, HDR,
source replacement and audio conversion are not enabled.

See [read-only discovery](docs/READ_ONLY_DISCOVERY.md) for scan behaviour and
[real CPU encoding](docs/REAL_ENCODING.md) for the supported encode slice, setup,
validation and recovery details.

## Deploy discovery in an LXC

Use Python 3.12+ and the installation steps above. Install ffmpeg with libx265 and
ffprobe (Debian/Ubuntu normally provides both in the `ffmpeg` package). Give the
application user read/traverse access to media roots, a writable application data
directory outside those roots, and a separate writable workspace with a `jobs/`
subdirectory. Settings reports whether the binaries, libx265 and workspace are
usable at startup.

```sh
KOMPRESSOR_MEDIA_BACKEND=filesystem \
KOMPRESSOR_MOVIES_ROOT=/your/movies \
KOMPRESSOR_SHOWS_ROOT=/your/shows \
KOMPRESSOR_FFPROBE_BINARY=/usr/bin/ffprobe \
KOMPRESSOR_FFMPEG_BINARY=/usr/bin/ffmpeg \
KOMPRESSOR_WORKSPACE_ROOT=/mnt/kompressor \
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Open Settings, verify/save the paths, and click **Scan library**. Navigate to
Movies/Shows while it runs: names appear first, technical details follow. Saved
paths override environment defaults, including saved empty paths. Missing
ffprobe leaves files listed with unknown metadata and reported errors.

Series naming currently requires a series directory and `SxxExx` episode names;
unsupported names are reported, not resolved through external metadata. The scanner never writes to the media roots; validated outputs go only to the
separate workspace. There is no authentication; use the intended private homelab
network.

See the [full Settings audit](docs/SETTINGS_AUDIT.md) and
[schema, migration and scale report](docs/INVENTORY_STORAGE.md).

## Seed workflows

- Movies and Shows display the existing JSON inventory. Episode tables are flat,
  with season separators, search, filters and mass selection.
- Review opens scoped presets and reads `/api/eligibility` for each selected item.
  Technical settings are read-only. Preserve Audio and container metadata are the
  only per-job options; the backend applies inherited protection tags.
- Queue submissions re-evaluate every item. Blocked, missing or already pending
  items are excluded individually. Encoder settings are copied from the preset,
  never accepted as client overrides.
- In seed mode, CPU and QSV are independent fake lanes. Each starts its next job
  on the next one-second scheduler tick. Encoding takes 180 simulated seconds,
  followed by five seconds of validation. Filesystem mode uses one real CPU lane;
  QSV remains unavailable there. Completed/failed/skipped/blocked jobs appear in
  History.
- Default order is estimated bytes saved, descending. Manual priority overrides
  this order; Move next overrides priority within the same lane. Changing a job’s
  priority clears its Move next override. Active jobs require Stop & Skip.
- Queue/history survive restarts. Interrupted seed simulations reset to zero and
  revalidate. Interrupted real jobs are requeued from zero after stale workspace
  output is removed. Real measured savings are separate from estimates; seed
  simulations never change media or inventory.
- Settings separates movie/show presets and supports creating, editing,
  duplicating and enabling/disabling every preset. The initial presets are copied
  from the seed JSON once. Subsequent startups do not overwrite user edits.
  Preset details are collapsed by default. The Settings page also persists the
  Movies and Shows library paths in SQLite; these paths are currently stored for
  the filesystem scanner; saving alone does not trigger a scan.
  Built-in display names are user-owned: Just convert to HEVC, Tone it down a bit
  - HEVC, and Tone it down a bit + HEVC + Efficient Audio. Intent is stored
    separately from those names. All preserve source resolution. Existing jobs keep
    a complete preset snapshot, even if that preset is later edited, moved to
    another scope or disabled. Disable affects new submissions. The catalogue
    upgrade inserts missing current presets and disables untouched legacy defaults
    once; user edits and queued snapshots are preserved. Legacy custom presets
    retain explicit source applicability rules, while target resolution is
    configured separately.
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
output bit depth, source applicability and SDR/HDR10 input support. The current
real worker consumes CPU CRF/ABR, x265 preset and output bit depth; QSV ICQ and
HDR preset behavior remain planning-only. Filesystem encoding accepts confirmed
SDR only, and tone mapping is unavailable.
Quality modes carry planning bitrate ranges separately from encoder settings.
They do not return fake exact output sizes or savings; ranges are labeled as
planning estimates and may fall outside their bounds. Actual quality-mode savings
must be checked after encoding; pre-encode minimum-saving shortfalls produce a
warning. The editor fills these ranges from the selected intent; they are optional
advanced planning metadata, not values that control CRF, ICQ or x265.

Jobs default to `replace_source: false` (keep the original and write a separate
test output). `true` records replacement intent, but the real worker refuses it.
Seed mode simulates jobs without touching files. Filesystem mode writes a validated
keep-output artifact under the workspace and never changes the source. Keeping the
original reclaims no storage and requires free workspace space; actual output size
and saving are recorded only after validation.

Tag writes and pending-job revalidation share a transaction. Queued jobs get
updated effective audio policy and savings. Jobs that become ineligible move to
History as **blocked**, with the reason; submit a new job after resolving it.
An active fake job also becomes blocked if protection or audio requirements
change. Before every fake worker tick, policies are rechecked against current
media/tags and the job's preset snapshot.

Seed tags retain their seed IDs. Filesystem rows use persistent file IDs, with
tag assignments owned by semantic media identity; reconciliation preserves
identity across supported renames. Seed IDs are not automatically migrated.

## Architecture

HTTP and Jinja routes resolve one `MediaProcessor` per application lifespan.
`app/container.py` assembles its catalog, preset management, queue and seed
scanner/probe dependencies. Tests may inject a processor or override the FastAPI
dependency. Application exceptions are mapped to HTTP responses centrally.

The existing `PolicyEngine` remains the source of eligibility decisions.
`CatalogService` shares media lookup and tag inheritance between the API, views
and scheduler. Server-rendered estimates use the first eligible scoped preset,
falling back to the first enabled preset to expose blockers. The modal opens the
suggested preset and calls the eligibility API again when inputs change.

`QueueService` coordinates ordering, state transitions and duplicate prevention
under a lock. SQLite repositories own persisted state; `FakeEncoderWorker` advances
progress independently of browser polling through the application lifespan.
Repository, scanner, probe and encoder protocols provide the adapter boundaries.
`FakeEncoder` implements encode/progress/stop without touching files; the fake
worker supplies the existing clock and validation simulation in seed mode. The
filesystem worker uses a separate `FFmpegEncoder` for process details, while
`FFprobeService` owns probing.
Real execution must run outside HTTP requests and scheduler transactions. There are no scattered development-mode branches, authentication, hardware checks
or ffmpeg dependencies. Only the optional filesystem backend invokes ffprobe.

SQLite uses the Python standard library with parameterized statements and
transactions shared by repositories. Schema version 2 is recorded with
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
