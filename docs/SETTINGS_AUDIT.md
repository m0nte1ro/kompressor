# Runtime settings audit

This audit tracks the runtime effect of editable and environment settings. The
[real encoding lanes](REAL_ENCODING.md) consume a conservative subset of preset/job
fields and validate their MKV output. Filesystem mode has independent CPU/libx265
and Intel GPU/hevc_vaapi worker processes; both remain confirmed-SDR, progressive and
keep-resolution, and either keep the output in the workspace or replace an MKV
source after verification. Audio is copied unless a job opts out; only GPU may then
apply the Efficient Audio rules. Unsupported settings are refused instead of silently
ignored. The scanner never writes to source roots; only a replace job's swap does.

## Precedence and timing

For `Settings` construction: explicit Python arguments > process environment >
`.env` > model defaults (Pydantic Settings). Startup configuration uses the
`KOMPRESSOR_` prefix. Adapter settings require restart; they are not editable in
the Settings form. `GET /api/settings/runtime` reports captured runtime values
and apply timing.

Paths additionally use the persisted **whole** `LibraryPaths` object, if present,
in preference to environment defaults. Empty saved paths disable a root; they do
not fall back to the environment. A save immediately changes which roots library
queries display. The next scan captures the saved paths once; a running scan
continues with its original roots. Saving never initiates a scan or moves files.

Presets use seeded/catalogue-migrated defaults, then persistent edits. Catalogue
migrations are marker-controlled and may intentionally normalize built-in
configurations; custom presets and queued snapshots are preserved. New jobs
capture a complete preset. Later preset edits do not rewrite queued jobs. Tags
are applied immediately, inherit Show → Season → Episode, and revalidate pending
jobs. Mandatory safety checks are not user-disableable.

## Full audit table

Paths in this table are relative to `app/`. `P` abbreviates
`models/preset.py:PresetSettings`; `E` is `services/estimation.py`; `Policy` is
`services/policy.py`; `Queue` is `services/queue.py`. All preset fields are
validated by Pydantic (enums/ranges/cross-field checks), with additional support
checks in `services/presets.py`. API-only nested controls are included.

