# Real encoding lanes

Filesystem mode can run one CPU/libx265 encode and one Intel GPU/hevc_vaapi encode
concurrently when each lane's prerequisites are available. Both require `ffmpeg`,
`ffprobe` and a writable dedicated workspace; CPU additionally requires libx265,
while GPU requires the configured render device and the hevc_vaapi encoder. Seed mode
remains deterministic simulation. No encode runs inside an HTTP request.

Configure the existing filesystem roots and output workspace for both processes.
Start the WebUI with `.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1`,
the CPU encoder with `.venv/bin/python -m app.worker_main cpu`, and, after the render
device is available, the GPU encoder with `.venv/bin/python -m app.worker_main qsv`.
The processes share the same SQLite database and environment configuration.
Restarting or stopping the WebUI leaves active ffmpeg processes alone; each worker
owns only its own lane. Example systemd units live in `deploy/systemd/`.

The workspace must already contain a writable `jobs/` directory. Settings shows the
resolved binary paths, whether `libx265` and `hevc_vaapi` are available, the configured GPU render
device, workspace writability, active encoder mode, supported backends, and lane
availability. Binary and
workspace changes require an application restart. Library roots cannot overlap
one another, the database, or the output workspace.

The supported slice remains conservative: HEVC, confirmed SDR, progressive video,
unchanged resolution and keep-output MKV. CPU/libx265 supports CRF/ABR and copied
audio. Intel GPU supports QVBR/legacy ICQ/ABR and can apply the existing Efficient Audio rules
(AAC for mono/stereo and E-AC3 for multichannel when a track is not copied). AV1,
HDR/unknown colour signalling (except untagged progressive 8-bit VC-1, see
[VA-API migration](VAAPI_MIGRATION.md#legacy-vc-1-without-colour-signalling)),
interlaced or unknown scan, source replacement and
presets requesting resolution changes are refused. The
existing policy engine still decides eligibility; execution capability adds these
runtime-specific refusals afterward. A `Preserve Audio` tag remains authoritative,
and `Preserve A/V` / `Preserve Video` still block work.

Each job writes only under
`<workspace>/jobs/<job-id>/<source-stem>.kompressor.partial.mkv`. FFmpeg receives
an argv list with explicit stream mapping, copy mode for non-video streams, and
machine-readable progress. The source is revalidated from its captured file ID,
revision, root/path, physical identity and stat evidence immediately before
starting FFmpeg. After FFmpeg succeeds, ffprobe validates codec, dimensions, progressive scan,
requested bit depth, SDR signalling, known source colour range, duration and
preserved stream/chapter counts. Only then is the partial promoted to
`<source-stem>.kompressor.mkv`. The source is never renamed, truncated,
replaced or deleted. Re-encoded video (and audio converted by Efficient Audio) drops
copied mkvmerge track statistics (`BPS`, `NUMBER_OF_BYTES`, `NUMBER_OF_FRAMES`,
`DURATION`, `_STATISTICS_*`, with or without a language suffix), which would
otherwise report the source bitrate and size. Copied streams keep theirs. Measured saving is recorded separately from the estimate.

Stop & Skip is persisted as a cancellation request in SQLite. The standalone owner
of the affected CPU or GPU lane observes that request, terminates only the subprocess
it owns, removes the partial output and records the job as skipped. Manual worker pause blocks new claims
but lets an active job finish. Quiet hours also block new claims; when the quiet
window begins, known progress at or below the configured cutoff is cancelled while jobs above it
(and jobs whose progress is unknown) finish before the worker goes idle.

Stopping the WebUI does not stop owned encoder processes because the WebUI owns none.
Stopping an encoder worker does stop only its own process; on the next startup that
worker recovers only its lane, requeues an interrupted ordinary encode from zero
and removes stale partial/final workspace output. An interrupted output is never treated as validated. Pending jobs
from the other runtime mode are blocked instead of being run by the wrong worker.
CPU and GPU execution remain independent; one lane never recovers or stops the other.


## VA-API GPU migration

The GPU lane now uses only `hevc_vaapi`. Persisted `qsv` lane keys, the worker CLI,
service filename and `KOMPRESSOR_QSV_DEVICE` remain compatible; UI labels say GPU.
User preset names and queued preset snapshots are unchanged. GPU `encoder_preset`
values remain stored for compatibility but are not emitted as x265 effort options.
See [VA-API migration and manual checks](VAAPI_MIGRATION.md) for command details.

The runtime check encodes ten Main10/QVBR-23 at 4 Mbps frames into a private temporary folder
under workspace/jobs, probes their codec/profile/pixel format/frame count, then
removes the folder. Failure disables only the GPU lane; there is no silent
rate-control fallback. The check does not run when common prerequisites fail.
Profiles now persist in schema 4. Known compatible profiles use hardware decode;
unknown or unsupported profiles use software decode plus hwupload. Cached probes
without profiles remain usable through software decode and are not forcibly rescanned.
Output validation additionally requires the preset's HEVC profile/pixel format,
stream mapping order and audio ordinals. An optional video ENCODER tag must name
hevc_vaapi. None of these checks measure perceptual quality.
