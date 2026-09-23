# Kompressor

Kompressor is a lightweight, self-hosted media compression manager for homelab media libraries.

Its purpose is to inspect an existing movie/TV library, understand the technical characteristics of each media file, apply user-defined safety and quality policies, estimate potential storage savings, and queue safe compression jobs using either CPU or Intel iGPU hardware encoding.

Kompressor is **not** a replacement for Sonarr, Radarr, qBittorrent, Plex, Jellyfin, or any other media manager.

It operates on an already existing media library.

---

# 1. Project Goals

Kompressor should provide:

- A simple WebUI for browsing Movies and Shows.
- Technical information about every media file.
- Safe policy-based compression.
- User-defined compression presets.
- Separate CPU and Intel QSV processing lanes.
- Bulk selection and bulk queue operations.
- Strong protection against accidentally modifying valuable media.
- Accurate enough storage-saving estimates.
- Queue prioritization based primarily on potential space savings.
- Job history and storage-saving statistics.
- A clean separation between development/mock infrastructure and production media/ffmpeg adapters.

The application should remain lightweight and easy to maintain.

---

# 2. Core Philosophy

The main priority is:

> Save significant storage without accidentally damaging valuable media.

Kompressor should be conservative when uncertain.

It is better to refuse a compression than to silently damage a high-quality source.

Examples:

- A 4K Blu-ray REMUX movie such as Dune: Part Two must never be accidentally transcoded.
- A 1080p sitcom episode at 20-30 Mbps can be compressed aggressively if visual quality remains reasonable.
- A 1080p HEVC episode already around 2 Mbps should normally be left alone.
- Dolby Vision content must not be transcoded until the pipeline explicitly supports it safely.
- Hardlinked/seeding files must not be modified.

Movies are treated significantly more conservatively than TV shows.

---

## Read-only discovery milestone

The optional `filesystem` backend now discovers video files recursively and uses
`FFprobeService` to normalize real technical metadata. MediaProcessor delegates
scan → probe → reconciliation to LibraryDiscoveryService; the existing library
views read a projection of that reconciled inventory. Seed mode remains the
default. Roots have no production defaults and can be configured through settings.
Scans are explicit and read-only. The separate [first real CPU encoding milestone](REAL_ENCODING.md)
can enqueue only when ffmpeg, ffprobe, libx265 and the dedicated workspace are
available; it leaves source media untouched and writes validated outputs elsewhere.
No source replacement, hashing or reconciliation redesign is included. See
[READ_ONLY_DISCOVERY.md](READ_ONLY_DISCOVERY.md) for scan configuration, probe
limitations, naming assumptions and verification details.

## Fixture reconciliation milestone

A separate reconciliation inventory now models persistent logical `file_id`
records, content `revision_id` versions and independent output `artifact_id`
identities. It consumes fixture snapshots through MediaProcessor, with tiered
fingerprint evidence, conservative rename matching and root-scoped absence rules.
Raw probe/HDR facts are separate from semantic media identity and derived HDR
classification. Source revision and hardlinks must be freshly revalidated both
before processing and immediately before any future replacement. The existing seed catalogue/queue remains unchanged. Real scanning and probing
are documented separately, and source replacement remains unimplemented. See
[RECONCILIATION.md](RECONCILIATION.md) for identity contracts and safety boundaries.

## Application boundary and composition

```text
Frontend → HTTP/JSON API → thin FastAPI routes → MediaProcessor
                                                ↓
                             services / repositories / adapters
```

`app/services/media_processor.py` is the application facade. It delegates to
CatalogService, PresetService and QueueService; PolicyEngine remains the sole
eligibility engine. Jinja rendering is still supported. Routes resolve the facade
through `get_media_processor` and contain only HTTP parsing/response concerns.
Application errors are mapped to HTTP 404/409/422 at that boundary.

`app/container.py:build_media_processor` centrally assembles dependencies once
per FastAPI application lifespan. The instance lives in
`app.state.media_processor`, with explicit processor injection and dependency
overrides available for tests. Run one application process for the existing
single scheduler; multiple processes sharing the queue are not supported.

Seed mode composes seed media, SQLite presets/tags/jobs, fake scanner/probe
adapters and `FakeEncoderWorker`; it remains deterministic and never touches media
files. Filesystem mode composes `FilesystemMediaRepository`, `FilesystemScanner`,
`FFprobeService` and, when startup prerequisites pass, one background CPU
`RealEncoderWorker`. `LibraryDiscoveryService` owns scan/probe/reconciliation
flow, and filesystem scans publish batches while they run.

`FFprobeService` owns probing and domain-model translation. `FFmpegEncoder` owns
all ffmpeg arguments, stream mapping, progress, subprocess lifecycle and output
paths. Real work runs outside HTTP requests and queue/database transactions. The
current worker accepts only CPU/libx265, confirmed SDR progressive sources, copied
audio, unchanged resolution and keep-output MKV. It validates with ffprobe before
publishing the result. QSV, HDR, audio conversion, deinterlacing and source
replacement remain unsupported. See [REAL_ENCODING.md](REAL_ENCODING.md) for the
current boundaries; the fake tick loop remains only for seed mode.