| Setting | Defined in | Source/precedence | Persisted? | Consumed by | Apply timing | Status |
|---|---|---|---|---|---|---|
| `app_name` | `config.py` | Startup precedence | Env/config only | FastAPI title, health response, HTML title/brand | Restart | RESTART REQUIRED |
| `development_mode` | `config.py` | Startup precedence | Env/config only | Nothing; deprecated compatibility flag | No effect; use `media_backend` | UNUSED |
| `media_backend` | `config.py` | Startup precedence; injected test repository can override | Env/config only | `container.py`, lifespan worker selection, facade scan/queue guards | Restart | RESTART REQUIRED |
| `movies_root`, `shows_root` | `config.py` | Startup defaults overridden by saved paths | Env/config only | Preferences defaults, root validation | Restart to change unsaved defaults | RESTART REQUIRED |
| `movies_path`, `shows_path` | `models/preferences.py`, Settings form | Saved object > root defaults, including empty values | SQLite metadata | `configured_roots`, scanner, filesystem projection | Query visibility immediately; next scan | WIRED |
| `database_path` | `config.py` | Explicit app/factory override > startup config | Env/config only | Shared Database; root exclusion validation | Restart | RESTART REQUIRED |
| `ffprobe_binary` | `config.py` | Startup precedence; nonempty string | Env/config only | `FFprobeService.inspect` subprocess argument 0 and runtime diagnostics | Restart | RESTART REQUIRED |
| `ffmpeg_binary` | `config.py` | Startup precedence; nonempty string | Env/config only | Startup `-encoders` capability check and real `FFmpegEncoder` subprocess | Restart | RESTART REQUIRED |
| `workspace_root` | `config.py` | Startup precedence; absolute path | Env/config only | Runtime writable/overlap check and per-job output paths | Restart | RESTART REQUIRED |
| `qsv_device` | `config.py` | Startup precedence; absolute path; default `/dev/dri/renderD128` | Env/config only | GPU render-node access check, HEVC smoke test and ffmpeg device selection | Restart | RESTART REQUIRED |
| `timezone` | `config.py` -> persisted `WorkerSettings` default | Saved worker setting > startup default | SQLite after first save | Quiet-hours wall-clock evaluation for CPU/GPU | Immediate after save | WIRED |
| `ffprobe_timeout` | `config.py` | Startup precedence; >0 and ≤600 seconds | Env/config only | `subprocess.run(timeout=...)` | Restart | RESTART REQUIRED |
| `seed_media_path` | `config.py` | Startup precedence | Env/config only | `SeedMediaRepository` in seed mode | Restart for path; fixture content read on requests | RESTART REQUIRED |
| `seed_presets_path` | `config.py` | Startup precedence; existing SQLite preset edits take precedence after catalogue migrations | Env/config only; resulting presets in SQLite | Seed loader and catalogue migration | Startup/initialization, not a live override | RESTART REQUIRED |
| `start_workers` / `start_real_worker` (factory arguments, not UI/env) | `main.py` | Explicit arguments | No | Seed lifespan fake tick; opt-in embedded real worker exists for tests only. Production real workers use standalone services | App construction | WIRED |
| Preset `name` | P / editor | Persisted edit > seed | Yes | Lists, modal, queued snapshot | Immediately; new jobs | WIRED |
| Preset `scope` | P / editor | Persisted edit > seed | Yes | Scoped choices and Policy mismatch block | Immediately; new jobs | WIRED |
| Preset `intent` | P / editor | Persisted edit > seed; changing UI intent fills defaults | Yes | UI defaults; model planning defaults when bounds omitted; snapshot | New preset/edit; does not overwrite supplied bounds | WIRED |
| Preset `origin` | P; service-owned | Creation/duplicate forces custom; update retains existing origin | Yes | Delete restrictions, built-in HDR validation, catalogue migrations | Immediately | WIRED |
| Preset `enabled` | P / editor/toggle | Persisted edit > seed | Yes | Preview/modal filtering, Policy, supported-codec validation | Immediately; existing job snapshot unchanged | WIRED |
| Preset `backend` | P / editor | Persisted edit > seed | Yes | Lane selection, Quality CPU tag, CRF/ICQ validation, warnings | New jobs | WIRED |
| Preset `destination_codec` | P / editor | Persisted edit > seed | Yes | Validation, eligibility, snapshot; real worker accepts HEVC and rejects AV1 | New jobs | PARTIALLY WIRED |
| `rate_control` | P / editor | Persisted edit > seed | Yes | CPU consumes CRF/ABR; GPU consumes QVBR/legacy ICQ/ABR | Eligibility/new jobs | WIRED for current HEVC lanes |
| `target_video_bitrate` | P / editor | ABR requires positive value; quality modes require null | Yes | E estimate/floor; CPU/GPU ABR pass bitrate to the selected HEVC encoder | Eligibility/new jobs | WIRED for ABR |
| `quality_value` | P / editor | Persisted edit; ICQ integer practical range 18–30 in the UI | Yes | CPU CRF is passed to libx265; GPU QVBR/ICQ quality is passed as `global_quality` | New job plan | WIRED |
| `encoder_preset` | P / editor (hidden as x265 effort for GPU) | Persisted edit > seed | Yes | Passed only to libx265; retained but ignored for GPU presets (VA-API has no x265 effort preset) | New job plan | WIRED |
| `output_bit_depth` | P / editor | 8/10; HDR10 requires 10 | Yes | Selects CPU/GPU output pixel format/profile; ffprobe validation requires the requested output bit depth | New job plan/output validation | WIRED |
| `source_resolutions` | P / API, retained by UI edits | Explicit values > all supported defaults | Yes | Policy source applicability; enabled empty list rejected | Eligibility/new jobs | WIRED |
| `target_resolution` (legacy `resolution_policy` accepted) | P / editor | Explicit target > migrated legacy value > keep | Yes | Inherited resolution-floor check; real worker accepts `keep` and refuses scaling | Eligibility/new job plan | PARTIALLY WIRED |
| `planning_video_bitrate_low`, `planning_video_bitrate_high` | P / API; hidden editor state | Explicit bounds > scope/intent defaults | Yes | E quality-mode ranges and queue savings order | Eligibility/new jobs | WIRED |
| `hdr_support` | P / editor | Persisted edit > seed | Yes | SDR-only input block, HDR10 validation/warnings | Eligibility/new jobs | WIRED |
| `hdr_policy` | P / editor | Persisted edit > preserve source default | Yes | Policy blocks tone mapping; model cross-field checks | Eligibility/new job plan | PARTIALLY WIRED |
| `preserve_hdr_metadata` | P / editor | Persisted edit > default true | Yes | HDR policy/snapshot; current real worker refuses HDR inputs | New job plan | PARTIALLY WIRED |
| `hdr_metadata.validate_signalling` | `HDRMetadataPolicy` / API, retained by editor | Persisted nested value > true | Yes | Model tone-map consistency, snapshot; future validator | New job plan | PARTIALLY WIRED |
| `hdr_metadata.preserve_color_primaries` | Same | Same | Yes | Snapshot; future encoder/validator | New job plan | PARTIALLY WIRED |
| `hdr_metadata.preserve_transfer_characteristics` | Same | Same | Yes | Snapshot; future encoder/validator | New job plan | PARTIALLY WIRED |
| `hdr_metadata.preserve_matrix_coefficients` | Same | Same | Yes | Snapshot; future encoder/validator | New job plan | PARTIALLY WIRED |
| `hdr_metadata.preserve_mastering_display_metadata` | Same | Same | Yes | Snapshot; future encoder/validator | New job plan | PARTIALLY WIRED |
| `hdr_metadata.preserve_max_cll`, `preserve_max_fall` | Same | Same | Yes | Snapshot; future encoder/validator | New job plan | PARTIALLY WIRED |
| `audio_policy`, `audio_conversion_policy` | P / editor | Persisted edit > seed | Yes | Effective preserve decision, E audio plan; at least one efficient policy permits conversion | Eligibility/new jobs | WIRED |
| `preserve_audio_by_default` | P / API, editor default policy mapping | Retained for compatibility | Yes | Nothing at runtime; audio is preserved unless the job explicitly sends `preserve_audio: false` | — | COMPATIBILITY ONLY |
| `stereo_audio_bitrate`, `target_audio_bitrate` | P / editor | Persisted edit > defaults; efficient audio requires target | Yes | E plan; real GPU lane applies AAC/E-AC3 conversion where the plan says encode | Eligibility/new jobs/output validation | WIRED for GPU efficient audio |
| `efficient_audio_rules.mono_stereo_codec`, `multichannel_codec` | `EfficientAudioRules` / API | Fixed validated AAC/E-AC3 values | Yes | E output audio plan | New job plan | WIRED |
| `efficient_audio_rules.channel_handling` | Same | Explicit preserve/downmix > preserve default; legacy preserve_channels migrated | Yes | E channels, codec and bitrate planning; Preserve Audio overrides downmix | Eligibility/new jobs | WIRED |
| `efficient_audio_rules.copy_codecs` | Same | Explicit list > AAC/E-AC3/AC3 | Yes | E conditional copying | Eligibility/new jobs | WIRED |
| `efficient_audio_rules.copy_if_bitrate_at_or_below_target` | Same | Explicit bool > true | Yes | E conditional copying | Eligibility/new jobs | WIRED |
| `efficient_audio_rules.copy_unknown_bitrate` | Same | Explicit bool > true | Yes | E unknown-bitrate handling | Eligibility/new jobs | WIRED |
| `efficient_audio_rules.copy_channels_above` | Same | Explicit 2–16 or null > null | Yes | E conditional copying | Eligibility/new jobs | WIRED |
| `minimum_source_bitrate` | P / editor | Persisted edit > default 0 | Yes | Policy source floor | Eligibility/new jobs | WIRED |
| `minimum_expected_saving_percent` | P / editor | Persisted edit > default 20 | Yes | ABR eligibility block, quality-mode planning warning; the measured saving must meet it before a replace job swaps the source (otherwise skipped, source kept) | Eligibility/new job plan/replacement | WIRED |
| `allow_hevc_reencode` | P / editor | Persisted edit > false | Yes | Policy HEVC recompression block | Eligibility/new jobs | WIRED |
| Job `preserve_audio` | `models/queue.py`, compression modal | Explicit `false` opts out; `null`/omitted and the modal's default tick preserve; Preserve Audio tag and preserve-only presets always preserve | Job snapshot | Policy/E and the shared `plan_audio_tracks`; CPU requires copy plans, GPU supports copy or Efficient Audio plans; copied tracks verified by packet SHA-256 | New job | WIRED |
| Job `preserve_subtitles` (chapters/attachments/metadata) | Same | Explicit bool > true | Job snapshot | Real explicit stream/chapter mapping and validation counts; opting out drops subtitles/attachments/chapters and global tags only (`-map_metadata:g -1`), never audio languages/titles | New job | WIRED for supported Matroska streams |
| Job `replace_source` / keep output | Same; modal Output handling | Modal default true (replace); API default false (keep) | Job snapshot | Replace: MKV source, writable directory, measured saving ≥ preset minimum, journalled verified swap with backup; keep: validated MKV under workspace | New job | WIRED |
| Queue priority / Move next | Queue models and UI | Explicit priority/order > normal, then estimated/planning bytes saved | Yes | Queue ordering across independent seed lanes and real CPU/GPU lanes | Immediately for queued jobs | WIRED |
| Remove queued / Stop & Skip | Queue UI/API | Explicit action; active deletion prohibited | Yes | Seed fake lifecycle or owned ffmpeg process lifecycle | Immediately | WIRED |
| Tags Preserve A/V, Preserve Video, Preserve Audio, Quality CPU | `models/tags.py`, tag dialog | Explicit assignment > seed direct tags; inherited union remains | Yes, semantic identity | TagService, Policy, queued-job revalidation | Immediately | WIRED |
| Quality Floor minimum bitrate / minimum height | `models/tags.py`, tag dialog | Maximum inherited/direct floor; values required with tag | Yes | Policy output height and ABR bitrate checks; CRF/ICQ/QVBR blocked under bitrate floor | Immediately | WIRED |
| Tag bulk add/remove/replace | `TagUpdate` / API and dialog | Explicit operation; bulk replacement prohibited | Resulting assignments | Atomic TagService update | Immediately | WIRED |
| Worker manual pause | `WorkerLaneSettings.paused`, Queue UI/API | Persistent per-lane control | SQLite metadata | Blocks new claims; active job normally finishes | Immediate | WIRED |
| Quiet-hours enabled/start/end/cutoff | `WorkerLaneSettings`, Settings form | Persistent per-lane schedule | SQLite metadata | Standalone worker claim gate and quiet-start action | Immediate; evaluated in saved timezone | WIRED |

