# CHR image versions and provenance

## Public versions are CHR versions

Use `7.21.4` or `v7.21.4` in both repositories:

```text
hossein3piol/mikrotik-routeros:7.21.4
ghcr.io/im-ecorp/mikrotik-routeros:7.21.4
```

These aliases identify the bundled **RouterOS CHR seed**, used only to initialize a new persistent disk. Replacing a container does **not** upgrade an existing guest. Check the running RouterOS version separately. Compose and `.env.example` default to `7.21.4`.

CHR version aliases may receive tested container/runtime fixes **only with explicit overwrite approval**. Use the recorded `@sha256:...` index digest for immutable deployments.

Publication no longer creates a `sha-<full 40-character source SHA>` tag. That tag only duplicated `org.opencontainers.image.revision`, which is built into the image and readable with `docker inspect`, while adding a permanent unreadable entry to every public tag list. Historical `sha-` tags already in the registries are still **preserved and verified unchanged** by the recovery path; nothing is deleted. The separate candidate tag `sha-<source SHA>-chr-<seed>` is unrelated and still required — it is the staging handle the matrix publisher qualifies before promoting aliases.

The default-seed publisher and bounded recovery never write `latest`. The separately authorized **current website matrix** below may promote `latest` to qualified `7.24.4` only after all 17 versions pass. No publisher deletes images or creates new `-r` suffix tags. The existing Docker Hub `7.21.4-r1.0.0` reference is preserved read-only; it is not a new public versioning scheme and must not be newly published to GHCR. Previous documentation described suffix-tag publication and untouched CHR aliases. That policy is superseded by this explicitly approved CHR-only policy.

`WRAPPER_VERSION` / OCI `org.opencontainers.image.version` remain internal container-code metadata, not the public CHR version. `SOURCE_REVISION` and `SOURCE_URL` populate revision/source labels; `io.mikrotik-routeros.seed.version` records CHR separately. Local builds default to internal wrapper `dev` / revision `unknown` rather than claiming a release.

## Current website matrix (separate manual publisher)

`current-chr-matrix.yml` implements the reviewed website selection in [`config/chr-versions.json`](../config/chr-versions.json). This is a captured, reviewed scope, **not a claim of continuously tracking the website**. Discovery deduplicated the visible long-term, stable and development selectors in code:

- `6.49.17`, `6.49.18`, `6.49.19`, `6.49.20`, `6.49.21`, `6.49.22`
- `7.21.5`, `7.23.4`, `7.23.5`, `7.23.6`, `7.23.7`
- `7.24`, `7.24.1`, `7.24.2`, `7.24.3`, `7.24.4`, `7.25beta5`

Exactly **17** entries record the x86 VDI ZIP URL, vendor `checksumSHA256`, and channel membership. Discovery-response SHA-256 values identify the reviewed inputs. Hidden state mentioning `7.24rc4` is not an offered selector option and is excluded. Native ARM guest downloads are not used: both container platforms run the x86 guest. The default Dockerfile/Compose seed remains `7.21.4`; its vendor ZIP checksum is pinned separately. Every Dockerfile seed download must pass SHA-256 **before unzip**. Supplying a different `ROUTEROS_VERSION` without its corresponding `ROUTEROS_SHA256` fails closed.

The matrix does not weaken the older publisher's canonical `X.Y.Z` / tested-default restrictions. It separately accepts only the exact manifest entries, including `7.24` and `7.25beta5`. It uses three parallel jobs with `fail-fast: false`, so one guest failure does not prevent other versions from qualifying.

### Dispatch and gates

After merging reviewed changes to main:

```sh
gh workflow run current-chr-matrix.yml --repo im-ecorp/mikrotik-routeros --ref main \
  -f approve_version_overwrite=true
```

The boolean explicitly approves the listed version/`v` aliases **and** the gated `latest` update; false never authorizes replacing an existing alias. **False is not a dry run or a no-publication setting:** it may create previously absent candidate/source and qualified version/`v` tags, and it permits read-only reuse of already-correct tags. Existing aliases at a different digest remain blocked. Review the intended scope before dispatch. Credentials are the same Docker Hub secrets and job-scoped GHCR token as above; GHCR package Actions access is required. Only the authorized repository's manual main dispatch runs. Checkouts/build labels bind to its exact commit, and the unit suite plus required Compose validation run first. Unlike the default publisher, qualification comes from **each final built image**, not default-seed CI.