For compatibility, job-level `keep_output` remains represented by
`replace_source: false` in the API and persistence; `true` records replacement
intent only. Preset names, schemas and frontend contracts remain unchanged.
Spatial applicability normalizes 1080i to the 1080p resolution class while keeping
the display label and separate interlaced flag. Interlaced media is blocked for
unsupported deinterlacing, not merely because its label ends in `i`.

# 3. Development Environment

Development must NOT depend on the production homelab.

The development machine may have:

- no ffmpeg
- no ffprobe
- no Intel QSV
- no `/media`
- no actual movies/shows
- no Sonarr
- no Radarr
- no qBittorrent
- no Plex/Jellyfin
- no production workspace

Development uses fake/seed data.

Example:

````text
fixtures/
├── media.json
├── presets.json
└── queue.json

Fake services should be used where production adapters will later exist.

The development WebUI and API must be fully usable against those fixtures.

There should NOT be if development_mode checks spread throughout the codebase.

Use clear interfaces/adapters instead.

Example architecture:

Application services
        │
        ├── MediaRepository
        │       ├── SeedMediaRepository
        │       └── FilesystemMediaRepository      [future]
        │
        ├── PresetRepository
        │       ├── SeedPresetRepository
        │       └── DatabasePresetRepository       [future]
        │
        ├── QueueService
        │
        ├── ProbeService
        │       ├── FakeProbeService               [dev]
        │       └── FFprobeService                 [production]
        │
        └── EncoderWorker
                ├── FakeEncoderWorker              [dev]
                └── FFmpegEncoderWorker            [production]

Development code must not require ffmpeg to import or start the application.

4. Technology Stack

Current preferred stack:

Backend
Python 3.12+
FastAPI
Pydantic
SQLite later for persistent application state
Jinja2
Frontend

Frontend lives in the same repository as the backend.

Preferred:

server-rendered HTML
Jinja2
vanilla JavaScript
custom CSS
optional HTMX where it clearly simplifies interactions

Do NOT introduce React, Vue, Node, npm, or a frontend build pipeline without a strong reason.

The WebUI should feel like a polished self-hosted/homelab tool rather than a generic SaaS dashboard.

Dark UI, compact, desktop-first.

5. Repository Layout

Target shape:
kompressor/
├── app/
│   ├── main.py
│   ├── config.py
│   │
│   ├── models/
│   ├── repositories/
│   ├── services/
│   ├── workers/
│   ├── routers/
│   │
│   ├── templates/
│   └── static/
│
├── fixtures/
│   ├── media.json
│   ├── presets.json
│   └── queue.json
│
├── tests/
├── pyproject.toml
├── PROJECT_SPEC.md
├── README.md
└── LICENSE

Frontend templates, JS and CSS remain in this repository.

6. Production Environment

Production target is a Debian LXC container named kompressor.

The production system will eventually have access to:
/media/movies
/media/shows
/mnt/kompressor
The media library currently originates from a mergerfs storage pool on the Proxmox host.

The temporary compression workspace should live on host SSD/ZFS storage and appear inside the container as:
/mnt/kompressor
The media filesystem is the source of truth.

SQLite stores application metadata/state, not the media itself.

7. Media Inventory

Kompressor must inventory Movies and TV Shows.

Technical information should include where available:

name/title
year
filesystem path
source/release type
file size
duration
width
height
resolution label
video codec
video bitrate
HDR type
Dolby Vision presence
interlaced/progressive state
audio tracks
audio codecs
channel count
language metadata
subtitle streams
chapters
attachments
hardlink count
tags
queue state
compression eligibility
estimated output size
estimated potential saving

Bitrate may come directly from metadata when trustworthy or be calculated from stream/file data.

8. Movies View

Movies are shown as a flat table.

Suggested columns:
Select
Movie
Year
Source
Resolution
Video Codec
HDR / DV
Bitrate
Size
Audio
Policy / Tags
Estimated Saving
Action
Useful filters:

search
H.264
HEVC
1080p
4K
HDR
Dolby Vision
protected
compressible
hardlinked
queued
low bitrate
large files

Movie compression uses movie-scoped presets only.

A Show preset must never appear when operating on a Movie.

9. Shows View

The first Shows page lists individual shows.

Example:

The Vampire Diaries
171 episodes
493 GB

Modern Family
250 episodes
620 GB

Opening a show displays a flat episode table.

Do NOT display real season folders.

Instead use small visual separators:

Season 1

S01E01 ...
S01E02 ...

Season 2

S02E01 ...
S02E02 ...

Suggested columns:

Select
Episode
Resolution
Video Codec
Bitrate
Size
Audio
Effective Policy / Tags
Estimated Saving
Action

Show compression uses show-scoped presets only.

A Movie preset must never appear when operating on a Show/Episode.

10. Presets

Kompressor ships with exactly five intentionally simple enabled built-in presets.
They describe user intent rather than resolution-specific variants. Every default
preset preserves the source resolution.

Users choose one of these intents:

- preserve perceived quality: visually transparent / extremely difficult to distinguish from the source during normal viewing
- streaming-style compression: substantial storage reduction with premium-streaming-style visual goals
- efficient audio for Shows: the same Show streaming video policy with deterministic audio conversion

