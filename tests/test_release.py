"""Release policy tests execute the workflow's actual stdlib Python policy."""
from pathlib import Path
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/docker-image.yml"


def policy():
    text = WORKFLOW.read_text()
    start = "          # BEGIN RELEASE POLICY\n"
    end = "          # END RELEASE POLICY"
    assert start in text and end in text, "workflow must contain executable release policy"
    code = textwrap.dedent(text.split(start, 1)[1].split(end, 1)[0])
    namespace = {"__name__": "release_policy_test"}
    exec(compile(code, str(WORKFLOW), "exec"), namespace)
    return namespace


def gate_policy():
    text = WORKFLOW.read_text()
    start = "          # BEGIN GATE POLICY\n"
    end = "          # END GATE POLICY"
    assert start in text and end in text, "workflow must gate exact-SHA CI"
    namespace = {"__name__": "release_policy_test"}
    exec(compile(textwrap.dedent(text.split(start, 1)[1].split(end, 1)[0]),
                 str(WORKFLOW), "exec"), namespace)
    return namespace


class ReleaseTests(unittest.TestCase):
    def test_registry_preflight_fails_closed(self):
        text = WORKFLOW.read_text()
        start = "          # BEGIN REGISTRY POLICY\n"
        end = "          # END REGISTRY POLICY"
        self.assertIn(start, text)
        namespace = {"__name__": "release_policy_test"}
        exec(compile(textwrap.dedent(text.split(start, 1)[1].split(end, 1)[0]),
                     str(WORKFLOW), "exec"), namespace)
        absent = namespace["manifest_absent"]
        self.assertTrue(absent(404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}'))
        for status, body in [(200, b'{}'), (401, b'{}'), (403, b'{}'),
                             (429, b'{}'), (500, b'{}'), (404, b'not found'),
                             (404, b'{"errors":[{"code":"NAME_UNKNOWN"}]}')]:
            with self.subTest(status=status, body=body):
                self.assertFalse(absent(status, body))

    def test_publishing_requires_validation_and_never_legacy_tags(self):
        text = WORKFLOW.read_text()
        self.assertIn("needs: validate", text)
        self.assertIn("docker/setup-qemu-action@", text)
        self.assertIn("linux/amd64,linux/arm64", text)
        self.assertNotIn("tag_latest", text)
        self.assertNotIn(":latest", text)
        self.assertNotIn("secrets.CR_PAT", text)
        for line in text.splitlines():
            if "${{ inputs." in line:
                self.assertTrue(line.startswith("  RELEASE_VERSION:") or
                                line.startswith("  ROUTEROS_VERSION:"))

    def test_supported_base_and_traceability_labels(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertRegex(dockerfile, r'^FROM alpine:3\.24\.2@sha256:[0-9a-f]{64}\n')
        for arg in ("WRAPPER_VERSION", "SOURCE_REVISION", "SOURCE_URL"):
            self.assertIn("ARG " + arg, dockerfile)
        for label in ("org.opencontainers.image.version", "org.opencontainers.image.revision",
                      "org.opencontainers.image.source", "io.mikrotik-routeros.seed.version"):
            self.assertIn(label, dockerfile)

    def test_named_ci_job_cannot_be_skipped(self):
        require_job = gate_policy().get("require_job")
        self.assertIsNotNone(require_job, "successful workflows must include a successful required job")
        require_job([dict(name="integration", status="completed", conclusion="success")], "integration")
        for jobs in ([], [dict(name="other", status="completed", conclusion="success")],
                     [dict(name="integration", status="completed", conclusion="skipped")]):
            with self.subTest(jobs=jobs), self.assertRaises(ValueError):
                require_job(jobs, "integration")

    def test_ci_gate_requires_latest_successful_exact_main_push(self):
        gate = gate_policy()["require_run"]
        sha = "a" * 40
        run = dict(id=10, run_attempt=1, head_sha=sha, head_branch="main",
                   event="push", status="completed", conclusion="success")
        self.assertEqual(gate([run], sha)["id"], 10)
        for changes in [dict(head_sha="b" * 40), dict(head_branch="feature"),
                        dict(event="pull_request"), dict(conclusion="failure"),
                        dict(status="in_progress"), dict(conclusion="skipped")]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                gate([dict(run, **changes)], sha)
        with self.assertRaises(ValueError):
            gate([], sha)
        with self.assertRaises(ValueError):
            gate([run, dict(run, id=11, conclusion="failure")], sha)

    def test_dispatch_validation_and_immutable_tags(self):
        validate = policy()["validate_inputs"]
        sha = "a" * 40
        tags = validate("1.0.0", "7.21.4", "refs/heads/main", sha, "7.21.4")
        self.assertEqual(tags, ["7.21.4-r1.0.0", "sha-" + sha])
        for release, seed, ref, revision in [
            ("$(touch /bad)", "7.21.4", "refs/heads/main", sha),
            ("1.0.0\nlatest", "7.21.4", "refs/heads/main", sha),
            ("01.0.0", "7.21.4", "refs/heads/main", sha),
            ("1.0.0", "../7.21.4", "refs/heads/main", sha),
            ("1.0.0", "7.21.5", "refs/heads/main", sha),
            ("1.0.0", "7.21.4", "refs/heads/feature", sha),
            ("1.0.0", "7.21.4", "refs/heads/main", "main"),
        ]:
            with self.subTest(values=(release, seed, ref, revision)):
                with self.assertRaises(ValueError):
                    validate(release, seed, ref, revision, "7.21.4")


if __name__ == "__main__":
    unittest.main()