1. Snapshot all destinations and both old `latest` digests before building. Keep `chr-matrix-snapshot` unchanged throughout run attempts. Also snapshot existing `7.21.4`, `v7.21.4`, historical suffix and historical source-SHA references read-only in both registries.
2. Build both `linux/amd64` and `linux/arm64`, with SBOM/provenance, to Docker Hub's sole candidate source tag `sha-<full-source-SHA>-chr-<exact-seed>`. This candidate is publicly readable but **not runtime-qualified yet**; do not deploy candidate tags until evidence passes. Seed inclusion prevents matrix collisions. An existing source is label/platform/digest verified and reused rather than overwritten/rebuilt.
3. Pull that **exact final index digest** with `--platform linux/amd64` and execute the full actual `tests/docker-integration.py --run` harness. It checks the fresh guest seed, DHCP, published HTTP/SSH/UDP, guarded isolation, health transitions, clean shutdown, disk backups, restart and recreation persistence. Successful process exit, sanitized report, exact source/seed/checksum labels, harness hash and clean cleanup are required. ARM64 remains **build-only**.
4. Only after that version passes, copy the identical index and all platform/attestation manifests using `skopeo copy --all --preserve-digests` to both registries' exact version/`v` aliases and seed-qualified source tag. Recheck the original old-or-desired target digest before every write; read every destination back, including labels/config digests/platforms. Failed runtime qualification leaves that version's public aliases unchanged.
5. Aggregate requires **exactly 17 unique successful reports**, the current run/source, both builds and amd64 full qualification. Missing, duplicate, failed, foreign or incomplete evidence fails closed. Re-read all 102 source/version references across both registries and the preserved historical scope before changing `latest`. Only then copy the verified `7.24.4` digest to both `latest` aliases; re-read everything and record `latest_before`/`latest_after`.

### Reruns, partial failures and evidence

Use **Re-run failed jobs** on the same workflow run to recover a partial copy. The original snapshot is downloaded, not refreshed. Already-correct destinations are read-only; remaining targets must still equal the original baseline. Existing candidates are reused but receive fresh full amd64 runtime tests whenever that variant reruns. An all-jobs rerun also reuses the original snapshot. If its snapshot artifact is missing, stop; do not invent a baseline. A new dispatch is a new reviewed authorization boundary, not a blind recovery mechanism.

Version aliases can advance independently; `latest` requires the complete matrix. Cross-registry writes are not atomic: failure during copy can leave one alias advanced. No automatic rollback overwrites concurrent changes. Preserve the report/snapshot and rerun the failed jobs. Unknown drift blocks recovery even with overwrite approval. Registry tags are not compare-and-swap: the shared concurrency group serializes repository publishers, not external writers; restrict external write access.

Always-uploaded `matrix-output/report.json` records cover prerequisite, build, runtime, copy and aggregate failures. Subprocess failures include `failure.stage` (fixed `build`, `pull`, `runtime` or `copy`), `reason` (`nonzero_exit` or `timeout`), `exit_code` (null when timeout prevents observing an exit code) and `timed_out`. The embedded runtime report retains the harness's already-allowlisted `dhcp_diagnostics`, `health_diagnostics` and `network_diagnostics`. Its `failure.stage`/`reason` distinguish known harness checks by exact fixed-message mapping; unknown errors become `harness`/`unclassified_failure`, never copied text. A malformed runtime report records `harness`/`invalid_report` without hiding the subprocess failure. No qualification assertion is relaxed.

Raw subprocess output, arbitrary error text, authentication files/transcripts and guest disks are excluded. Private runtime files are not uploaded; the final JSON embeds the safe evidence. CLI output contains status, command and exception type only. Process/network waits and job durations are bounded. Hard runner loss can still prevent an artifact upload; the aggregate rejects missing evidence. Download the snapshot, all per-version reports and final aggregate for durable retention beyond the 90-day artifact window.

**Implementation and unit tests are not publication evidence.** Do not claim any matrix image or `latest` was published until the real hosted Docker jobs and final both-registry readback succeed. No production networking or in-guest package upgrade is part of this workflow.

## Release gates and dispatch

