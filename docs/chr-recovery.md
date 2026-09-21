# Bounded original-dispatch recovery

`chr-matrix-recovery.yml` is manual, main-only and requires explicit overwrite
approval. It shares `chr-image-publication` concurrency with the existing
publishers. Do not rerun the original publisher blindly.

The executor comes from the dispatch SHA. The build tree, Dockerfile, seed
manifest, harness and its serial helper come from the pristine separate checkout
of `858d85fd17ff720df88a65b44ec65ecf198dd8dd`. The executor SHA must differ from
that source. Nothing patches the guest, runtime or qualification harness.

The only variant jobs are `6.49.21`, `6.49.22`, `7.23.5`, `7.24.2`, `7.24.3`,
`7.24.4`, and `7.25beta5`. Every job verifies the snapshot bytes from original run
`35607084875` against SHA-256
`4deddff21e16cc5a22d5d1d5c134253395c3e2feb88d1086f699c3c16091772d`.
It never creates a replacement baseline. Ten successful original reports have
individually pinned byte hashes; their original qualification identity is retained
and all sixty references are read back before recovery writes. The eight preserved
historical references, all remaining source tags and aliases, and both `latest`
references are also checked. Mutable destinations must be baseline or verified
original-source desired digest. Missing/expired artifacts or drift fail closed.

Existing valid immutable source tags are reused, not rebuilt. A missing seed is
built only from the original source checkout and vendor checksum. Exact-digest
amd64 qualification uses the original harness; arm64 remains build-only. Version
aliases are copied only after that qualification. `latest` is separately gated on
all seventeen original-source reports and both-registry readback through the
unchanged matrix policy. Original labels and top-level `run_id`/`source_sha` stay
unchanged; the `recovery` object records executor SHA, actual recovery run/attempt,
original run and snapshot hash. The ten originals are never rewritten.

Variant jobs run **one at a time** (`max-parallel: 1`). Two full runs showed the
qualification is timing-sensitive when three QEMU TCG guests share a runner:
7.24.3 passed at 21 checks then failed at 14, and 7.24.2 stopped at 14 checks then
19. Versions that pass do so in 131-138 seconds; a stalled one spends ~110 extra
seconds in a serial read before the harness gives up. 6.49.21 and 6.49.22 fail
identically at 1 shutdown and 11 checks in every attempt and are a separate,
guest-side problem that serialising does not address.

Artifacts have attempt-specific names, without overwrite. Aggregation requires
both `needs.preflight.result` and `needs.variant.result` to be `success`. That
matrix summary alone is not proof of every version's last execution on selective
reruns. Before creating a write-scoped copier, the executor uses `gh api` with
read-only `actions` permission to fetch **all jobs, across all attempts**, for the
exact recovery run. It checks pagination completeness, repository/run URL,
executor source SHA, attempt bounds and unique per-version attempts. Each
version's newest executed job must have completed successfully and its exact
attempt's report must exist. Runner death, cancellation or upload failure cannot
fall back to an older success, including on a later aggregate-only rerun.

Aggregate-only reruns retain successful reports for versions not rerun; failed-job
reruns replace only versions actually reexecuted. Missing API evidence, API errors,
unknown jobs, foreign/duplicate/future evidence and missing newest reports fail
closed. The current workflow keeps GitHub's `variant (VERSION)` job names as the
API identity contract. Cross-run recovery-report reuse is unsupported. A repeated
local invocation refuses an existing output directory rather than replacing evidence.
The unchanged seventeen-report qualification gate still follows these checks.

### Shared registry content

Docker Hub counts a pull as a `GET` on `/v2/*/manifests/*`; manifest `HEAD`
requests are exempt, which is why the documented way to read your own remaining
quota is a `HEAD`. Measured directly against `registry-1.docker.io` on
2026-09-22 with a single token:

| request | `ratelimit-remaining` |
| --- | --- |
| `HEAD` x3 | 100, 100, 100 |
| first `GET` | 99 |
| second `GET` | 99 |
| `HEAD` after | 99 |

`HEAD` never decrements, `GET` does, and two `GET`s on one manifest cost a single
pull. The advertised window was `w=3600`, so no code may assume a particular
window length. Every job therefore resolves tags with `HEAD` and keeps a
digest-keyed cache of the bytes it has already verified.

The preflight job additionally uploads `registry-content.json`
(`chr-recovery-content-<attempt>`): base64 manifest bytes keyed by
`<repository>|sha256:<digest>`, and nothing else. Variant and aggregate jobs
import it and re-hash every entry against the digest that keys it, refusing any
mismatch, foreign repository, bad schema or non-manifest payload. **Tag bindings
are deliberately never shared.** Each job still resolves every tag live with a
fresh `HEAD`, so drift detection is exactly as strong as before; shared content
only answers "what are the bytes behind this digest", which is immutable.

Because entries are digest-addressed, any attempt's artifact is equally valid,
and a missing one is not an error: the consumer download is `continue-on-error`
and the job falls back to full reads. Shared content is a quota optimization and
never an input to a correctness decision.

Measured over the offline budget fixture, one full recovery run
(1 preflight + 7 variants + 1 aggregate) costs on Docker Hub:

| | manifest `GET` (billable) |
| --- | --- |
| before | 1053 |
| digest cache only | 603 |
| digest cache + shared content | 179 |
| + scoped absence proof | **95** |

### Scoped absence proof

A `HEAD` alone cannot prove `MANIFEST_UNKNOWN`, so absence still costs a `GET`,
and absence is never cached — absence is what authorizes a write.

Each job therefore GET-proves absence only for the destinations it may write:
a variant job covers its own version, while the preflight and aggregate jobs
cover all seventeen. Every other destination is compared from a fresh `HEAD`.

This removes redundancy, not coverage. Each of the seven failed versions is still
GET-proven three times — by the preflight job, by its own variant job immediately
before its writes, and by the aggregate job before `latest` moves — instead of
nine times by every job in the run. The write path in `publish_version` re-proves
each target with `manifest()` immediately before copying regardless, and
`chr-image-publication` concurrency already serialises publication, so external
drift still fails the run.

`RecoveryRegistry.resolve()` is the read-only half of that split and is
deliberately unavailable to the write path.

Only `recovery-output/report.json` and `recovery-output/registry-content.json` are uploaded. Registry errors, bounded command
failures and runtime failure codes come from the instrumented executor. Raw
subprocess output, private runtime directories, disks, auth and exception strings
are never uploaded. Original runtime gates are not relaxed; a genuine guest
failure remains a failure and blocks latest. Registry copies are not atomic;
pre-write checks and final readback detect drift but cannot provide registry CAS.

Local tests use offline registries/process fixtures; they do not prove hosted
credentials, builds or seven-version guest success. Validate with required Compose
and actionlint, obtain independent review, then merge and manually dispatch on
main. A hosted failure is diagnostic evidence, not permission to weaken gates.
