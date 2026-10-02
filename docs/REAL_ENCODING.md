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
unchanged resolution and MKV output that either replaces an MKV source (see
[Source replacement](#source-replacement)) or is kept in the workspace. CPU/libx265
supports CRF/ABR and copied audio. Intel GPU supports QVBR/legacy ICQ/ABR and can
apply the existing Efficient Audio rules (AAC for mono/stereo and E-AC3 for
multichannel when a track is not copied). AV1, HDR/unknown colour signalling (except
untagged progressive 8-bit VC-1 and H.264, see
[VA-API migration](VAAPI_MIGRATION.md#untagged-legacy-vc-1-and-8-bit-h264)),
interlaced or unknown scan, replacing non-MKV sources and presets requesting
resolution changes are refused. The
existing policy engine still decides eligibility; execution capability adds these
runtime-specific refusals afterward. A `Preserve Audio` tag remains authoritative,
and `Preserve A/V` / `Preserve Video` still block work.

Each encode writes only under
`<workspace>/jobs/<job-id>/<source-stem>.kompressor.partial.mkv`. FFmpeg receives
an argv list with explicit stream mapping, copy mode for non-video streams, and
machine-readable progress. The source is revalidated from its captured file ID,
revision, root/path, physical identity and stat evidence immediately before
starting FFmpeg. After FFmpeg succeeds, ffprobe validates codec, dimensions, progressive scan,
requested bit depth, SDR signalling, known source colour range, duration,
preserved stream/chapter counts, stream order and every audio track (see
[Audio safety](#audio-safety)). Only then is the partial promoted to
`<source-stem>.kompressor.mkv`. A keep-original job ends there and never renames,
truncates, replaces or deletes the source. Re-encoded video (and audio converted by Efficient Audio) drops
copied mkvmerge track statistics (`BPS`, `NUMBER_OF_BYTES`, `NUMBER_OF_FRAMES`,
`DURATION`, `_STATISTICS_*`, with or without a language suffix), which would
otherwise report the source bitrate and size. Copied streams keep theirs. Measured saving is recorded separately from the estimate.

Stop & Skip is persisted as a cancellation request in SQLite. The standalone owner
of the affected CPU or GPU lane observes that request, terminates only the subprocess
it owns (the encode or the audio verification), removes the partial output and records
the job as skipped. A job in the `replacing` state cannot be stopped, blocked by a tag
change or cut off by quiet hours; the swap always runs to a verified replacement or
a restored original. Manual worker pause blocks new claims
but lets an active job finish. Quiet hours also block new claims; when the quiet
window begins, known progress at or below the configured cutoff is cancelled while jobs above it
(and jobs whose progress is unknown) finish before the worker goes idle.

Stopping the WebUI does not stop owned encoder processes because the WebUI owns none.
Stopping an encoder worker does stop only its own process; on the next startup that
worker recovers only its lane, requeues an interrupted ordinary encode from zero
and removes stale partial/final workspace output. An interrupted output is never treated as validated.
An interrupted replacement is never requeued; it is resolved first, as described
under [Source replacement](#source-replacement). Pending jobs
from the other runtime mode are blocked instead of being run by the wrong worker.
CPU and GPU execution remain independent; one lane never recovers or stops the other.


## Audio safety

Audio is never converted by default. A job preserves (stream-copies) every audio
track unless it explicitly sends `preserve_audio: false`, which in the WebUI means
unticking Preserve Audio. The preset field `preserve_audio_by_default` is retained
for compatibility but no longer changes that. A Preserve Audio tag, or a preset whose
audio and conversion policies are both preserve, copies audio even if the box is
unticked. Only the GPU lane can convert; the CPU lane refuses jobs that would.

One function, `plan_audio_tracks` in `app/services/estimation.py`, decides copy or
encode per track. The planning estimate, the ffmpeg command and output validation all
use it, so they cannot disagree about which tracks are copied. In the command every
stream is mapped explicitly under `-c copy`; only tracks planned as `encode` receive
`-c:a:N`/`-b:a:N`/`-ac:a:N` options.

Before a job completes or replaces anything, validation checks every audio track
in order: planned codec and channels, unchanged language and title, and for copied
tracks unchanged sample rate and channel layout. It then reads the source and the
output in full with `ffmpeg -map ... -c copy -f streamhash -hash sha256 -` and
requires each copied track's packet SHA-256 to match. Stream copy leaves packets
byte-identical, so a match proves the track is bit-for-bit the source's. Any
mismatch fails the job; nothing is promoted or replaced. The hash pass costs one
extra read of the source and output (I/O only, no decoding) and runs for keep-original
jobs too.

Unticking "Preserve subtitles / chapters / attachments / metadata" drops subtitles,
attachments, chapters and global container tags only. It uses
`-map_metadata:g -1`: a bare `-map_metadata -1` would also have stripped every audio
track's language and title, which earlier builds did.

## Source replacement

A replace job (the WebUI default) runs the full encode and validation above, then:

1. **Saving check.** The measured saving must be positive and at least the preset's
   minimum expected saving. Otherwise the source is kept, the output discarded and
   the job recorded as skipped with the measured percentage.
2. **Staging.** The job enters `replacing` and stops being stoppable. It needs an
   `.mkv` source in a writable directory (not a read-only mount) with room for the
   output plus 256 MiB. The validated output is copied next to the source as
   `.<job-id>.kompressor-incoming` (a hidden name the scanner ignores), fsynced,
   given the source's mode, and re-read to confirm its SHA-256 matches the
   workspace output.
3. **Swap.** SourceGuard re-checks the source afresh (same revision, path, inode,
   size, mtime, exactly one hardlink; ctime is ignored so chmod/ACL maintenance
   does not block replacement). The original is renamed to
   `.<job-id>.kompressor-backup`, and the verified copy is renamed into the source path.
4. **Final validation.** The file now at the source path must be the staged copy
   (same inode and size) and pass the same ffprobe output validation. On failure the
   copy is removed and the backup renamed back: the original is restored.
5. **Cleanup.** Only after that is the replacement given the source's owner and
   group (best effort: where chown is not permitted, e.g. unprivileged LXC or a
   mergerfs pool, the worker log records the new owner and the job is not marked;
   access then follows the folder's permissions/ACLs), the backup deleted and the workspace
   output removed. History shows the job as completed with `Source replaced` and the
   measured saving.

Every step is journalled on the job (paths, the original's and the copy's device and
inode, and the phase) before it happens. If the worker is killed mid-replacement, its
next start resolves the job before any other recovery, using only identity checks:

| Found at startup | Result |
| --- | --- |
| Original at the source path, no backup | Staged copy removed, original untouched; the validated output is kept as a History result (Replace source retries the swap) |
| Source path empty, original at the backup | Backup renamed back, original restored; validated output kept as above |
| Staged copy at the source path, phase before `verified` | Rolled back to the original; job failed |
| Staged copy at the source path, phase `verified` | Backup removed; job completed |
| Staged copy at the source path, backup already gone | Job completed |
| Anything else (unknown file at the path, backup not the original) | Nothing touched; job failed with `MANUAL CHECK REQUIRED` and all paths |

A file is only deleted when its device and inode prove it is Kompressor's own
staged copy, or the backup of an original that is verifiably back in place.
Replacement also runs `before_replacement` directly before the swap, so a file that
becomes hardlinked (for example re-seeded) while encoding is not replaced.

After replacement the inventory still describes the old file until the next
**Scan library**. Queueing the same item again before that fails safely at the source
guard. After the rescan it shows HEVC and is blocked by "Preset does not allow HEVC
recompression" unless a preset allows it.

Limits: MKV sources only (the output is Matroska; changing a file's extension
would look like a missing file to Sonarr/Radarr). Replacement needs the media
mount read-write for the worker user. Extended attributes and ACLs are not copied.
If the worker is stopped during the copy, the original is untouched and the
validated output is kept in History instead of being discarded.
The systemd stop timeout (15 s) can interrupt a long copy; recovery handles it the
same way.

### Replacing a kept output from History

A completed keep-original job keeps its validated MKV under
`<workspace>/jobs/<job-id>/`. History shows **Replace source** for it. After a
confirmation, this queues a replace-only job on the same worker lane
(`POST /api/queue/{job_id}/replace-source`). The worker does not encode again. It:

- confirms the kept file is that job's own workspace output;
- re-runs policy, capability and output validation, including the bit-exact audio check;
- re-checks the source against the identity captured when the output was encoded
  (a source changed since then, e.g. upgraded by Sonarr, fails the job);
- runs the same journalled swap as above.

Because the choice is explicit and made after seeing the measured result, the
preset's minimum saving is not applied. The output must still be smaller than the
source, or the job is skipped and nothing changes. On success the kept output is
deleted and the original History row notes that its output was moved into the
source. On any failure the kept output stays in the workspace and can be retried.
An item with another queued or active job cannot be replaced from History.

Any finished History entry can be deleted (`DELETE /api/queue/history/{job_id}`).
A kept output still in the workspace is deleted with it; the source is never
touched. Deleting is refused while a replacement from that output is pending.
Measured savings are shown in green and outputs that grew in red.

## History quality tools

Completed keep-original encodes (output still in the workspace, source unchanged
since encoding) offer read-only quality tools in History. They run one at a time in
a background thread of the WebUI process under `nice -n 19`. They only read the source
and output, and write only below `<workspace>/compare/<name>-<job>/`. Progress and the
latest result show above the History table; results are saved on the job.

**Compare** takes six random frames between 5% and 95% of the source. Each is a PNG
with the source on the left and the output on the right, at the source's resolution
and the same timestamp:
`<workspace>/compare/<name>-<job>/compare-<UTC time>/01_00h12m03.417s_source-left_output-right.png`.
Untagged sources that the encoder assumed SDR get the same colour tags stamped before the
RGB conversion, so both halves use the same matrix. Look at textures, faces, dark
areas and gradients at 100%; still frames cannot show motion artefacts.

Frames are matched by position from each file's first video frame, not by
container time. A source whose container starts at -0.021 s (AAC priming) and its
HEVC output starting at 0 put "the same" `-ss` one frame apart. Each file's first
frame pts is read (`-copyts` + `showinfo`), and both are seeked with
`-seek_timestamp 1` to first frame + T − half a frame.

**Benchmark** asks for a duration (5–600 s) and scores that many seconds from the
middle of the file with libvmaf. The kept output is the distorted input and the
source is the reference, both widened to 10-bit 4:2:0 and frame-aligned as above.
Sources above 1080p use the `vmaf_4k_v0.6.1` model. History shows the mean, the
5th-percentile frame ("5% low") and the minimum. The per-frame JSON log is kept at
`<workspace>/compare/<name>-<job>/vmaf-<UTC time>/vmaf.json`. It needs an ffmpeg
built with libvmaf (Debian's is), and uses half the CPU cores at nice 19. On the
test layout above, container-time seeking scored a near-lossless encode 41 instead
of 99. A surprisingly low score next to good-looking screenshots therefore points to
misalignment (for example a variable-frame-rate source) rather than quality.

The same screenshots from a shell (same command builder; progress printed per frame):

```sh
.venv/bin/python -m app.tools.compare "$SRC" "$OUT" --count 6 --out /mnt/kompressor/compare/manual
```

### Manual CT checks for replacement

Run as the service user in CT 110. They leave progress visible, per the other CT checks.

```sh
# 1. The media mount must be writable for replace jobs (expect exit 0).
touch /media/shows/.kompressor-write-test && rm /media/shows/.kompressor-write-test

# 2. The copied-audio proof the worker uses, on a finished keep-original job with Preserve Audio ticked:
SRC='/media/shows/Series/Season 01/Series.S01E01.mkv'
OUT='/mnt/kompressor/jobs/JOB-ID/Series.S01E01.kompressor.mkv'
time ffmpeg -hide_banner -nostdin -v warning -stats -i "$SRC" -map 0:a -c copy -f streamhash -hash sha256 - > /tmp/src-audio.sha256
time ffmpeg -hide_banner -nostdin -v warning -stats -i "$OUT" -map 0:a -c copy -f streamhash -hash sha256 - > /tmp/out-audio.sha256
diff /tmp/src-audio.sha256 /tmp/out-audio.sha256 && echo "all audio tracks bit-identical"
```

For a first replace job, pick one episode you have a copy of, queue it with the
default Replace choice, and afterwards confirm in History that it shows
`Source replaced`, that no `.kompressor-incoming`/`.kompressor-backup` file remains
in its folder, and that `ffprobe -v error -show_streams -of compact "$SRC" | grep
codec_type=audio` lists the same audio tracks, languages and titles as before.

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