`Publish CHR image` (`docker-image.yml`) accepts only manual dispatch on `refs/heads/main` in `im-ecorp/mikrotik-routeros`. Both checkouts and build revision use the dispatch event's exact `github.sha`; no arbitrary source-ref input exists.

Before publishing:

1. Validate canonical `X.Y.Z` internal `release_version` and CHR `routeros_version`. The CHR seed must equal the Dockerfile default on this SHA. A different seed requires a source change and fresh CI, not a dispatch override. Inputs enter scripts through environment variables, not shell interpolation.
2. Require the latest **main push** runs of `validate.yml` and `runtime-integration.yml` for that exact source SHA to have completed successfully. Their latest-attempt jobs named `validate` and `integration` must explicitly succeed, not skip. PR merge checks, older commits, manual runtime runs, failed/skipped workflows, and pending runs do not qualify.
3. Run the unit suite, required Compose rendering, and shell syntax checks again. Publishing depends on this validation job.
4. Read all six intended references and both `latest` references through authenticated registry APIs. Only `404 MANIFEST_UNKNOWN` establishes absence; HTTP auth/rate-limit/server errors, ambiguous 404s, malformed JSON, missing digest headers, and header/body digest mismatches fail closed. Existing CHR aliases require boolean `approve_version_overwrite=true`. Existing SHA tags always stop the normal publisher. Internal wrapper metadata does not reserve a public Git tag.
5. Build `linux/amd64` and `linux/arm64` with provenance and SBOM, then publish only the CHR version, `v` version, and SHA reference in each registry. Read all six back: their exact index digest must match BuildKit output and their platform list must be amd64 plus arm64, excluding attestation descriptors. Confirm `latest` equals its preflight digest in both registries.

For a new CHR version with absent aliases, leave approval false:

```sh
gh workflow run docker-image.yml --repo im-ecorp/mikrotik-routeros --ref main \
  -f release_version=1.0.0 -f routeros_version=7.21.4 \
  -f approve_version_overwrite=false
```

For an intentional tested fix to **existing** CHR aliases, review the source and target digests, then explicitly set `-f approve_version_overwrite=true`. This never permits overwriting a SHA tag. Do not use a new build to recover the already-tested image described below.

Docker Hub requires `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`. GHCR uses the job-scoped `github.token` with `packages: write`; no `CR_PAT` is consumed. The existing GHCR package must grant this repository Actions write access. Validation has `contents: read` / `actions: read`; publishing has `contents: read` / `packages: write`. Checkouts do not retain credentials.

## Bounded exact-digest recovery

`Recover tested CHR 7.21.4 image` (`recover-chr-image.yml`) is a separate manual, main-only workflow. **Its presence is not proof that recovery has run.** Before deploying the new Compose default, require a successful recovery run and its verified `recovery-manifest.json`. Until then the CHR aliases may still point at the previous image, which lacks the runtime health-check contract.

Recovery does not build anything. `scripts/image_release.py` hardcodes the authorization boundary:

| Item | Exact value |
| --- | --- |
| Existing source repository | `hossein3piol/mikrotik-routeros` |
| Source SHA | `f6108c7672800618fd78e407ef79fcd15f69eef8` |
| CHR seed | `7.21.4` |
| Desired index digest | `sha256:9dc63b8d19af9bb3e343fd946c487189cdf912fc822f3f829455b616b91d8425` |
| Allowed old alias digest | `sha256:026123ea23088ceb1338b6d7d0d62ad06fd160c9394996e00c5a3e31c20e7449` |
| Preserved `latest` digest, both registries | `sha256:be880f3daf8926d6f51f31694bf7e1085ebd1113d1da8c6778afe1bd51b49569` |

