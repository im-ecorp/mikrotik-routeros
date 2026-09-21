# Wrapper releases and provenance

## Versions are separate

- **Wrapper release:** `v1.0.0` identifies this repository's container/runtime code.
- **RouterOS seed:** `7.21.4` identifies the bundled vendor VDI used only to initialize a new persistent disk. A new container or seed does **not** upgrade an existing RouterOS guest. Check the running guest's version separately.
- **Container tags:** `7.21.4-r1.0.0` and `sha-<full 40-character source SHA>` in both `hossein3piol/mikrotik-routeros` and `ghcr.io/im-ecorp/mikrotik-routeros`.

The wrapper publisher never writes `latest`, `7.21.4`, or `v7.21.4`. Existing images and those compatibility tags are left untouched; they do not automatically receive wrapper fixes. Use a wrapper tag or, preferably, the recorded `@sha256:...` manifest digest.

`WRAPPER_VERSION`, `SOURCE_REVISION`, and `SOURCE_URL` build arguments populate the OCI version, revision, and source labels. `io.mikrotik-routeros.seed.version` records the seed independently. Local unlabelled builds intentionally default to wrapper `dev` / revision `unknown`, not a claimed release.

## Supported Alpine base and platform scope

The Dockerfile pins the **multi-platform index**, not an amd64-only manifest:

```text
alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
```

The digest was retrieved from Docker Hub's registry API and independently matched to SHA-256 of the response bytes. The [official Alpine release table](https://alpinelinux.org/releases/) lists v3.24 as supported in both main and community, with branch end-of-support 2028-06-01. Community support generally lasts until the next stable release, **not necessarily that end-of-support date**; QEMU comes from community. Recheck support and rebuild under a new wrapper release when Alpine's support level changes.

The initial publication matrix is deliberately conservative:

| Container platform | Release qualification |
| --- | --- |
| `linux/amd64` | Required real Docker/CHR runtime integration on a GitHub-hosted runner, exact main source SHA |
| `linux/arm64` | Cross-built with `docker/setup-qemu-action`; build-only, no claimed CHR runtime validation |

Both run the x86-64 CHR guest with `qemu-system-x86_64`; arm64 here describes the container host, not a native ARM RouterOS guest. The v3.24 main/community APK indexes for `x86_64` and `aarch64` were checked: every explicit Dockerfile package is present, including `qemu-system-x86_64` 11.0.3-r0. Index availability is not a substitute for a successful image build: the release run must complete both platform builds and read back the manifest before success is claimed.

The former publisher advertised `linux/arm/v6`, `linux/arm/v7`, and `linux/386` without this release's build/runtime evidence. They are excluded from **new wrapper releases**, not deleted from old images. The generic Dockerfile and existing published compatibility images remain available. Reintroduce a platform only after verifying its package dependencies, actual build, and documenting whether runtime was tested.

## Release gates and dispatch

Only `workflow_dispatch` on `refs/heads/main` in `im-ecorp/mikrotik-routeros` can publish. There is no arbitrary source-ref input. The source is the dispatch event's immutable `github.sha`, used explicitly by both checkouts and OCI labels.

Before any registry push:

1. Validate canonical `X.Y.Z` wrapper and seed inputs using environment variables, never shell interpolation. The seed must equal the Dockerfile default tested on that SHA; a different seed requires a source change and fresh CI, not a dispatch override.
2. Query GitHub Actions for the latest **main push** runs of `.github/workflows/validate.yml` and `.github/workflows/runtime-integration.yml` on that exact SHA. Both must have completed successfully, and their latest-attempt jobs named `validate` and `integration` must explicitly succeed rather than skip. PR-merge SHA checks, older source commits, manual integration runs, skipped/failed workflows, and still-running checks do not qualify.
3. Run the unit suite, required Compose rendering, and shell syntax validation again in the publisher's `validate` job. `publish` explicitly depends on this job.
4. Refuse an existing wrapper Git tag `v<release_version>`, then check both new image tags in both registries. Only an authenticated registry `404 MANIFEST_UNKNOWN` is accepted as absence. Authentication errors, rate limits, malformed responses, other errors, and existing tags stop publication.
5. Build amd64 and arm64 with provenance and SBOM enabled, push only the four explicit references, then read every reference back. Its digest must match BuildKit's output and its platform list must be exactly amd64 plus arm64 (attestation descriptors excluded).

The workflow has one non-cancelling concurrency group to serialize its own publishers. This is a **fail-on-reuse policy**, not registry-enforced immutability: an unrelated writer can race a preflight. Restrict other writers and enable registry tag immutability where available. Cross-registry publishing is not atomic. If a push partly succeeds, do not overwrite or delete the successful tags or blindly rerun: investigate the recorded digests and finish recovery deliberately. The SHA tag deliberately prevents publishing different wrapper metadata from the same source commit.

Wait for both main push workflows to finish, then dispatch:

```sh
gh workflow run docker-image.yml --repo im-ecorp/mikrotik-routeros --ref main \
  -f release_version=1.0.0 -f routeros_version=7.21.4
```

Required secrets: `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`. GHCR uses the job-scoped `GITHUB_TOKEN` with `packages: write`; the legacy `CR_PAT` is not consumed. The existing GHCR package must grant this repository Actions write access; if not, correct package access rather than weakening the token policy. The validation job gets only `contents: read` and `actions: read`; publishing gets `contents: read` and `packages: write`. Checkouts do not retain credentials.

## Published release record

The workflow records the full source SHA, wrapper and seed versions, all image references and verified digests, actual platform list, qualification boundary, and run URL in `release-manifest.json`. It uploads that file as the `release-manifest` artifact (90-day retention) and writes it to the job summary. The Docker build action also publishes its build record; OCI source/revision/version labels plus provenance and SBOM remain associated with the images.

The image workflow intentionally cannot create Git tags or GitHub Releases (`contents` is read-only). After a successful run, a maintainer must attach the manifest to a durable GitHub Release at the **same source SHA**. Do not create `v1.0.0` before the image run: preflight rejects reused Git tags. Example, replacing the placeholders with the successful run ID and its recorded full SHA:

```sh
gh run download <run-id> --repo im-ecorp/mikrotik-routeros \
  --name release-manifest --dir release-evidence
gh release create v1.0.0 --repo im-ecorp/mikrotik-routeros \
  --target <full-source-sha> --title 'Wrapper v1.0.0 (RouterOS seed 7.21.4)' \
  --notes-file <reviewed-release-notes.md> release-evidence/release-manifest.json
```

Release notes should link the exact main Validate and Runtime integration runs and the image publication run, and identify arm64 as build-only. Verify the release tag target and attached manifest after creation. A checked-in workflow, package-index lookup, or passing policy tests alone is **not** a published release or runtime proof.

## Reproducibility boundary

The Alpine base index and source commit are pinned. APK repositories and MikroTik's HTTPS seed URL remain external inputs and may change; this is traceable publication, not a claim of bit-for-bit reproducibility or independently authenticated vendor seed checksums. The runtime gate tests the source's amd64 integration build; the final multi-platform release build differs in wrapper labels and can consume newer APK packages. Preserve CI/build records, review provenance/SBOM, and pin deployments by the verified final digest. Native arm64 runtime, KVM acceleration, production network environments, and every legacy platform are outside the initial release qualification.