The words Preserve Quality do not promise mathematical or bit-perfect equality.
HEVC/x265 transcoding is lossy unless a true lossless mode is used, which is not
the default workflow.

Presets describe the complete technical compression policy.

The user should normally choose a preset rather than manually configuring encoder parameters for each job.

A preset contains at least:

id
name
scope: movie | show
intent: preserve_quality | streaming_quality
origin: built_in | custom
enabled/disabled

backend:
    cpu
    qsv

destination codec:
    hevc
    av1 [future / disabled for now]

target video bitrate
rate control and encoder configuration
quality value and clearly experimental planning range when using CRF/ICQ

target resolution:
  keep
  max_2160p
  max_1080p
  max_720p
  max_576p
  max_480p

source applicability:
  supported input resolutions, separate from the output target; defaults to all
  supported resolutions and is not a normal user-facing preset choice

audio policy:
    preserve
    efficient

audio conversion policy and preserve-audio-by-default setting
efficient audio rules for mono/stereo codec, multichannel codec, bitrates,
channel handling and copy conditions

target audio bitrate, if applicable

HDR input support and output policy
preserve source HDR mode by default; no implicit tone mapping
HDR10 signalling preservation/validation for primaries, transfer, matrix,
mastering display metadata, MaxCLL, MaxFALL and related playback metadata
minimum source bitrate
minimum expected saving %
allow HEVC re-encode

AV1 may be represented in the model/UI but should remain disabled until implemented.

11. Preset Scope

Preset scope is strict.

scope = movie

means the preset only appears for Movies.

scope = show

means the preset only appears for Shows/Episodes.

Scope filtering should be enforced by the backend/API and not merely hidden in JavaScript.

12. Default Preset Catalogue

The only enabled built-in defaults are:

Just convert to HEVC
Scope: Movie; CPU/x265; HEVC; CRF quality mode; preserve source resolution;
copy every audio track and language untouched; conservative, experimental
high-fidelity starting point.

Tone it down a bit + HEVC
Scope: Movie; CPU/x265; HEVC; CRF quality mode; preserve source resolution;
copy every audio track and language untouched.

Just convert to HEVC
Scope: Show; CPU/x265; HEVC; CRF quality mode; preserve source resolution;
preserve audio; conservative, experimental high-fidelity starting point.

Tone it down a bit + HEVC
Scope: Show; Intel QSV/iGPU; HEVC; ICQ quality mode; preserve source resolution;
preserve every audio track and language by default.

Tone it down a bit + HEVC + Efficient Audio
Scope: Show; Intel QSV/iGPU; exactly the same video policy as Show Streaming
Quality; preserve source resolution; deterministic efficient audio by default.
The rules retain languages, tracks and channel layouts, copy already-efficient
tracks where sensible, use AAC for mono/stereo and E-AC3 for multichannel audio,
and never downmix unless an explicit future audio policy requests it.

All CRF/ICQ values, speed settings and planning ranges are experimental until
validated against real media. CRF and ICQ values are not equivalent.

Preset names are user-owned display data and are separate from intent. Built-in
intent may remain preserve_quality or streaming_quality without changing a name
the user has chosen.

Specialized behavior such as 4K to 1080p is created by duplicating or creating a
custom preset and setting its target resolution to max_1080p. It must not be
encoded in a default preset name.

Custom presets support create, duplicate, edit, enable/disable and delete.
Duplicating a built-in creates an independent custom preset with a new ID and a
user-chosen name. Existing custom presets and queued preset snapshots are
preserved during built-in catalogue migration.
13. Compression Modal

The compression modal must intentionally expose very few editable parameters.

The user selects:

Preset

The following preset properties are then displayed as read-only / disabled controls:

Video backend
Destination codec
Target bitrate
Resolution policy
Audio policy
HDR policy

The user should NOT modify those fields in the compression modal.

They belong to the preset.

Only these per-job overrides remain editable:

Preserve Audio
Preserve subtitles / chapters / attachments / metadata

The modal should also display:

original/source format
source resolution
source bitrate
source size
source audio
source HDR/DV state
selected preset
eligibility
blocking reasons
warnings
estimated output size
estimated saving
estimated saving percentage

For CRF/ICQ presets, planning ranges remain internal estimate metadata. Do not
present them as editable encoder settings or a midpoint as an exact predicted
output.

Eligibility must come from the backend policy engine.

Do NOT duplicate policy logic in JavaScript.

14. Audio Behaviour
Preserve Audio enabled

All audio tracks are copied bit-for-bit where the container supports them.

This includes, for example:

TrueHD
DTS-HD MA
E-AC3
AC3
AAC
multiple languages
commentary tracks
Preserve Audio disabled

The preset's audio conversion policy decides what to do. Both built-in Movie
presets always stream-copy every source audio track. The Movie modal Preserve
Audio option is checked and locked for those presets. Movie audio conversion,
downmixing and track removal are not supported by the built-in presets.

The Show preserve-quality and Show streaming-quality intents copy every audio
track untouched by default. The Efficient Audio Show preset is the only built-in
that enables conversion by default.

For a preset explicitly configured for audio conversion, it may use efficient codecs such as:

E-AC3
AAC

Track languages, dispositions and channel layouts should be retained where
practical. Downmixing surround audio is allowed only when explicitly part of an
audio policy; it is not a default behaviour.

Example:

For Modern Family, preserving high-end surround audio is not important.

For visually/cinematically important content, audio preservation is preferred.

A Preserve Audio tag always overrides a preset that would otherwise re-encode audio.

Efficient audio policy must be deterministic enough to describe mono/stereo and
multichannel codecs and bitrates, when already-efficient tracks are copied, how
unknown bitrates are handled, and how channels are handled.

15. Subtitle and Container Metadata

Default behaviour should preserve:

subtitles
subtitle language metadata
default/forced dispositions
chapters
attachments
embedded fonts
container metadata where practical

The compression modal contains an option:

Preserve subtitles / chapters / attachments / metadata

Default:

enabled
16. Tags

Tags are owned by Kompressor.

They are not Sonarr/Radarr tags.

Initial important tags:

Preserve A/V
Preserve Video
Preserve Audio
Quality CPU
Quality Floor
17. Tag Inheritance

TV tags inherit through:

Series
  ↓
Season
  ↓
Episode

Example:

House of the Dragon
└── Quality CPU

All episodes inherit Quality CPU.

A season can add additional tags.

An episode can add additional tags.

The effective policy is derived from the inherited tag set.

When rules conflict, the more restrictive protection wins.

Movies have direct tags.

18. Tag Semantics
Preserve A/V

Neither video nor audio may be modified.

Effectively immutable for transcoding.

Preserve Video

Video bitstream may not be transcoded.

Audio/container operations may theoretically remain possible.

Preserve Audio

Audio must remain bit-for-bit preserved.

Video may still be compressed if otherwise eligible.

Quality CPU

Compression should use a CPU/x265 quality-oriented preset instead of the normal QSV show preset.

Typical use:

House of the Dragon
visually important series
Quality Floor

Allows compression but prevents quality from dropping below the configured quality/resolution/bitrate policy.

This is preferable to abusing Preserve Video for TV shows.

19. Movies Are Conservative

Movie policies are intentionally stricter than TV policies.

A movie downloaded as a high-quality 4K REMUX is assumed to have been selected intentionally at that quality.

Automatic rule:

Movie
+ 2160p
+ REMUX
= protected by default

The normal compression workflow must not touch it.

Example:

Dune: Part Two
UHD Blu-ray REMUX
Dolby Vision / HDR

must remain untouched.

This is a core product safety rule.

20. TV Shows Are More Aggressive

TV episodes may be compressed far more aggressively where appropriate.

Example:

A 1080p episode with:

H.264
25 Mbps
6 GB

may reasonably become approximately:

HEVC
2.5 Mbps
~1-2 GB

for normal everyday viewing, assuming acceptable visual quality.

The goal is not mathematically lossless preservation.

The goal is to avoid visible objectionable degradation while recovering significant storage.

A visually unimportant sitcom should not consume REMUX-like bitrate.

21. Compression Floors

Compression should only occur when worthwhile.

Presets define a minimum source bitrate.

Example:

Tone it down a bit + HEVC
quality mode with an experimental planning range
minimum source bitrate remains explicit

A source already at:

1080p HEVC
2.1 Mbps

should normally be:

Not worth compressing

A minimum expected percentage saving should also exist.

Example:

minimum_expected_saving = 20%

Do not spend hours encoding to recover insignificant storage.

22. HEVC Recompression

HEVC → HEVC recompression should be configurable per preset.

Default philosophy:

Movies:

HEVC → HEVC OFF

unless a specific quality preset explicitly allows it.

Shows:

May be allowed for selected custom presets, particularly unusually high-bitrate HEVC sources.

Low-bitrate HEVC should normally remain untouched.

23. Resolution Policy

The user-facing resolution setting is a target-resolution cap with these options:

KEEP | 2160p | 1080p | 720p | 576p | 480p

KEEP is the default for every built-in preset. A numeric option means maximum
output resolution, never upscaling. For example, a 4K source with 1080p selected
is downscaled to 1080p, while a 720p source with 1080p selected remains 720p.

Default behaviour:

Never downscale unless the selected preset explicitly says to.

Examples:

2160p → 2160p
1080p → 1080p

Source applicability remains a separate advanced rule. It determines which input
resolutions a preset accepts and does not determine the output resolution.

24. HDR and Dolby Vision

HDR processing must be conservative.

Dolby Vision

Initial policy:

Dolby Vision transcoding = BLOCKED

Do not risk losing Dolby Vision RPU/metadata.

HDR10

HDR10 sources may be encoded to HEVC only through a tested HDR-safe pipeline that
preserves HDR10 mode and validates, where present, 10-bit output, colour
primaries, PQ transfer characteristics, matrix coefficients, mastering display
metadata, MaxCLL, MaxFALL and related playback signalling.

HDR metadata preservation should be the default.

HDR → SDR

Do NOT expose this as a normal/default option. No built-in preset performs tone
mapping.

HDR to SDR requires tone mapping and is intentionally destructive.

If supported later, it belongs only to an explicit custom/Advanced workflow with
warnings and validation.

