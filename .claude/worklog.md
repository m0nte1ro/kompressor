# Work log: kompressor

## Where things stand
_Updated 2026-10-06_

- **Branch / state:** `feature/unknown-resolution-bitrate`, level with `origin` (pushed), no uncommitted work. Not merged to `main`; `main` still has the progress-write bug and the wrong recovery-table row, both fixed on this branch.
- **Next steps:**
  1. Check by hand in the CT that the audio-hash ffmpeg command (progress and the hash report both on stdout, `app/workers/ffmpeg.py` `packet_hashes`) prints `progress=` blocks and a `0,a,SHA256=…` line. Only fakes have tested it so far.
  2. Look at the UI in a browser: queued-job "Waiting: …" notes, the renamed `#preserve-subtitles-and-metadata` checkbox, the debounced bitrate field, and the README Workflows wording.
  3. Skim `docs/PROJECT_SPEC.md` as rendered Markdown (its structure was inferred by a script; the words were verified unchanged).
  4. Decide when to merge the branch to `main` (the user merges branches themselves).
- **Open questions / decisions pending:**
  - Real content fingerprinting for the source guard? Today it compares device, inode, size, mtime and hardlinks only, so an in-place edit that keeps size and mtime goes unnoticed (documented).
  - History grows forever, and the queue page snapshot plus startup recovery still read all of it.
- **User said:** each point fixed gets its own commit; commit on the current branch, never create branches unaided; never push unless asked.
- **Tried and dropped:**
  - Matching `_STATISTICS_WRITING_APP` to the muxing app literally: ffprobe reports MuxingApp (`libebml … + libmatroska …`), so the match is by tool family (`STATISTICS_WRITERS` in `ffmpeg.py`).
  - Cover-art `0:v:0` decode concern: a false alarm, because `build_command` maps the primary video first.
- **Watch out for:**
  - `QueueJob.move_to` now raises `InvalidTransition` for any status change missing from `JOB_TRANSITIONS` (`app/models/queue.py`). A path I missed would surface as a logged error, not a silent write.
  - The eligibility API response field is now `preserve_subtitles_and_metadata`; requests still accept `preserve_subtitles`.
  - No storage I/O may run inside a queue transaction: use `QueueService.storage_checks()` beforehand and pass the result to `revalidate(storage=...)`.
- **Relevant docs:**
  - `docs/REAL_ENCODING.md`: outage, watchdog, lock and guard behaviour changed this session.
  - `docs/INVENTORY_STORAGE.md`: jobs index and rescan transaction.
  - `README.md` (Architecture, Workflows): the queue module split.

## Log

### 2026-10-06
- **Did:** reviewed the branch against `main` and fixed all review findings: busy-only retries, root listing outside transactions with a 5 s limit, empty-root semantics with waiting notes, a watchdog for verification runs, a frame-statistics trust rule, and a debounced bitrate input. Then fixed the audit's remaining points, one commit each: spec Markdown, GPU naming in the docs, the presets report, the honest source guard, the `preserve_subtitles` rename, jobs-table indexing and progress throttling, rescan locking (3 s down to 13 ms for 4,000 files), source checks before the lock, the queue.py split with a transition table, and README fixes. 576 tests pass.
- **Decided:** split the database audit point into 3 commits, because each part had its own status; split queue.py with mixins, so its public API is unchanged; drop the tautological guard checks rather than add hashing.
- **Commits:** 9c542df, b70a812, 3813114, 6048490, e055537, a298bce, ed70f1d, 09c6a1a, 65be78d, 639e158, ce42267, 5a3aff0, 4a1f2f6, 4d37ecd, 17ff5cd, 9b5fd60
- **Left open:** the real-ffmpeg check, the browser check, and the merge to `main`.
