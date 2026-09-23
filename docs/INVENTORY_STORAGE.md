# Indexed inventory and background discovery

Schema version 2 replaces `metadata.reconciliation_inventory_v1` with relational
storage. File/media/revision/artifact identity and reconciliation decisions are
unchanged. Filesystem media remains read-only: no encoding, hashing, source
replacement, external metadata lookup or media-server integration is added.

## Tables and indexes

- `media_items`: semantic media ID and scope; existing tag keys remain unchanged.
- `library_files`: logical file identity, media, root/path, presence, scan sequence,
  current revision, current observation and indexed show grouping.
- `file_revisions`: historical content versions, immutable identity/reference and
  creation timestamp. Probe facts may be attached after discovery without
  inventing a new content revision.
- `observations`: per-file/current and per-revision stat/fingerprint evidence,
  container, duration, container bitrate and uncommon metadata. Separate current
  observations preserve refreshed location/hardlink facts without rewriting
  historical revision observations.
- `streams`: typed video/audio/subtitle/attachment/data rows. Codec, dimensions,
  resolution class, scan/field order, rational frame rate, pixel format, bitrate,
  bit depth, colour signalling, channels, sample rate, language and title are
  columns. Dispositions, uncommon metadata and full raw HDR signalling use small
  per-stream JSON fields; HDR classification remains derived.
- `chapters`: ordered chapter records per observation.
- `scan_runs`, `reconciliation_issues`: applied root scan sequence/status and
  indexed issues, including requested fingerprint tier and candidate IDs.
- `artifacts`: independent output identity, unique job and reserved location,
  source file/revision, complete preset snapshot and simulated lifecycle.

Indexes cover root/path, present root/scope, media ID, show ID/presence,
file/current-revision relationships, semantic scope, physical identities,
relocation evidence, stream kind/codec and artifact source references. A partial
unique index permits only one present file per root/path, while retaining missing
history. Deferred composite foreign keys enforce current-revision ownership and
allow atomic path swaps; artifact revision ownership is enforced too.

`observation_store.py` batches technical facts; `inventory_views.py` provides SQL
aggregates. SQL remains in repositories. Show cards and summary counts do not
decode episode probes. A show detail selects only that show's present files,
then batches streams, chapters and tag assignments. Presets are fetched once per
preview batch. Show/season tag lookup uses indexed present paths instead of
projecting all episodes, and reads the season paths as a cursor. Already-normalized
relative paths bypass `PurePosixPath` reconstruction; ambiguous paths still use
the existing normalization and validation. `/api/library` intentionally remains a full catalogue export;
normal page queries do not use it or load historical revisions. Diagnostic
`repository.load()` is an explicit fixture/test export, never a runtime fallback.

## Migration and transactions

Opening schema 0/1 creates schema 2 inside the existing database transaction.
The old document is validated, imported and foreign-key checked before its
metadata row is removed and `user_version` advances. Malformed data or inconsistent
references raises an explicit migration failure and rolls back both DDL and data.
Presets, tags, preferences, jobs and history are untouched. Reopening is idempotent.
Unknown newer schema versions are rejected. Keep ordinary deployment database
backups as with any migration; no destructive reset is performed.

Each reconciliation snapshot is atomic. Renamed paths are released together and
reassigned inside the transaction; failed writes roll back inventory, revisions,
issues and scan sequence. Partial snapshots query only relevant media/path
candidates. Complete scans load current records for one root to preserve existing
rename/displacement semantics; no historical revisions or other roots are loaded.
In-memory media/path indexes remove per-episode whole-root searches.

SQLite uses foreign keys and WAL. Snapshot readers do not acquire the writer lock
or start `BEGIN IMMEDIATE`. No database transaction spans walking/statting media
or invoking ffprobe. Each technical result attaches in a short transaction only
if its file/revision/stat evidence still matches the current observation.

## Progressive scans

`POST /api/library/scan` validates/captures roots and returns **202** immediately.
A single managed background thread performs discovery. Concurrent requests get
409. `GET /api/library/scan` reports scan ID, generation, phase, counts, errors and
root reconciliation results; errors remain visible if the job fails.

On an initially empty root, discovery publishes bounded partial batches (100
files or approximately one second when files are encountered). These transactions
make rows available before enumeration finishes. A final complete snapshot
reconciles the root. On subsequent scans, root enumeration completes before
reconciliation so displaced paths/renames retain the existing global matching
semantics. Metadata enumeration is separate from slow technical probing.

All configured roots are discovered before ffprobe work starts. Rows therefore
appear with unknown technical fields first; cached facts are retained for
unchanged revisions. New/changed files are probed sequentially, outside database
locks, and facts are published individually with throttled progress notifications.
The browser polls every 1.5 seconds and replaces server-rendered table/card
content without a page reload. Filters, selections and expanded metadata remain;
updates wait while a dialog is open. Settings shows progress/errors, and a banner
follows the scan onto Movies/Shows. No JavaScript policy engine is introduced.

A published batch is an independently committed observation, not a promise that
the entire scan will succeed. Failed/partial/unavailable scans never infer that
unobserved sources are missing. Only complete root enumeration does that. A probe
failure does not undo physical discovery or imply absence. Scan interruption may
leave some already-discovered rows with unknown facts; rerunning completes them.
The transient background report resets on restart; applied reconciliation runs
and issues persist. Shutdown stops between files/directories and waits for the
current bounded ffprobe call. Run **one Uvicorn worker/process**; a distributed
scan coordinator is not implemented.

## Scale validation and limits

The synthetic fixture test uses 4,080 episodes across 200 shows, including a
100-episode show. It exercises initial insertion, SQL summaries/cards, scoped
show detail, show/season tag lookup (including a late season), a mostly unchanged
scan and missing-file detection. It forbids the runtime diagnostic full export,
full probe JSON deserialization in Show HTTP responses and episode projection
during show/season tag lookup. Query budgets do not depend on episode count:
**5 SELECTs** for combined summary/cards, **3 SELECTs** for HTTP `/shows`,
and **13 SELECTs** for HTTP detail and a direct 100-episode detail. Local HTTP
timings on this fixture were approximately 0.038-0.039s for the list and
0.093-0.098s for detail before these changes, versus 0.034s and 0.093s after.
Creation, queries and reconciliation together took approximately 5.3s after.
These are development-machine observations, not a disk/ffprobe throughput
promise or a statistically significant speedup. The earlier LXC `py-spy`
profile captured a different code path; `py-spy` was unavailable locally, so
no post-change LXC profile or production speedup is claimed.

Migration tests cover round-trip/idempotence, preserved unrelated application
state and rollback for malformed/inconsistent documents. Background tests block
ffprobe and directory enumeration deliberately and verify that HTTP stays
responsive, initial rows are visible, conflicts/failures are reported, and probing
fills details. Queue polling remains responsive during a blocked probe, and
shutdown joins the active scan thread with a cancelled report. Full settings
trace: [SETTINGS_AUDIT.md](SETTINGS_AUDIT.md).

Large directory walks can still take time on slow/network storage; subsequent
scans preserve atomic root reconciliation instead of guessing at renames from
incomplete enumeration. Live updates currently render the current movie table or
show page, not a paginated/delta protocol. Full exports and complete-root
reconciliation still scale linearly with the selected data. Probe errors and
status reports are process-local; there is no scan resume after restart.