25. Interlaced Video

Interlaced media such as some Top Gear 1080i sources requires a correctly validated deinterlacing pipeline.

Initial policy:

Interlaced detected
→ compression blocked

Do not blindly encode interlaced material as progressive.

Support may be added later once the deinterlacing pipeline is tested.

26. Hardlinks / Seeding Safety

This rule is extremely important.

Media files can be hardlinked to qBittorrent download files.

Replacing the library-side file while another hardlink still exists may:

fail to reclaim expected storage
interfere with seeding assumptions
create confusing duplicate data

Initial rule:

st_nlink > 1
→ compression blocked

UI should show something like:

Hardlinked / Seeding

There is no override in v1.

27. Eligibility Engine

Eligibility must be calculated on the backend.

Possible blocking reasons include:

Preserve A/V
Preserve Video
hardlinked/seeding
2160p Movie REMUX
Dolby Vision
interlaced
source bitrate below preset floor
HEVC recompression disabled
estimated saving below minimum
preset scope mismatch
disabled preset
already active/queued where relevant

The UI displays backend results.

The UI must not independently recreate those rules.

28. Potential Saving

Potential savings are estimates.

They do not need to predict exact encoder output.

For target-bitrate presets, estimation can use approximately:

duration × target video bitrate
+ target/preserved audio
+ small container overhead

UI wording should clearly indicate:

Estimated
~3.8 GB saving

Do not present estimates as guarantees.

29. Mass Selection

Movies and Episodes support mass selection.

Example:

100 selected
93 eligible
7 excluded

Blocked items should NOT cause the entire bulk operation to fail.

The modal should explain exclusions.

Eligible items are added to the queue.

Selected queued jobs can be bulk removed if they are not active.

Active jobs cannot be removed with the normal queue-delete action.

30. Queue Architecture

There is one logical scheduler with two independent execution lanes.

Scheduler
├── CPU Queue
└── QSV Queue

The lanes may process jobs concurrently.

31. CPU Queue

CPU jobs normally include:

Movies
Just convert to HEVC
Quality CPU-tagged shows
selected custom quality jobs

Production encoder will eventually use CPU/x265.

Development uses a fake worker.

32. QSV Queue

QSV jobs normally include:

everyday TV compression
Tone it down a bit + HEVC
Tone it down a bit + HEVC + Efficient Audio

Production encoder will use Intel Quick Sync Video.

Development uses a fake worker.

33. Worker Concurrency

Initial production target:

CPU workers = 1
QSV workers = 1

A CPU encode and QSV encode may run simultaneously.

The host CPU is usually lightly loaded.

However Kompressor must leave system headroom for media playback/transcoding.

Long-term production considerations:

CPU budget around 70-75%
lower scheduler priority / nice level
avoid monopolizing all host resources
do not start a new QSV compression job if the media server is actively using hardware transcoding

The media server has priority over Kompressor.

34. Queue Priority

Default queue ordering should primarily favour:

estimated bytes saved

Large space-saving jobs naturally rise in priority.

Movies may often rank highly because one movie can recover large amounts of storage.

Manual override must exist.

Possible priority concept:

Urgent
High
Normal
Low

Useful actions:

Move next
Change priority
Remove queued job
35. Active Queue Jobs

An active job cannot be deleted with a normal X.

Initial controls:

Stop & Skip

This means production behaviour will eventually:

terminate encoder
delete temporary output
mark job skipped
start next job

Pause support may be implemented later.

Do not overcomplicate v1 with process suspension/resume.

36. Development Queue

Development must not spawn ffmpeg.

Use fake queue data and fake workers.

Fake workers should be able to simulate:

QUEUED
→ ENCODING
→ progress updates
→ VALIDATING
→ COMPLETED

and optionally:

FAILED
SKIPPED

This allows the full WebUI workflow to be developed without media tooling.

37. Production Job Lifecycle

Eventually a real job should follow:

DISCOVERED
→ QUEUED
→ ENCODING
→ VALIDATING
→ REPLACING
→ COMPLETED

Failure:

ENCODING
→ FAILED

The original file must remain untouched until the new output has passed validation.

Output handling is a job option, not a quality preset. `replace_source: false`
keeps the original and stores a validated real output in the Kompressor workspace;
seed mode simulates that choice. `replace_source: true` records intent for review
but is refused by the real worker. No replacement workflow is implemented.
Future options such as sample_only must not require redesigning the preset model.

38. Safe Replacement

Never encode directly over the source file.

Target production workflow:

source.mkv

→ encode into /mnt/kompressor/jobs/<job-id>/output.mkv

→ validate output

→ source.mkv renamed to temporary backup
→ validated output moved into source path

→ final validation

→ backup removed

If replacement fails:

restore backup

The operation should behave as transactionally as practical.

39. Validation

Before replacing a source, production validation should verify as appropriate:

output exists
output size > 0
ffprobe succeeds
expected video stream exists
duration sufficiently matches source
resolution obeys policy
audio policy obeyed
subtitle streams preserved when requested
chapters preserved when requested
attachments preserved when requested
HDR policy obeyed
output is not obviously corrupt

Failure means:

do not replace original
40. Prevent Recompressing Kompressor Output