`WIRED` for planning fields means the planning/policy consumer runs. Real
filesystem jobs are accepted only when the selected lane's startup diagnostics
and per-item execution guards pass. CPU consumes CRF/ABR plus libx265 settings;
GPU consumes QVBR/legacy ICQ/ABR, VA-API profile/bit depth and optional Efficient
Audio rules. Both enforce stream preservation, keep-resolution and bit-exact copied
audio, and replace only MKV sources in writable directories; unsupported combinations
are excluded with a reason.

## Absent settings and fixed behavior

No editable workspace path, CPU/GPU worker counts, CPU budget, global minimum
saving, scan interval, automatic scan, configurable extension list or
reconciliation tuning exists. Workspace and binary paths come from environment
settings and require restart. Seed mode has one CPU and one GPU fake lane; its
one-second tick and simulated stage durations are constants. Filesystem mode has
one standalone real CPU worker and one standalone real GPU worker when their
respective prerequisites are available. Scans are explicit; no watcher/startup scan.
Extension support and 48-packet ffprobe inspection are adapter constants. Polling
is 1.5 seconds (5 seconds in a hidden browser tab).

Hardlink, DV, unsupported/uncertain HDR, interlaced and UHD Movie REMUX protections
are mandatory Policy/SourceGuard behavior. There are no safety toggles. The
hardcoded restrictions do not conflict with an editable setting. Tone mapping is
shown as unavailable; the existing model also rejects incompatible HDR
combinations, and Policy blocks any tone-map preset reaching eligibility.

