# Default preset catalogue

Kompressor ships with five intentionally simple enabled built-in presets. They
describe user intent, not resolution-specific variants. Every default preserves
the source resolution: 480p stays 480p, 720p stays 720p, 1080p stays 1080p and
2160p stays 2160p.

The defaults are experimental starting points. Preserve Quality means visually
transparent or extremely difficult to distinguish during normal viewing, not
mathematical or bit-perfect preservation. HEVC/x265 transcoding is lossy in this
workflow.

| Preset | Scope | Video | Audio default | Resolution |
| --- | --- | --- | --- | --- |
| Movie Preserve Quality | Movie | CPU/x265 HEVC, experimental CRF | Preserve | Preserve source |
| Movie Streaming Quality | Movie | CPU/x265 HEVC, experimental CRF | Preserve by default; efficient fallback | Preserve source |
| Show Preserve Quality | Show | CPU/x265 HEVC, experimental CRF | Preserve | Preserve source |
| Show Streaming Quality | Show | Intel QSV HEVC, experimental ICQ | Preserve by default | Preserve source |
| Show Streaming + Efficient Audio | Show | Same video policy as Show Streaming Quality | Efficient rules by default | Preserve source |

The streaming presets aim for a premium-streaming-style visual philosophy and
significant storage reduction. They do not copy Netflix bitrate settings. CRF and
ICQ values are not equivalent, and the current QSV values have not been validated
on Intel UHD 730 hardware.

## Audio policy

Preserve Quality presets copy all audio tracks. Movie Streaming Quality and Show
Streaming Quality preserve audio by default, but an explicit per-job Preserve
Audio override can be disabled to use their efficient fallback. Show Streaming +
Efficient Audio selects that fallback by default.

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
The King of Comedy is eligible for Movie Streaming Quality when no other rule
blocks it. A 2160p UHD REMUX such as Dune: Part Two remains blocked by the movie
REMUX guard, and its Dolby Vision and Preserve A/V protections also remain in
force. Hardlinked/seeding media and interlaced media remain blocked.

HEVC re-encoding, source applicability, minimum source bitrate, HDR policy and
minimum worthwhile saving remain configurable on each preset. Defaults are
conservative; specialized behavior such as 4K to 1080p is created by duplicating
a preset and changing its resolution policy.

## Custom presets and migration

Built-ins have `origin = built_in`. Users can create, duplicate, edit,
enable/disable and delete custom presets. Duplicating Show Streaming Quality
creates an independent custom preset such as `Show Streaming Quality (Copy)`
with a new ID. Changing its resolution policy to `max_1080p` does not change the
built-in default.

Catalogue migration is idempotent. It inserts missing current built-ins, disables
untouched obsolete built-ins, preserves edited/custom presets, and leaves queued
preset snapshots unchanged.

## Output handling

Output handling is a job option, separate from presets. Development defaults to
keeping the source untouched and retaining a test output in the Kompressor
workspace. Replacement intent can be recorded, but this project does not yet
implement real ffmpeg, filesystem replacement, or media scanning.

Real-media validation is still required for CRF values, QSV ICQ behavior, speed
settings, HDR handling, player compatibility, frame and grain sensitivity,
audio codec support, and actual output-size savings.