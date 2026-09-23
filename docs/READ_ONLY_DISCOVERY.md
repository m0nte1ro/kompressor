# Read-only filesystem discovery and probing

The reconciliation milestone was closed in `f580cf4`. This milestone keeps that
identity/revision model and adds read-only discovery/probe adapters. The separate
[first CPU encoding slice](REAL_ENCODING.md) writes only to the dedicated workspace;
media roots remain read-only. This document's discovery flow does not encode,
replace sources, hash media, use media servers or external metadata APIs.

## Enable and use

Seed mode remains the default and needs no ffprobe installation. To opt in:

```sh
KOMPRESSOR_MEDIA_BACKEND=filesystem \
KOMPRESSOR_MOVIES_ROOT=/your/movies \
KOMPRESSOR_SHOWS_ROOT=/your/shows \
KOMPRESSOR_FFPROBE_BINARY=/usr/bin/ffprobe \
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Roots default to unconfigured, never to production paths. Either root can be
omitted. Environment roots are defaults; paths explicitly saved in Settings take
precedence. Paths must be absolute and non-overlapping, with the application
SQLite database outside them. Saving paths does not scan, create or move media.
Use **Settings → Scan library**, or `POST /api/library/scan`, to initiate discovery.
`GET /api/library/scan` shows the current/last report in this application process.

Scans are explicit and run in a managed background thread. POST returns 202;
GET reports progress/errors. Movies and Shows update automatically about every
1.5 seconds without a page reload. Initial discovery publishes batches before
ffprobe runs; subsequent scans preserve complete-root rename reconciliation.
Each ffprobe invocation has a timeout (`KOMPRESSOR_FFPROBE_TIMEOUT`, 30 seconds by
default). Closing the browser does not cancel a running scan. The inventory
persists in indexed SQLite tables; the transient scan report resets on restart.
Run one application process. See [inventory storage](INVENTORY_STORAGE.md) for
atomic batch boundaries, migration and query measurements.
There is no startup scan or filesystem watcher. A missing ffprobe executable is
reported per file; discovered files remain visible with unknown technical facts.

## Application path

`container.py` centrally selects the adapters. The facade delegates the use case
to `LibraryDiscoveryService`:

```text
POST scan → MediaProcessor → LibraryDiscoveryService
                              ├─ FilesystemScanner → observations/stat facts
                              ├─ FFprobeService → MediaProbeResult
                              └─ ReconciliationService → existing SQLite inventory

FilesystemMediaRepository → existing Movie/Show/Episode models
                          → CatalogService / PolicyEngine → current UI and APIs
```

`FilesystemMediaRepository` is a projection, not a second real-media catalogue.
It includes present records from the currently configured roots. It exposes
`file_id` as the row/API item ID, semantic `media_id` separately, and current
`revision_id`. Direct file-row tag operations resolve to the semantic identity;
seed tag keys remain unchanged. Missing records/revisions stay in the reconciled
inventory even when hidden from the active library views.

Filesystem source mounts remain read-only. When ffmpeg, ffprobe, libx265 and the
workspace are ready, the filesystem backend can enqueue the separate CPU-only
real encode slice; otherwise queue submission returns a clear runtime error. It
never uses the seed fake worker. The original seed workflow, including its fake
workers, still works.

## Scanner behaviour

The scanner recursively enumerates supported video extensions and collects size,
mtime, ctime, device, inode and hardlink count. It does not open media for writing,
rename, delete, chmod/chown, hash, or traverse symlink files/directories. A symlink
root is unavailable. Supported extensions are listed in `filesystem_scanner.py`.

Incomplete directory traversal yields a partial scan; an inaccessible root yields
an unavailable scan. Neither marks unobserved records missing. Complete scans can
mark missing records without deleting their history. Probe failures do not turn a
physically discovered file into a missing file. Stat facts are checked again after
probing; facts from a file changed during the probe are discarded and reported.

Unchanged sources reuse persisted probe facts. Basic filesystem-derived naming is
intentional: movies use filenames; shows use the series directory and `SxxExx`
filename notation. Unrecognized episode names are reported rather than guessed.
Other naming schemes, edition/multi-episode semantics, arbitrary series-folder
renames and manual identity resolution are not added in this milestone. Existing
same-path/unique physical-identity matches preserve their semantic IDs.

## Probe facts and limitations

The adapter executes only the configured ffprobe binary, without a shell, and
restricts protocols to local file/pipe access. It requests format, streams,
chapters and a bounded sample of 48 packets for frame/side-data observations.
It parses JSON inside the adapter into the existing normalized facts model.
See the [official ffprobe documentation](https://ffmpeg.org/ffprobe.html).

Captured facts include reported codecs, dimensions, rational frame rate, scan and
field order, bit depth where derivable from an explicit pixel format, stream and
container bitrate separately, audio channels/layouts/sample rate, subtitle and
attachment streams, dispositions, languages/titles, chapters and HDR signalling.
Total container bitrate is never presented as video bitrate. Unknown duration,
video bitrate, dimensions, channels or colour metadata remain unknown; unavailable
estimates display a dash. API clients must accept nulls for these fields in real
inventory mode. Seed fixtures still carry their previous known values.

1920×1080 interlaced content stays resolution class 1080 with an `1080i` display
label. It is blocked with: “Interlaced source requires deinterlacing; no validated
pipeline is enabled”. Unknown scan type is not assumed progressive.

HDR classification derives from stored facts. Ten-bit alone does not imply HDR.
SDR signalling remains SDR; PQ/BT.2020 can be represented as HDR10. Mastering
metadata, MaxCLL/MaxFALL, Dolby Vision configuration/RPU and HDR10+ side data are
retained when exposed by ffprobe. Sampling can establish presence but cannot prove
absence of dynamic metadata throughout a file. HDR10 may therefore be identified
while remaining conservatively blocked due to incomplete dynamic-HDR evidence.
DV base-layer compatibility remains unknown; DV, HDR10+, HLG and uncertain HDR
are not enabled for transcoding. No tone mapping or deinterlacing is performed.

External subtitle discovery, full frame analysis, advanced DV classification and
hardware capability detection remain out of scope. Inventory now uses indexed schema-v2 storage; complete-root reconciliation remains
linear in the current records for that root. This does not redesign identity.

## Validation

Parser tests use checked-in ffprobe JSON. Scanner/integration tests use temporary
directories and fixture probe results, including hardlinks, unavailable/partial
roots, removals, renames, restart persistence, failed/changed probes, seed isolation
and the existing UI/API. An optional ffprobe executable test reads a tiny WAV
created with Python's standard library; it is skipped when ffprobe is absent.
