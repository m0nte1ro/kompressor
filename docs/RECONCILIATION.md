# Fixture reconciliation

This document describes the completed fixture milestone. Its read-only filesystem
and ffprobe follow-up is documented in [READ_ONLY_DISCOVERY.md](READ_ONLY_DISCOVERY.md);
that follow-up adds a scan endpoint while retaining the reconciliation model below.

This milestone adds a persistent reconciliation sandbox behind
`MediaProcessor.reconcile_library(snapshot)`. It consumes supplied observations;
it never walks/stat()s `/media`, computes hashes, invokes ffprobe, or encodes a
file. The existing seed library, tags, queue and HTTP/UI contracts are unchanged.
There is no scan HTTP endpoint and no automatic migration of seed IDs.

## Identity

- `media_id` identifies the logical Movie/Episode. Observations must supply an
  already-resolved ID; name parsing and semantic identity resolution are future work.
- `file_id` is the persistent logical **library-file record**, not an inode or a
  particular physical file. An in-place release replacement keeps it.
- `revision_id` identifies the record's content version. Changed evidence creates
  a new revision and invalidates old probe facts. Historical revisions remain.
- `artifact_id` identifies an independent output associated with a job, source
  reference and full preset snapshot. Keep-output/test results do not become
  library files or revise the source. Their reserved locations are excluded from
  subsequent observations imported into the library inventory.

Tags remain owned by media identity. A changed media ID at an existing location
produces a reconciliation issue; the service never silently transfers tags.
Copies and separately observed hardlinks receive separate logical file records.
Matching bytes alone does not merge files that are simultaneously present.

## Reconciliation and evidence

Each root has a monotonic scan sequence. Applying an older/already-applied sequence
fails without writes; rescanning with a new sequence is idempotent for inventory
identity. All changes commit in one repository transaction.

1. Unchanged paths are reserved before rename matching.
2. Unique displaced physical identities with unchanged content evidence identify
   renames, including path swaps, during complete scans.
3. Replacement at the same path keeps file identity but creates a revision when
   content evidence changes. Confirmed equal full hashes can avoid a needless
   revision even if the physical file or timestamps changed.
4. Ambiguous relocation uses tiered fingerprints: metadata first; compatible
   sample fingerprints select candidates; compatible full digests confirm a
   unique relocation. Samples alone never establish content equality.
5. Missing evidence or multiple candidates produces an issue containing candidate
   IDs and the next fingerprint tier needed, rather than guessing or inserting
   a duplicate. Strong fingerprints may need to have been collected *before*
   the old location disappeared.
6. Only complete, successful scans mark unseen files missing. Partial scans and
   unavailable roots preserve absence state. History is never deleted.

The service compares digests supplied by fixtures/adapters. A fingerprint's
scheme identifies its hash algorithm and, for samples, sample layout/version.
A routine unchanged scan requests no hashing and retains previously collected
stronger evidence. Metadata/inode matching is a heuristic, not mathematical
content identity; inode generation is stored when available. Filesystem-specific
identity reliability (especially mergerfs), explicit cross-root moves, identity
conflict resolution and adversarial same-stat changes still require production
work. Cross-root records are deliberately not auto-merged.

## Technical metadata

`MediaProbeResult` holds container, streams, chapters and metadata without Movie
or Episode identity. Streams retain indices, languages, dispositions, channel
layouts and unknown bitrates. Attachments remain explicit streams. Spatial
resolution and `progressive/interlaced/mixed/unknown` scan type are separate;
1920×1080 interlaced content has resolution class 1080.

HDR signalling stores transfer, primaries, matrix, bit depth, mastering/content
light metadata, DV configuration/RPU evidence and HDR10+ evidence. Classification
is derived on demand; it is not a separately editable label. Incomplete inspection
remains uncertain, 10-bit alone does not imply HDR, and DV/HDR10+ flags may coexist.
DV base-layer compatibility is conservatively unknown until a validated profile
classifier exists. The existing PolicyEngine remains unchanged: these new facts
are not yet imported into its legacy seed models. Future integration must block
unsupported or uncertain HDR rather than coercing it into SDR/HDR10. External
subtitle discovery and interpretation of real ffprobe output remain future work.

## Source safety and output artifacts

`SourceGuard.before_processing(reference)` and
`SourceGuard.before_replacement(reference)` independently obtain a fresh
observation through `ObservationSource`. Both require the captured revision to
remain current, the source to be present, matching identity/stat evidence and
exactly one known hardlink. Hash conflicts also invalidate the check. The second
check never reuses an earlier successful result.

`ArtifactService` is an isolated fixture lifecycle: plan an output, run the
pre-processing guard and mark the artifact simulated. Before any replacement
attempt it runs the second guard, then rejects keep-output mode or simulated
output. No artifact is marked validated; no source is marked previously processed.
These services do not bypass PolicyEngine or execute jobs: the existing fake queue
continues to use seed media. Production workers must integrate these guards with
policy checks, actual output validation and provenance recording. Checks alone
are not a filesystem transaction: real replacement also needs handle/path identity
checks and race-resistant replacement/rollback immediately around the operation.

`FixtureObservationSource` is intentionally independent of persisted scan state:
fixtures can change the source after a scan or between the two safety checks.
This exercises stale inventory, changed revision, new hardlinks and mount failure.

## Persistence and validation

The composition root injects `ReconciliationService` with a SQLite inventory
repository. It stores a versioned JSON document in the existing metadata table,
without changing schema version 1 or the current seed inventory. Before scaling
to a real library, use indexed file/revision/artifact tables rather than loading
this entire fixture-sized document.

Fixtures live in `fixtures/reconciliation/`; tests cover reconciliation, rollback,
restart persistence, source checks at both boundaries, artifacts and raw probe
facts. Existing UI/API tests continue to exercise the original seed workflow.
