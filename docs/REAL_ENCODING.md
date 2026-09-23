# First real encoding slice

Filesystem mode can run one background CPU encode at a time when the configured
runtime has `ffmpeg`, `ffprobe`, `libx265`, and a writable dedicated workspace.
Seed mode remains deterministic simulation. No encode runs inside an HTTP request.

Configure the existing filesystem roots and the output workspace before startup:

```sh
KOMPRESSOR_MEDIA_BACKEND=filesystem \
KOMPRESSOR_MOVIES_ROOT=/media/movies \
KOMPRESSOR_SHOWS_ROOT=/media/shows \
KOMPRESSOR_FFMPEG_BINARY=/usr/bin/ffmpeg \
KOMPRESSOR_FFPROBE_BINARY=/usr/bin/ffprobe \
KOMPRESSOR_WORKSPACE_ROOT=/mnt/kompressor \
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The workspace must already contain a writable `jobs/` directory. Settings shows the
resolved binary paths, whether `libx265` is present, workspace writability, active
encoder mode, supported backends, and any reason encoding is disabled. Binary and
workspace changes require an application restart. Library roots cannot overlap
one another, the database, or the output workspace.

The supported slice is deliberately small: CPU/libx265, HEVC, confirmed SDR,
progressive video, unchanged resolution, copied audio, and keep-output MKV. QSV,
AV1, HDR/unknown colour signalling, interlaced or unknown scan, audio conversion,
source replacement, and presets requesting resolution changes are refused. The
existing policy engine still decides eligibility; execution capability adds these
runtime-specific refusals afterward. A `Preserve Audio` tag remains authoritative,
and `Preserve A/V` / `Preserve Video` still block work.

Each job writes only under
`<workspace>/jobs/<job-id>/<source-stem>.kompressor.partial.mkv`. FFmpeg receives
an argv list with explicit stream mapping, copy mode for non-video streams, and
machine-readable progress. The source is revalidated from its captured file ID,
revision, root/path, physical identity and stat evidence immediately before
starting FFmpeg. After FFmpeg succeeds, ffprobe validates codec, dimensions, SDR
signalling, duration and preserved stream/chapter counts. Only then is the partial
promoted to `<source-stem>.kompressor.mkv`. The source is never renamed, truncated,
replaced or deleted. Measured saving is recorded separately from the estimate.

Stop & Skip terminates only the subprocess owned by that job ID, waits a bounded
period before killing it if necessary, removes the partial output, records the job
as skipped and wakes the next CPU job. Shutdown stops owned processes. If the app
restarts with an active real job in filesystem mode, recovery requeues it from zero
and removes stale partial/final output in that job directory; an interrupted output
is never treated as validated. Pending jobs from the other runtime mode are blocked
instead of being run by the wrong worker. QSV remains unsupported.