## Corrections made during the audit

- Startup validates effective saved roots, not superseded environment roots.
  Root saves validate overlap and application-database exclusion in both backends.
- Health and page branding now read the app's actual configured name, including
  explicitly injected Settings in tests.
- UI edits preserve API-only HDR facts, empty disabled applicability lists,
  explicitly configured audio defaults and nonstandard permitted audio bitrates.
  Intent defaults now set checkboxes via `checked`, not `value`.
- Preserve Audio keeps original channel counts even under a custom downmix rule;
  actual stereo plans use the stereo codec/bitrate.
- `development_mode` is explicitly deprecated as unused, rather than implying a
  runtime switch. `media_backend` is the sole adapter selector.
- Startup-only fields and next-scan/new-job timing are exposed honestly; output
  behavior is no longer described as already implemented.

## Validation trace

`test_settings_runtime.py` checks both saved roots changing scan inputs, saved
roots surviving restart and overriding conflicting environment defaults, injected
app name, runtime timing reporting, configured ffprobe executable/timeout reaching
`subprocess.run`, and audio preservation/downmix planning. Existing preferences,
streaming preset, policy, persistence and queue suites cover CRUD, disabled/scope
restrictions, HDR, HEVC, floor inheritance, tags, snapshots and output defaults.
Frontend live updates preserve selection, filters and open metadata details and
pause row replacement while a dialog is open. Browser automation was not used.
