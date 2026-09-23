# Default preset catalogue

Kompressor ships with five intentionally simple enabled built-in presets. They
describe user intent, not resolution-specific variants. Every default preserves
the source resolution: 480p stays 480p, 720p stays 720p, 1080p stays 1080p and
2160p stays 2160p.

The defaults are experimental starting points. Preserve Quality means visually
transparent or extremely difficult to distinguish during normal viewing, not
mathematical or bit-perfect preservation. HEVC/x265 transcoding is lossy in this
workflow.

| Preset                                      | Scope | Video                                                  | Audio default              | Target resolution |
| ------------------------------------------- | ----- | ------------------------------------------------------ | -------------------------- | ----------------- |
| Just convert to HEVC                        | Movie | CPU/x265 HEVC, experimental CRF                        | Preserve every track       | KEEP              |
| Tone it down a bit + HEVC                   | Movie | CPU/x265 HEVC, experimental CRF                        | Preserve every track       | KEEP              |
| Just convert to HEVC                        | Show  | CPU/x265 HEVC, experimental CRF                        | Preserve every track       | KEEP              |
| Tone it down a bit + HEVC                   | Show  | Intel QSV HEVC, experimental ICQ                       | Preserve every track       | KEEP              |
| Tone it down a bit + HEVC + Efficient Audio | Show  | Same video policy as the Show streaming-quality intent | Efficient rules by default | KEEP              |

The streaming presets aim for a premium-streaming-style visual philosophy and
significant storage reduction. They do not copy Netflix bitrate settings. CRF and
ICQ values are not equivalent, and the current QSV values have not been validated
on Intel UHD 730 hardware.

## Audio policy

Both Movie presets and the Show preserve-quality and Show streaming-quality
presets copy every audio track, language, codec, channel layout and lossless
audio stream untouched. Movie Preserve Audio is forced and locked in the job
modal. The Efficient Audio Show preset is the only built-in that enables
conversion by default. Other custom presets may opt into conversion explicitly.

The experimental efficient rules are deterministic:

- mono/stereo tracks use AAC at 192 kbps;
- multichannel tracks use E-AC3 at 640 kbps;
- already-efficient AAC, E-AC3 and AC-3 tracks at or below the target are copied;
- unknown-bitrate tracks are copied;
- all languages, tracks and channel layouts are retained where practical;
- channels are not downmixed by default.

A Preserve Audio tag always overrides conversion. Language or track removal is a
separate future policy.

## Planning estimates

CRF and ICQ presets expose low/high planning ranges. They do not return a fake
exact output size or exact savings prediction. The midpoint may be used only for
queue ordering and is labeled as a planning value. Actual size, visual quality,
compatibility and savings require a real encode and validation.

The ranges are not bitrate caps or guaranteed output bounds. A minimum saving
shortfall in a quality-mode estimate produces a warning; it is not treated as a
measured result.

## Safety and applicability

Movie and Show scopes are enforced by the backend. A 1080p Blu-ray REMUX such as
The King of Comedy is eligible for the Movie streaming-quality intent when no other rule
blocks it. A 2160p UHD REMUX such as Dune: Part Two remains blocked by the movie
REMUX guard, and its Dolby Vision and Preserve A/V protections also remain in
force. Hardlinked/seeding media and interlaced media remain blocked.

HEVC re-encoding, source applicability, minimum source bitrate, HDR policy and
minimum worthwhile saving remain configurable on each preset. Defaults are
conservative; specialized behavior such as 4K to 1080p is created by duplicating
a preset and changing its target resolution to `max_1080p`. Target resolution is
a maximum cap, so it never upscales a smaller source. The available choices are
`KEEP`, `2160p`, `1080p`, `720p`, `576p` and `480p`; every built-in uses `KEEP`.
Source applicability defaults to all supported resolutions and is kept out of the
normal preset editor and compression workflow.

## Custom presets and migration

Built-ins have `origin = built_in`. Users can create, duplicate, edit,
enable/disable and delete custom presets. Duplicating a Show streaming-quality
preset creates an independent custom preset with a user-chosen name
with a new ID. Changing its target resolution to `max_1080p` does not change the
built-in default.

Catalogue migration is idempotent. It inserts missing current built-ins, removes
known obsolete built-in catalogue rows, preserves edited/custom presets, and
leaves queued preset snapshots unchanged.

## Output handling

Output handling is a job option, separate from presets. Seed mode simulates the
selected intent without creating files. Filesystem mode's first real CPU slice
accepts keep-output only and writes validated MKV artifacts to the dedicated
workspace; it never replaces the source. See [real CPU encoding](REAL_ENCODING.md)
for its conservative SDR/progressive/HEVC limits.

Broader real-media validation is still required for perceptual CRF quality,
QSV ICQ behavior, speed settings, HDR handling, player compatibility, frame and
grain sensitivity, audio conversion, and output-size expectations.

The broader preset model keeps HDR10 intent separate from SDR intent; it does
not implicitly tone-map. The current real CPU worker accepts confirmed SDR only,
so HDR10 encode support remains unavailable. A future HDR path must preserve and
validate colour primaries, PQ transfer characteristics, matrix coefficients,
mastering display metadata, MaxCLL, MaxFALL and related signalling.
