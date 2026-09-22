# Streaming-style presets: short report

The goal is a convincing commercial-stream appearance with substantial storage
reduction, accepting some loss of remux fidelity. These are experimental starting
points, not Netflix recipes or validated visual-quality guarantees. Netflix's
[per-title encoding approach](https://netflixtechblog.com/per-title-encode-optimization-7e99442b62a2)
supports adapting bitrate to content complexity rather than giving every title
the same bitrate.

## Defaults

All primary presets use HEVC, CPU/x265, `slow`, 10-bit output, original resolution
and explicit source-resolution applicability. Higher CRF means more compression;
these values are engineering starting points for visual testing.

| Source | Movie CRF | Show CRF | Video planning range, Mbps |
|---|---:|---:|---:|
| 480p | 20 | 21 | 0.5–1.5 |
| 720p | 21 | 22 | 1–3 |
| 1080p | 22 | 23 | 2–6 |
| 2160p SDR | 23 | 24 | 6–16 |

Two optional show profiles offer QSV ICQ 23 at 720p and 1080p. ICQ and CRF values
are not equivalent; hardware support and quality require testing on the target
iGPU. See [x265 rate control](https://x265.readthedocs.io/en/stable/cli.html) and
[FFmpeg QSV modes](https://ffmpeg.org/ffmpeg-codecs.html#QSV-Encoders).

Planning ranges are configurable assumptions, **not bitrate caps or predicted
output bounds**. The scheduler uses their midpoint for ordering. Actual output
may fall outside the range. Minimum saving for quality modes must be verified
after encoding, before any future source replacement.

## King of Comedy

Use **Movie Streaming 1080p**: CRF 22, slow, 10-bit HEVC. A 1080p H.264 Blu-ray
remux is eligible; automatic movie-remux protection applies to 2160p.

For the seed's 19.5 GB source, planning output is roughly **3.0–6.3 GB** with
audio preserved, or **1.8–5.1 GB** with efficient stereo audio. These are arithmetic
estimates, not an encode or an assessment of the film's grain. Judge dark scenes,
faces, motion and grain on your normal display before accepting a preset.

## Audio, safety and rollout

- Preserve Audio stays checked by default. Unchecking it enables the preset's
  per-track policy: AAC 192 kbps for mono/stereo, E-AC3 640 kbps up to 5.1, copying
  already-efficient low-bitrate tracks. Larger layouts and unknown bitrates are
  copied. All tracks/languages and channel counts are retained; no downmix.
- Default presets accept SDR only. HDR10 requires an explicit experimental
  preset; Dolby Vision, hardlinks, interlaced media and protected tags stay blocked.
  HEVC recompression is off by default.
- Quality Floor remains a bitrate/resolution constraint, not a quality score.
  CRF/ICQ cannot promise its bitrate floor and are blocked when that tag applies.
- **Keep original / separate test output** is the default job option. Replacement
  intent is recorded separately from the preset and shown in Queue/History.
  Workers still simulate: neither option creates, replaces or deletes files yet.
  Real test encodes and safe replacement remain future worker work.
- Upgrade adds these profiles and disables untouched legacy defaults. User edits
  and queued preset settings are retained. Legacy custom presets without explicit
  applicability need review before use. The upgrade is idempotent.

Validation includes 84 passing tests for policies, estimates, persistence and
migration. Actual visual quality, frame-rate sensitivity, player compatibility
and encoder/hardware behaviour still require real-media trials.