Kompressor should record that it processed a file.

Eventually this may be stored both in SQLite and media/container metadata where practical.

Example metadata:

ENCODER=Kompressor
KOMPRESSOR_PRESET=Tone it down a bit + HEVC
KOMPRESSOR_VERSION=1

The scanner should recognise previously processed files.

Avoid accidental chains like:

AVC
→ HEVC
→ HEVC
→ HEVC
41. Queue Page

Queue UI should visibly contain two lanes.

Example:

CPU Queue

ACTIVE
The King of Comedy
Tone it down a bit + HEVC
63%
planning range for potential reclaim

UP NEXT
1. ...
2. ...


QSV Queue

ACTIVE
Modern Family S03E04
Tone it down a bit + HEVC
41%
planning range for potential reclaim

UP NEXT
1. ...
2. ...

Each lane displays:

worker state
active job
progress
preset
estimated saving
queued jobs
priority
Move Next
remove queued job
Stop & Skip active job
42. Settings

Settings should remain focused.

Useful sections:

Paths

Persisted library preferences:

Movies path
Shows path

The Settings page stores these paths in SQLite. They configure the future scanner
but do not trigger scanning or move media in the current seed workflow.

Production-only workspace path:

Workspace path
Workers
CPU workers
QSV workers
CPU resource budget
Safety
Block hardlinks
Block Dolby Vision
Block interlaced
Protect 2160p REMUX Movies
Minimum expected saving
Presets

Create/edit/delete presets.

Movie and Show presets are clearly separated.

Tags

Manage available Kompressor tags and tag policies.

Scanning

Eventually:

reconciliation interval

Do not turn Settings into a giant collection of obscure ffmpeg flags.

43. Preset Editing

Technical encoding settings belong in preset management.

They should not clutter the normal compression modal.

A preset editor may configure:

scope
backend
codec
target bitrate
resolution policy
audio policy
target audio bitrate
HDR preservation
minimum source bitrate
minimum expected saving
HEVC recompression permission

Future advanced encoder controls may be added later if genuinely necessary.

44. Scanner

Production scanner should eventually use:

/media/movies
/media/shows

No dependency on Sonarr/Radarr is required for inventory.

Preferred approach:

initial filesystem scan
+ filesystem notifications/inotify
+ periodic reconciliation

The application should tolerate imperfect filenames.

No external metadata API is required.

Readable filesystem-derived names are sufficient.

45. Authentication

No application authentication is required in the initial version.

Production access will be constrained by:

LAN
Cloudflare access/tunnel
possibly Tailscale

Authentication can be revisited later if necessary.

46. History

Kompressor should maintain job history.

Example:

Modern Family S03E04

Before: 6.1 GB
Planning output range: 1.0–1.8 GB
Planning saving range: 4.3–5.1 GB
Actual output and saving: recorded only after a real encode

AVC → HEVC
QSV
Show streaming-quality intent

Duration: 21 min
Completed: <timestamp>

Failed jobs should retain useful error information.

47. Statistics

Useful global statistics:

Total storage saved
Files compressed
CPU encodes
QSV encodes
Average reduction %
Failed jobs
Encoding time

Possible future breakdowns:

space saved by Show
space saved by Movie
space saved by preset
space saved per month
48. UI Badges / State Visibility

Important states should be visible rather than silently hiding actions.

Examples:

Preserve A/V
Preserve Video
Preserve Audio
Quality CPU
Quality Floor
Hardlinked
Dolby Vision
HDR10
Interlaced
Already HEVC
Low bitrate
Queued
Encoding
Protected

When an item cannot be compressed, the UI should explain why.

Do not simply hide the action.

49. Error Philosophy

Kompressor should fail safely.

If the application is uncertain:

do not alter the source

If validation fails:

do not alter the source

If metadata is unsupported:

block or warn

A failed compression is acceptable.

A silently damaged original is not.

50. Current Development State

The repository currently has:

FastAPI application
Python 3.12 environment
seed media repository
seed preset repository
Movie/Show/Season/Episode models
CompressionPreset model
policy/eligibility engine
seed fixtures
scoped preset API
media library API
summary API
eligibility API

Current useful endpoints include:

GET /healthz
GET /api/library
GET /api/summary
GET /api/presets
GET /api/presets?scope=movie
GET /api/presets?scope=show
POST /api/eligibility
POST /api/presets/{id}/duplicate
DELETE /api/presets/{id} for custom presets only

Verified behaviour:

Show preset scoping

scope=show only returns Show presets.

Modern Family eligibility

A high-bitrate H.264 Modern Family episode with the Show streaming-quality intent is eligible.

Example result:

backend = qsv
destination codec = hevc
rate control = experimental ICQ quality mode
planning range rather than an exact output-size prediction
Dune safety

A Dune: Part Two UHD REMUX seed item returns ineligible due to:

Preserve A/V
2160p REMUX auto-protection
Dolby Vision

This behaviour must remain protected by future tests.

51. Git Workflow

Use small, coherent commits.

Examples:

Bootstrap Kompressor application
Add seed presets and media eligibility policy engine
Add seed-backed Kompressor web interface
Add fake dual-lane queue scheduler
Add preset management
Add persistent job history

