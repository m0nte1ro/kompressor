# First real encoding slice

Filesystem mode can run one background CPU encode at a time when the configured
runtime has `ffmpeg`, `ffprobe`, `libx265`, and a writable dedicated workspace.
Seed mode remains deterministic simulation. No encode runs inside an HTTP request.

Configure the existing filesystem roots and output workspace for both processes.
Start the WebUI with `.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1`
and the encoder with `.venv/bin/python -m app.worker_main cpu`. The two processes
share the same SQLite database and environment configuration. Restarting or stopping
the WebUI therefore leaves an active ffmpeg process alone; only the CPU worker owns
that subprocess. Example systemd units live in `deploy/systemd/`.

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

Stop & Skip is persisted as a cancellation request in SQLite. The standalone CPU
worker observes that request, terminates only the subprocess it owns, removes the
partial output and records the job as skipped. Manual worker pause blocks new claims
but lets an active job finish. Quiet hours also block new claims; when the quiet
window begins, known progress below the configured cutoff is cancelled while jobs
at/above it (and jobs whose progress is unknown) finish before the worker goes idle.

Stopping the WebUI does not stop owned encoder processes because the WebUI owns none.
Stopping the CPU worker does stop its own process; on the next worker startup recovery
requeues an interrupted ordinary encode from zero and removes stale partial/final
workspace output. An interrupted output is never treated as validated. Pending jobs
from the other runtime mode are blocked instead of being run by the wrong worker.
QSV execution remains unsupported.
