# Portuguese-only audio cleanup (lossless remux)

This is a **standalone maintenance tool**, not a Queue/worker/WebUI feature.
It keeps one selected Portuguese audio track, makes it **default**, drops all
other audio tracks, and copies video bitstreams without transcoding. Subtitles,
attachments, chapters and standard metadata are mapped through where supported.

The tool uses ffprobe to enumerate tracks and refuses ambiguous or untagged
PT audio unless given an explicit `--audio-index`. It does not identify spoken
languages by listening to the audio. Valid language tags: `por`, `pt`,
`pt-PT` or `pt-BR`. The index comes from FFprobe's per-file stream list,
not an ordinal among audio tracks.

**Never run this against files while Kompressor jobs, Immich, Samba, or another
service may change them.** Stop the relevant worker and writers first. The
standalone tool does not reserve a Queue job or coordinate locks with it.
Only MKV files without hardlinks or symlinks are supported.

Inside CT 110, preview an episode:

```bash
python -m app.tools.audio_cleanup \
  '/media/cartoons/Phineas and Ferb/PhineasAndFerb.S01.E01.1080P.mkv' --plan
```

Preview all episodes recursively:

```bash
python -m app.tools.audio_cleanup '/media/cartoons/Phineas and Ferb' --recursive --plan
```

Write a **separate verified MKV**, leaving the original intact:

```bash
python -m app.tools.audio_cleanup '/path/to/episode.mkv'
```

Explicitly replace an original while **retaining a complete backup beside it**:

```bash
python -m app.tools.audio_cleanup '/path/to/episode.mkv' --replace
```

For recursive replacement, use `--recursive --replace` only after reviewing
the plan and ensuring adequate free space. The tool refuses a file when there
is no uniquely tagged Portuguese audio track. It returns a nonzero exit code
when any file is refused. Output files and retained backups are excluded from
subsequent recursive runs.

Safety: every operation stages a full remux beside the source, verifies video
and chosen audio stream SHA-256 bitstream hashes, checks resulting audio language
and default flag, and checks stream/chapter inventory. It rechecks the source
device/inode/size/mtime/ctime before swapping. During replacement, the original
is renamed to a unique adjacent backup before the staged output takes its name.
A failed remux or failed verification leaves the original unchanged. A failed
swap attempts immediate rollback. **Do not delete backups** until the restored
video has been independently checked in the target player.

The tool does not offer durable job recovery if the host is killed mid-swap;
an interrupted replacement may need manual inspection of hidden
`.original.mkv` and `.audio-cleanup.partial.mkv` paths. For unattended production
use, integrate a future audio cleanup job type with the existing persistent
queue, source guards and recovery journal instead of using this standalone tool.
