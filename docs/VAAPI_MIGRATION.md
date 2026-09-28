# Intel GPU lane: VA-API migration

The GPU lane executes `hevc_vaapi` exclusively. The UI calls it GPU or Intel GPU
(VA-API), including presets, queue/history, worker controls, status badges and
eligibility messages. CPU/libx265 behavior is unchanged.

Persisted backend `qsv`, worker argument `qsv`, `kompressor-worker-qsv.service`
and `KOMPRESSOR_QSV_DEVICE` deliberately retain their old spellings. They now mean
the Intel GPU lane using VA-API. Existing job snapshots, priorities, quiet hours,
pauses and user-owned preset names survive unchanged. No data reset is needed.
The runtime diagnostics field is now `hevc_vaapi_available`. History labels use
the generic GPU lane name so old outputs are not relabeled with a new encoder.

QVBR uses `-rc_mode:v:0 QVBR -b:v:0 N -global_quality:v:0 Q`;
ABR uses `-rc_mode:v:0 VBR -b:v:0 N`. Legacy ICQ preset snapshots still
produce ICQ arguments; they are not silently converted. GPU jobs do not emit `-preset`, CRF or low-power flags.
Their old encoder-effort property remains stored but is ignored. VA-API requires
hardware surfaces, so jobs either decode in hardware and use `scale_vaapi`, or
decode in software and use `format=...,hwupload`. These options follow the
[FFmpeg VA-API encoder documentation](https://ffmpeg.org/ffmpeg-codecs.html#VAAPI-encoders).

Codec/profile/pixel-format facts select the path in `app/workers/vaapi.py`.
Known supported H.264 8-bit, HEVC Main/Main10, MPEG-2, VC-1 Simple/Main and VP9
profiles may use hardware decode. Unknown profiles, H.264 Hi10P, VC-1 Advanced and
other codecs use software decode. This is a conservative selection, not a retry
after a hardware failure. VC-1 Advanced is excluded because the Intel UHD 730
render node returned `No support for codec vc1 profile 3` on real hardware; those
jobs decode in software, upload with `format=p010le,hwupload` (or `nv12` for 8-bit
presets) and still encode with hevc_vaapi. VC-1 Simple/Main hardware decode has
not been validated on this node.
Input acceleration targets only the primary stream's input index; filtering and
encoding target only output video 0, leaving mapped cover art copied.

Schema 4 adds a nullable stream `profile` column transactionally. Existing probes
with no profile still work with software decode; unchanged media is not forcibly
reprobed. New/changed media acquires profiles during ordinary probing.

## Runtime and remaining limits

Startup checks the render node and access, the encoder listing, and a ten-frame
Main10/QVBR-23 at 4 Mbps smoke encode. Its private temporary directory is under workspace/jobs
and is removed on success or failure. ffprobe must report HEVC, Main 10,
yuv420p10le and a positive decoded frame count. Common prerequisite failures skip
the encode. Missing hardware disables only the GPU lane and leaves queued GPU
jobs queued. CPU remains independently available.

The operator tested this render node manually: ICQ failed with “Driver does not
support ICQ RC mode (supported modes: CQP, CBR, VBR, QVBR).” The same Main10
upload path passed VBR and QVBR; QVBR reported quality 23 and encoded ten frames.
The application now checks QVBR directly, with no fallback. This gates the GPU
lane. The two built-in GPU Show presets again cover 480p, 576p, 720p, 1080p
and 2160p, with nominal video rates stored in each preset:

| Source | QVBR nominal bitrate |
| --- | ---: |
| 480p | 1 Mbps |
| 576p | 1.5 Mbps |
| 720p | 2.5 Mbps |
| 1080p | 4 Mbps |
| 2160p | 16 Mbps |

These are unvalidated starting points, not output-size promises. The 4 Mbps
startup smoke is a capability test, not a universal job target. The built-in
minimum source bitrate is now zero so it does not exclude older 576p shows;
minimum savings remain a planning warning until the real output is measured.
Untouched built-ins migrate from either ICQ or the earlier 1080p-only QVBR
configuration. Edited presets and queued job snapshots are preserved. Legacy
ICQ presets may still be edited, but this particular driver will reject them;
they must be deliberately changed to QVBR or VBR before use. QVBR's quality number
is not interchangeable with ICQ or CPU CRF. A ten-frame smoke does not establish
perceptual quality, actual size or throughput on a full episode; start with
keep-output and inspect real content before considering replacement. The seeded
Top Gear example is 1080i and remains blocked because deinterlacing is not
implemented; the restored 576p applicability helps progressive 576p sources.

### Untagged legacy VC-1 and 8-bit H.264

Old VC-1 sources and many H.264 WEBRips/remuxes carry no colour primaries,
transfer or matrix tags. Each codec has its own rule in `app/models/probe.py`,
combined by `assumed_sdr_colours()`, which library projection, the policy engine
and CPU/GPU execution capability use. Both rules share these requirements:
progressive, `yuv420p` and 8-bit; all three colour fields absent
(`unknown`/`unspecified` count as absent); and no mastering-display,
content-light, Dolby Vision or HDR10+ side data seen.

- **VC-1** (any profile): the codec has no PQ/HLG or HDR metadata carriage, so
  absence cannot hide HDR.
- **H.264** Baseline, Constrained Baseline, Main or High only. H.264 can carry
  HDR, but PQ/HLG in practice needs High 10 or above; High 10/4:2:2/4:4:4 and
  unknown profiles stay blocked. Accepted residual risk: 8-bit HLG signalled only
  by an alternative-transfer SEI with no VUI colour description is not detected.

HEVC, AV1, MPEG-2 and every other codec with absent colour signalling remain
unknown and blocked; each would need its own explicit decision.

For these sources the job writes the assumed SDR tags explicitly on output:
BT.709 for HD (wider than 1024 or taller than 576), BT.470BG primaries/matrix with
SMPTE 170M transfer for 576-line SD, and SMPTE 170M for 480-line SD. The values are
stamped on the frames with `setparams` (after `format=...`, before `hwupload`, or
after `scale_vaapi`; for CPU, `format=...,setparams=...`) and also passed as
`-color_*` output options. The first real VC-1 job showed why. With only the output
options on ffmpeg 7.1, primaries and transfer came out untagged (the encoder takes
them from the frames) and validation failed. Worse, `-colorspace bt709` is
negotiated inside the filtergraph, so ffmpeg auto-inserted a swscale matrix
conversion from the untagged (treated as BT.601) frames via 16-bit RGB. That
shifted colours and cost about 3x wall time and 10x CPU on a 1080p sample. With
`setparams` the frames already match, swscale changes only the bit depth and no
colour conversion is applied. Output validation is unchanged and
still requires the HEVC output to validate as confirmed SDR.

Source guards, hardlink restrictions, confirmed-SDR/progressive-only eligibility,
source-resolution preservation and keep-output remain in force. GPU validation
checks the requested pixel format/profile, stream kinds/order, audio codecs and
channels by output ordinal, and known stream languages/titles when metadata is
preserved. If present, the primary video's ENCODER tag must contain hevc_vaapi.
Output still stays in the dedicated workspace; no source is replaced.

## Local verification

- Full suite: `326 passed, 1 skipped, 2 warnings in 58.51s`.
- Pyright in workspace standard mode: 0 errors, 0 warnings.
- All frontend JavaScript modules passed Node's syntax check.
- `git diff --check` passed.
- Hardware subprocesses in the migration tests are mocked. The optional real
  ffprobe test is skipped when ffprobe is not installed. The two warnings are
  existing Starlette/httpx and AnyIO deprecations.

## Manual CT checks — for the operator

Run these in CT 110 as the same user as the services. These are instructions only;
they have not been executed by the coding agent. Stop encoder services before
updating deployed code so no old executable is encoding during the change.
The examples assume `/root/kompressor`, `/mnt/kompressor/jobs` already exists,
standard binary locations and the service environment from the repository.

### 1. Device and encoder

```sh
cd /root/kompressor
ls -l /dev/dri/renderD128
test -c /dev/dri/renderD128 && test -r /dev/dri/renderD128 && test -w /dev/dri/renderD128
ffmpeg -hide_banner -encoders 2>&1 | grep -w hevc_vaapi
```

Expect a character device, successful tests (exit 0), and an encoder listing
containing hevc_vaapi. No `vainfo`, oneVPL or libmfx tooling is required.

### 2. Exact application smoke check

```sh
.venv/bin/python - <<'PY'
from pathlib import Path
from app.workers.ffmpeg import FFmpegEncoder
ready, reason = FFmpegEncoder.vaapi_runtime_check(
    '/usr/bin/ffmpeg', Path('/dev/dri/renderD128'), '/usr/bin/ffprobe', Path('/mnt/kompressor'))
print('GPU ready:', ready, 'reason:', reason)
raise SystemExit(0 if ready else 1)
PY
```

Expect `GPU ready: True reason: None`. Failure reports the device/encoder issue or
`GPU VA-API Main10 QVBR smoke test failed: ...`. A failure leaves the GPU lane unavailable. The temporary smoke output is automatically removed.

If the service reports only `-22 (Invalid argument)`, run the same video path
with verbose FFmpeg logging. Its first error line often names the rejected option.
These commands encode into the null muxer and do not write to media roots:

```sh
ffmpeg -hide_banner -nostdin -loglevel verbose \
  -init_hw_device vaapi=va:/dev/dri/renderD128 -filter_hw_device va \
  -f lavfi -i 'testsrc2=size=320x240:rate=30' -frames:v 10 -an \
  -filter:v:0 'format=p010le,hwupload' -c:v:0 hevc_vaapi \
  -profile:v:0 main10 -rc_mode:v:0 QVBR -b:v:0 4000000 -global_quality:v:0 23 \
  -f null - 2>&1
```

If the first command fails without naming the cause, use the same Main10/upload
path with VBR to separate a rate-control rejection from a 10-bit or upload issue:

```sh
ffmpeg -hide_banner -nostdin -loglevel verbose \
  -init_hw_device vaapi=va:/dev/dri/renderD128 -filter_hw_device va \
  -f lavfi -i 'testsrc2=size=320x240:rate=30' -frames:v 10 -an \
  -filter:v:0 'format=p010le,hwupload' -c:v:0 hevc_vaapi \
  -profile:v:0 main10 -rc_mode:v:0 VBR -b:v:0 4000000 \
  -f null - 2>&1
```

The earlier VBR/ICQ comparison established the driver limitation. The commands
below use the verified QVBR syntax; they remain operator-run CT checks.

### 3. Sixty seconds with hardware decode

Choose a confirmed SDR progressive episode with H.264 8-bit or HEVC Main/Main10.
The following manual examples assume its primary video is input video 0.

```sh
SRC='/media/shows/Series/Season 01/Series.S01E01.mkv'
CHECK_DIR=$(mktemp -d /mnt/kompressor/jobs/vaapi-manual.XXXXXX)
ffprobe -v error -select_streams v:0 \
  -show_entries stream=codec_name,profile,pix_fmt,field_order,color_transfer,color_primaries,color_space \
  -of json "$SRC"
ffmpeg -hide_banner -nostdin -n \
  -init_hw_device vaapi=va:/dev/dri/renderD128 -filter_hw_device va \
  -hwaccel:v:0 vaapi -hwaccel_device:v:0 va -hwaccel_output_format:v:0 vaapi \
  -i "$SRC" -t 60 -map 0:v:0 -an \
  -filter:v:0 'scale_vaapi=format=p010' -c:v:0 hevc_vaapi \
  -profile:v:0 main10 -rc_mode:v:0 QVBR -b:v:0 4000000 -global_quality:v:0 23 \
  "$CHECK_DIR/hardware.mkv"
```

Expect successful encoding of about 60 seconds, with hevc_vaapi in the mapping.
These manual video-only samples isolate the video pipeline; a queued application
job also maps audio, subtitles, chapters and attachments. For explicit colour
flags, use the actual known source values, as the application does.

### 4. The same source with software decode and GPU encode

```sh
ffmpeg -hide_banner -nostdin -n \
  -init_hw_device vaapi=va:/dev/dri/renderD128 -filter_hw_device va \
  -i "$SRC" -t 60 -map 0:v:0 -an \
  -filter:v:0 'format=p010le,hwupload' -c:v:0 hevc_vaapi \
  -profile:v:0 main10 -rc_mode:v:0 QVBR -b:v:0 4000000 -global_quality:v:0 23 \
  "$CHECK_DIR/software-decode.mkv"
ffprobe -v error -select_streams v:0 -count_frames \
  -show_entries stream=codec_name,profile,pix_fmt,nb_read_frames:stream_tags=ENCODER \
  -of json "$CHECK_DIR/software-decode.mkv"
```

Expect HEVC / Main 10 / yuv420p10le, a positive frame count and a hevc_vaapi encoder
tag if the muxer exposes one. GPU encoding remains active on both decode paths.

### 5. Services and WebUI

After deploying this branch and the updated service file:

```sh
systemctl daemon-reload
systemctl restart kompressor-web.service
systemctl start kompressor-worker-qsv.service
systemctl status kompressor-worker-qsv.service --no-pager
journalctl -u kompressor-worker-qsv.service -n 50 --no-pager
curl -fsS http://127.0.0.1:8000/api/settings/runtime
curl -fsS http://127.0.0.1:8000/api/queue
```

Expect an active worker, `hevc_vaapi_available: true`, `qsv_available: true` and
`qsv` in `supported_backends`. Settings and Queue should show GPU as available.
The CLI exits 2 when unavailable; the supplied systemd unit excludes that exit
from automatic restarts. Settings availability reflects startup checks; it is
not a worker heartbeat. Start only one worker per lane.

### 6. One complete Show job

In Shows, select an eligible SDR progressive episode, choose a GPU Show preset,
and keep the original. Add it to Queue. Expect real GPU progress, validation,
then Completed with measured output size/saving and a workspace output path.
The source remains untouched. Check the exact output path shown in History:

```sh
OUT='/mnt/kompressor/jobs/JOB-ID/EPISODE.kompressor.mkv'
ffprobe -v error -show_streams -show_chapters -show_format -of json "$OUT"
ffprobe -v error -select_streams v:0 -count_frames \
  -show_entries stream=codec_name,profile,pix_fmt,nb_read_frames:stream_tags=ENCODER \
  -of json "$OUT"
```

Expect the preset's HEVC Main/Main10 and 8/10-bit output, matching duration,
planned stream counts/order, preserved languages and expected copied or converted
audio. Frame counting reads the full video and may take time. A failed validation
must leave the job failed, without promoting an output as completed.

### 7. Visual comparison at the same timestamp

Use a CPU baseline of the same source with no start offset, just like the GPU
samples. For the 60-second samples above use a timestamp below 60 seconds:

```sh
CPU_OUT='/mnt/kompressor/jobs/CPU-JOB/EPISODE.kompressor.mkv'
STAMP='00:00:30.000'
ffmpeg -hide_banner -nostdin -n -i "$SRC" -ss "$STAMP" -map 0:v:0 -frames:v 1 "$CHECK_DIR/source.png"
ffmpeg -hide_banner -nostdin -n -i "$CPU_OUT" -ss "$STAMP" -map 0:v:0 -frames:v 1 "$CHECK_DIR/cpu.png"
ffmpeg -hide_banner -nostdin -n -i "$CHECK_DIR/hardware.mkv" -ss "$STAMP" -map 0:v:0 -frames:v 1 "$CHECK_DIR/gpu.png"
```

Compare fine texture/grain, faces and dark regions at original size, plus moving
scenes during playback. Static frames alone cannot establish perceptual quality.