The source was qualified by [Validate 35579006980](https://github.com/im-ecorp/mikrotik-routeros/actions/runs/35579006980) and [Runtime integration 35579006957](https://github.com/im-ecorp/mikrotik-routeros/actions/runs/35579006957). [Publication 35579514765](https://github.com/im-ecorp/mikrotik-routeros/actions/runs/35579514765) built both platforms and published to Docker Hub but failed the GHCR package write. Recovery re-queries successful exact-source main CI and named jobs; historical links alone do not bypass the gate.

Preflight checks all targets before the first copy:

- The existing source index must match the desired digest. Both amd64 and arm64 config blobs must verify by digest and contain internal wrapper `1.0.0`, CHR seed `7.21.4`, source revision above, and this repository's source URL. Config architecture/OS must agree with the index.
- Both `7.21.4` and `v7.21.4` in **each** registry must equal the known old or desired digest. Missing aliases or unrelated digests stop recovery.
- GHCR `sha-f6108c7672800618fd78e407ef79fcd15f69eef8` must be absent (`404 MANIFEST_UNKNOWN`) or already equal the desired digest.
- Docker Hub's existing suffix and source-SHA tags must equal the desired digest and remain untouched. Both `latest` tags must equal the preserved digest before and after recovery.

Only five references are eligible for writes: the two CHR aliases in each registry and the immutable GHCR source-SHA tag. Each copy reads the **existing digest**, never a mutable source tag:

```text
skopeo copy --all --preserve-digests --authfile <private-file> \
  docker://hossein3piol/mikrotik-routeros@sha256:9dc63b8d19af9bb3e343fd946c487189cdf912fc822f3f829455b616b91d8425 \
  docker://<approved-repository>:<approved-tag>
```

The script logs in using password stdin, captures subprocess output rather than logging credentials, and cleans its mode-0600 auth file in a private temporary directory on exit. The workflow uses an ephemeral hosted runner; no auth files or guest disks enter artifacts. No suffix GHCR tag, `latest`, deletion, rebuild, or production operation is allowed.

Targets already at the desired digest are read back without rewriting. Other targets are rechecked immediately before copying. Every destination is checked after its copy and again at the end; preserved references are checked again. This permits safe idempotent completion after a partial failure **within the hardcoded scope**.

After review and successful main CI for the workflow change, dispatch:

```sh
gh workflow run recover-chr-image.yml --repo im-ecorp/mikrotik-routeros --ref main \
  -f approve_version_overwrite=true
```

Inspect `recovery-manifest.json` and require `status: success`, all five destination digests, both unchanged `latest` digests, and preserved Docker Hub references. On failure the artifact records completed observations and `status: failed`, not a success claim. An unrelated target digest requires investigation, not relaxed checks or blind reruns.

## Concurrency and evidence

All publication/recovery workflows share the non-cancelling `chr-image-publication` concurrency group. This serializes these workflows, **not external writers**. Registry preflight is not compare-and-swap or registry-enforced immutability; restrict other writers. Cross-registry publication is not atomic. Do not delete or rebuild successful partial results merely to retry. The bounded recovery covers only the exact image above; another source/digest needs separately reviewed recovery authorization.

`release-manifest.json` records internal wrapper and CHR versions, source SHA/URL, run URL, verified references/digests/platforms, and `latest` preservation evidence. Recovery records the original source SHA separately from the workflow executor SHA, CI run evidence, before/after observations, and completion status. Artifacts and job summaries retain these records for 90 days; download and retain durable release evidence. The workflows cannot create Git tags or GitHub Releases (`contents` is read-only). Public release notes should identify **CHR 7.21.4**, not advertise wrapper `1.0.0` as the RouterOS version, and disclose that arm64 is build-only.

## Platform and reproducibility boundaries

The Dockerfile pins the multi-platform Alpine index:

```text
alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
```

The digest was matched to registry response bytes. The [official Alpine release table](https://alpinelinux.org/releases/) lists v3.24 main/community support, with branch end-of-support 2028-06-01. Community support generally lasts until the next stable release, **not necessarily that date**; QEMU comes from community. Recheck support before future builds.

| Container platform | Qualification |
| --- | --- |
| `linux/amd64` | Required real Docker/CHR runtime integration on the exact main source SHA |
| `linux/arm64` | Cross-built only; no claimed CHR runtime validation |

Both run the x86-64 CHR guest. Legacy `arm/v6`, `arm/v7`, and `386` images are not deleted but are excluded from new builds. The Alpine base, source and vendor seed archive checksum are pinned, while APK repositories remain external mutable inputs. Normal publication is traceable, not bit-for-bit reproducible; its final build can consume packages newer than CI. Recovery avoids that change by preserving the already-built exact digest and all platform manifests/attestations. Native arm64 runtime, KVM, production networks, and legacy-platform runtime remain outside qualification.
