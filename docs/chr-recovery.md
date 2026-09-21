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

Only `recovery-output/report.json` is uploaded. Registry errors, bounded command
failures and runtime failure codes come from the instrumented executor. Raw
subprocess output, private runtime directories, disks, auth and exception strings
are never uploaded. Original runtime gates are not relaxed; a genuine guest
failure remains a failure and blocks latest. Registry copies are not atomic;
pre-write checks and final readback detect drift but cannot provide registry CAS.

Local tests use offline registries/process fixtures; they do not prove hosted
credentials, builds or seven-version guest success. Validate with required Compose
and actionlint, obtain independent review, then merge and manually dispatch on
main. A hosted failure is diagnostic evidence, not permission to weaken gates.
