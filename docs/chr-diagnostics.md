# Read-only CHR matrix diagnostics

Run **Read-only original CHR matrix diagnostics** manually from `main` to inspect
the runner-context failure before any further publication. There are no inputs.
It downloads `chr-matrix-snapshot` from Actions run `35607084875`, requires the
exact original snapshot SHA-256, validates the reviewed manifest and original
approval, then reads the eight historical preservation references. The image
source remains `858d85fd17ff720df88a65b44ec65ecf198dd8dd`; the current workflow
commit is recorded separately as `executor_sha`.

The workflow has only `actions: read`, `contents: read`, and `packages: read`.
It uses the same Docker Hub secrets and GitHub actor/token as the matrix, but
requests pull-scoped registry tokens through HTTP GET. It invokes no Docker
builds, pulls, logins, copier, subprocess, or registry write. It neither creates
nor updates a snapshot. Only `diagnostic-output/report.json` is uploaded; a fixed
prerequisite failure record is uploaded if the probe could not start.

Failure evidence contains fixed stage/reason codes, registry operation
(`token`, `manifest`, `config`), allowlisted registry name, bounded HTTP status,
and request attempt (1 or the existing one-time 401 refresh, 2). The preservation
reference ordinal is bounded to the validated eight-entry scope. No credentials,
HTTP bodies, URLs, raw process output, or exception strings are reported.

Matrix variant reports retain a reviewed version before snapshot validation and
distinguish snapshot loading, validation, preservation, source checking, runtime,
and alias promotion. Build/copy failures may include `output_reason`, a fixed
marker classification from at most the final 64 KiB of private output. This is a
hint, not proof of root cause; unrecognized output retains the generic exit
failure. Raw output remains private and is not uploaded.

Diagnostics do not qualify an image or authorize recovery. The existing
qualification, immutable source reuse, digest, source identity, alias approval,
and preservation gates are unchanged. Do not rerun a new publisher source or
rebaseline the original run to recover old images. A later recovery must bind
to the original source and existing verified digests under separate review.