Avoid huge unrelated commits.

Before starting work:

git status

The working tree should be clean.

Before committing:

run tests
inspect diff
make sure generated/local files are not included

Do not commit:

.venv
.env
SQLite runtime databases
logs
temporary outputs
actual media
credentials
API keys
52. Current Product Decisions That Should Not Be Accidentally Reversed

These are intentional decisions:

Frontend and backend live in the same repository.
Development requires no ffmpeg or real media.
Seed/fake adapters are first-class development infrastructure.
Movie and Show presets are strictly separated.
Technical encoder settings belong to presets.
The five enabled built-ins use the user-owned names Just convert to HEVC, Tone it
down a bit + HEVC, and Tone it down a bit + HEVC + Efficient Audio across their
Movie and Show scopes. Their intent remains separate from those names.
All five preserve source resolution; resolution-specific behavior is custom-preset configuration.
Built-ins have a built_in origin; user-created and duplicated presets have a custom origin.
Built-in catalogue migration is idempotent and does not rewrite custom presets or queued snapshots.
Duplicating is the preferred path for creating specialized presets such as a Show Streaming 1080p Max custom preset.
Compression modal is intentionally simple.
Movies are treated more conservatively than Shows.
2160p REMUX Movies are auto-protected.
Dolby Vision transcoding is initially blocked.
Interlaced transcoding is initially blocked.
Hardlinked media is initially blocked.
Resolution is preserved unless a preset explicitly allows downscaling.
Preserve Audio copies all audio streams.
Audio conversion/downmix behaviour belongs to the preset.
Subtitle/chapter/attachment preservation is default.
Tags are Kompressor-native.
TV tags inherit Series → Season → Episode.
CPU is preferred for quality-oriented Movie/Show jobs.
QSV is preferred for everyday TV compression.
CPU and QSV workers may operate concurrently.
Queue priority defaults primarily to planning storage savings until real measurements exist.
Active jobs cannot be casually removed.
No real file replacement occurs before validation.
Original media safety is more important than successful compression.
No application authentication is required for v1.
SQLite will eventually store application state, but media remains filesystem source-of-truth.
Do not introduce unnecessary external services or frontend frameworks.
53. Immediate Development Roadmap

Near-term milestones:

1. Seed-backed WebUI
2. Dual CPU/QSV fake queue
3. Preset management UI
4. Kompressor tags and inheritance UI
5. Fake worker progress/state transitions
6. History and statistics
7. SQLite persistence
8. Production filesystem scanner
9. ffprobe integration
10. Intel QSV production adapter
11. x265 CPU production adapter
12. validation pipeline
13. safe transactional replacement
14. media-server resource awareness

Production encoding should only begin after the WebUI, policies, queue model and safety lifecycle are already well tested with fake data.


---

## 2. Prompt para o Codex

Depois de teres esse `PROJECT_SPEC.md` no repo, dava-lhe isto:

```text
You are working on the Kompressor repository.

Repository location:

~/kompressor

First, read `PROJECT_SPEC.md` completely.

Treat it as the source of truth for product intent, architecture, safety rules, development constraints, UI behaviour, preset behaviour and future production requirements.

Then inspect the entire current repository before making changes.

Do not blindly regenerate or replace existing code. Understand what already exists and preserve working architecture unless there is a concrete reason to change it.

Before changing anything:

1. Run `git status`.
2. Confirm the working tree is clean.
3. Inspect:
   - current FastAPI app
   - models
   - repositories
   - services
   - fixtures
   - tests
   - pyproject.toml
4. Run the current tests/API sanity checks if practical.

Do not install or depend on:

- ffmpeg
- ffprobe
- Intel QSV
- Node
- npm
- React
- Vue
- real media files
- Sonarr
- Radarr
- qBittorrent
- Plex
- Jellyfin
- external APIs

Development must remain entirely seed/fake-data driven.

## Task

Implement the first real seed-backed Kompressor WebUI.

The backend already contains media models, seed repositories, preset models and an eligibility/policy engine.

Do not duplicate eligibility rules in JavaScript.

The browser must consume backend behaviour.

The UI should be polished enough to represent the intended product, not merely a debug page.

Use:

- FastAPI
- Jinja2
- vanilla JavaScript
- custom CSS

HTMX is allowed only if it genuinely simplifies something.

Keep frontend and backend in the same repository.

Prefer:

```text
app/templates/
app/static/

Keep JavaScript in dedicated static files rather than large inline scripts.

Keep CSS in dedicated static files.

Required navigation

Create a persistent sidebar with:

Movies
Shows
Queue
History
Settings

Movies, Shows and Queue should be meaningfully interactive.

History and Settings should also have useful seed-backed views.

Movies page

Render seed Movies as a flat table.

Columns:

mass-select checkbox
Movie
Year
Source
Resolution
Codec
HDR / DV
Bitrate
Size
Audio
Policy / Tags
Estimated Saving
Action

Add:

search
basic useful filters
individual compression action
mass selection

Movie compression must only expose presets with:

scope == "movie"

Show presets must never appear in Movie workflows.

Protected media should remain visible.

Do not hide blocked actions without explanation.

Examples that must visibly demonstrate policy behaviour:

Dune: Part Two is protected.
hardlinked media is blocked.
Shows page

Initial page lists Shows with useful summary information.

Opening a Show displays all Episodes in one flat table.

Do not display actual season directories.

Use small visual separators such as:

Season 1
Season 2

Episode columns:

checkbox
Episode
Resolution
Codec
Bitrate
Size
Audio
Effective Policy / Tags
Estimated Saving
Action

Support search and mass selection.

Show/Episode compression must only expose presets with:

scope == "show"

Movie presets must never appear.

Compression modal

The modal should intentionally be simple.

Editable:

Preset
Preserve Audio
Preserve subtitles / chapters / attachments / metadata

Preset technical properties are read-only.

Display them using disabled/read-only controls or clearly styled information fields:

Video backend
Destination codec
Target bitrate
Resolution policy
Audio policy
HDR policy

Also show:

source summary
source codec
source resolution
source bitrate
source size
audio summary
HDR/DV status
eligibility result
blocking reasons
warnings
estimated output
estimated saving
estimated saving percentage

Whenever the selected preset or editable override changes, call the backend eligibility API.

Do not implement policy rules in JavaScript.

Preserve Audio inheritance/tag overrides should be clearly shown.

For bulk operations:

evaluate all selected items
display eligible count
display excluded count
blocked items must not prevent eligible items being queued

Example:

93 eligible
7 excluded

Queue

Implement a development-only fake dual-lane queue.

One logical scheduler conceptually has:

CPU Queue
QSV Queue

Render these as visibly separate lanes.

Jobs are assigned according to the selected preset backend.

CPU and QSV lanes represent independent workers that may run concurrently.

This original UI scope describes seed mode only: it uses no subprocesses, ffmpeg
or real encoding. Seed mode still uses the fake infrastructure below. The current
filesystem real-encode slice is documented in [REAL_ENCODING.md](REAL_ENCODING.md).

Use clean fake infrastructure, preferably a fake queue repository/service and fixture data if necessary.

Each lane should support/display:

active job
queued jobs
fake progress
media name
preset
estimated saving
priority
Move Next
remove queued job
Stop & Skip active fake job

An active job must not have the same ordinary delete/X behaviour as a queued job.

Default ordering should favour estimated bytes saved.

Manual priority override should still be possible.

Fake progress/state transitions are welcome if they can be implemented cleanly without overengineering.

History

Create a useful seed-backed History view.

Show examples such as:

source size
final size
bytes/percentage saved
source codec
destination codec
backend CPU/QSV
preset
elapsed time
completion time
success/failure state

Also include summary statistics such as:

total saved
files processed
average reduction
CPU jobs
QSV jobs
failed jobs

Fake data is fine.

Settings

Settings should represent the intended product. Preset CRUD is persistent; future
worker, scanner and infrastructure settings may remain informational until their
production adapters exist.

At minimum show:

Movie Presets

Only Movie presets.

Show Presets

Only Show presets.

For each preset show:

name
scope
intent
origin
backend
destination codec
target bitrate or experimental planning range
resolution policy
audio policy
audio conversion policy
preserve-audio-by-default state
HDR preservation
minimum source bitrate
minimum expected saving
allow HEVC re-encode
enabled state

Also visually represent future settings sections for:

paths
workers
safety policies
tags
scanning

Do not invent large amounts of backend persistence yet.

Design

Use a dark, compact, polished, desktop-first visual design.

This is a self-hosted homelab tool.

Avoid generic oversized SaaS cards and excessive gradients.

Tables should be information-dense but readable.

Use clear badges for:

Preserve A/V
Preserve Video
Preserve Audio
Quality CPU
Quality Floor
Hardlinked
Dolby Vision
HDR10
Interlaced
Already HEVC
Low bitrate
Queued
Encoding
Protected

Blocked items should explain why.

Architecture constraints

Respect PROJECT_SPEC.md.

In particular:

no ffmpeg dependencies in development
no real filesystem media scanning yet
no external metadata APIs
no authentication
no database unless clearly required for this milestone
no development-mode conditionals scattered throughout application code
maintain clear future adapter boundaries
frontend must not contain duplicated policy logic
Movie and Show preset scope separation must be enforced by backend behaviour
original media safety philosophy must not be weakened

If a significant contradiction exists between the current implementation and PROJECT_SPEC.md, stop and explain it before performing a major refactor.

Tests

Add useful tests for new backend endpoints/services.

At minimum preserve tests around:

Movie preset scoping
Show preset scoping
Modern Family sample eligibility
Dune safety/protection
hardlinked media blocking

Add tests for queue API/service behaviour if you create queue backend endpoints.

Run:

pytest

Also start/import the FastAPI app locally to catch runtime/import/template errors.

Do not require browser automation.

Git workflow

Do not push.

Before making changes:

git status

At the end:

Run tests.
Show test result.
Show git diff --stat.
Summarize created/modified files.
Briefly explain architecture choices.
Create one coherent commit:

Add seed-backed Kompressor web interface

Do not modify LICENSE.

Do not commit:

.venv
local databases
logs
generated temporary files
credentials
secrets

After committing, stop.

Do not push to GitHub.
````
