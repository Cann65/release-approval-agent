"""Replay a mutable-tag release with real Git commits and an optional Docker registry."""

import argparse
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.request import Request, urlopen

from .ledger import Ledger
from .review import collect, operation_id, rules_advisor, utc_now
from .target import Client, DockerRuntime, Target, serve


class DemoRuntime:
    def __init__(self):
        self.operations = {}

    def start(self, operation, scope):
        self.operations[operation] = {"artifact": scope["artifact"], "simulation": True}
        return self.inspect(operation, scope)

    def inspect(self, operation, scope):
        return self.operations.get(operation)


class DropReply:
    def __init__(self, client):
        self.client = client

    def apply(self, operation, scope):
        self.client.apply(operation, scope)
        raise TimeoutError("simulated transport loss after the target accepted the deployment")

    def lookup(self, operation):
        return self.client.lookup(operation)


def command(*args):
    process = subprocess.run(args, check=True, capture_output=True, text=True, timeout=120)
    return process.stdout.strip()


def specimen(directory):
    repo = directory / "test-repository"
    shutil.copytree(Path(__file__).resolve().parents[1] / "example_service", repo)
    command("git", "-C", str(repo), "init", "-q", "--object-format=sha1")
    commits = []
    for version in (41, 42, 43):
        (repo / "version.txt").write_text(f"{version}\n")
        command("git", "-C", str(repo), "add", ".")
        command("git", "-C", str(repo), "-c", "user.name=Synthetic Release Fixture", "-c",
                "user.email=release-fixture@example.invalid", "commit", "-qm", f"Build checkout release {version}")
        commits.append(command("git", "-C", str(repo), "rev-parse", "HEAD"))
    return repo, commits


@contextmanager
def staging(directory, live):
    token = secrets.token_urlsafe(32)
    if not live:
        repository = "demo.invalid/checkout"
        runtime = DemoRuntime()
        with serve(Target(directory / "target.db", repository, runtime), token) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield repository, Client(f"http://127.0.0.1:{server.server_port}", token), runtime
            finally:
                server.shutdown()
                thread.join()
        return

    suffix = secrets.token_hex(5)
    registry = "approval-demo-registry-" + suffix
    command("docker", "run", "--rm", "-d", "--name", registry, "-p", "127.0.0.1::5000", "registry:2.8.3")
    with ExitStack() as cleanup:
        cleanup.callback(command, "docker", "rm", "-f", registry)
        port = command("docker", "port", registry, "5000/tcp").split(":")[-1]
        repository = f"127.0.0.1:{port}/portfolio/checkout"
        env = {**os.environ, "STAGING_TARGET_TOKEN": token}
        target_process = subprocess.Popen(
            [sys.executable, "-m", "approval_agent", "--db", str(directory / "target.db"),
             "target", "--repository", repository, "--port", "0"],
            stdout=subprocess.PIPE, text=True, env=env,
        )
        cleanup.callback(target_process.wait, 8)
        cleanup.callback(target_process.terminate)
        endpoint = target_process.stdout.readline().strip()
        if not endpoint.startswith("http://127.0.0.1:"):
            raise RuntimeError("staging control plane failed to start")
        yield repository, Client(endpoint, token), DockerRuntime()


def build_artifact(repo, revision, version, repository, live):
    tag = repository + ":staging-candidate"
    with tempfile.TemporaryDirectory() as context:
        archive = subprocess.check_output(["git", "-C", str(repo), "archive", revision], timeout=20)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(context, filter="data")
        checks = subprocess.run([sys.executable, "-m", "unittest", "discover"], cwd=context,
                                check=True, capture_output=True, text=True, timeout=20)
        if "Ran 1 test" not in checks.stderr:
            raise RuntimeError("expected the release service smoke test to run")
        if live:
            command("docker", "build", "--label", f"org.opencontainers.image.revision={revision}",
                    "--tag", tag, context)
    if live:
        command("docker", "push", tag)
        registry = repository.split("/")[0]
        manifest = Request(f"http://{registry}/v2/portfolio/checkout/manifests/staging-candidate",
                           headers={"Accept": ", ".join([
                               "application/vnd.oci.image.index.v1+json",
                               "application/vnd.oci.image.manifest.v1+json",
                               "application/vnd.docker.distribution.manifest.v2+json",
                               "application/vnd.docker.distribution.manifest.list.v2+json",
                           ])})
        with urlopen(manifest, timeout=5) as response:
            artifact = repository + "@" + response.headers["Docker-Content-Digest"]
    else:
        artifact = repository + "@sha256:" + f"{version:064x}"
    checks = {"status": "passed", "revision": revision, "artifact": artifact, "checks": 1}
    return artifact, checks


def run(directory, live=False, interactive=False):
    repo, (base, version_42, version_43) = specimen(directory)
    ledger = Ledger(directory / "approvals.db")
    actor = "staging-reviewer"
    ledger.set_role(actor, "staging", True)
    with staging(directory, live) as (repository, client, runtime), ExitStack() as cleanup:
        artifact_42, checks_42 = build_artifact(repo, version_42, 42, repository, live)
        health = {"service": "checkout", "environment": "staging", "requests": 12840,
                  "errors": 29, "collected_at": utc_now().isoformat()}
        ready = ledger.review(collect(repo, base, version_42, artifact_42, checks_42, health), rules_advisor)
        if interactive:
            print(json.dumps({key: ready[key] for key in ("id", "scope", "recommendation")}, indent=2))
            if input("Type the reviewed image digest to approve staging: ").strip() != artifact_42:
                raise ValueError("approval cancelled")
        ledger.approve(ready["id"], actor, artifact_42)
        artifact_43, checks_43 = build_artifact(repo, version_43, 43, repository, live)
        assert artifact_42 != artifact_43

        if live:
            cleanup.callback(
                subprocess.run, ["docker", "rm", "-f", runtime.name(operation_id(ready["scope"]))],
                capture_output=True, timeout=15,
            )
        error = ledger.dispatch(ready["id"], DropReply(client))
        assert error["state"] == "unknown"
        confirmed = ledger.dispatch(ready["id"], client)
        assert confirmed["state"] == "deployed" and confirmed["scope"]["artifact"] == artifact_42
        deployment = confirmed["result"]["deployment"]
        observed = "42"
        if live:
            with urlopen(deployment["url"], timeout=5) as response:
                observed = json.load(response)["version"]
            assert observed == "42"

        flawed_evidence = collect(repo, version_42, version_43, artifact_43, None, health)
        flawed = ledger.review(flawed_evidence, lambda *_: {
            "decision": "recommend", "summary": "The staging error ratio is within policy.",
            "references": ["monitoring"],
        })
        ledger.approve(flawed["id"], actor, artifact_43)
        invalid_dispatch = ledger.dispatch(flawed["id"], client)
        assert invalid_dispatch["state"] == "blocked"

        revoked = ledger.review(collect(repo, version_42, version_43, artifact_43, checks_43, health), rules_advisor)
        ledger.approve(revoked["id"], actor, artifact_43)
        ledger.set_role(actor, "staging", False)
        revoked_dispatch = ledger.dispatch(revoked["id"], client)
        assert revoked_dispatch["state"] == "blocked"
        assert sum(event["event"] == "dispatch_claimed" for event in ledger.audit(ready["id"])) == 1

    return {
        "registry_mode": "local Docker registry" if live else "synthetic image digests",
        "tag_after_approval": artifact_43,
        "deployed": {"scope": confirmed["scope"], "observed_version": observed,
                     "states": [error["state"], confirmed["state"]], "audit": ledger.audit(ready["id"])},
        "missing_tests": {"recommendation": flawed["recommendation"], **invalid_dispatch["result"],
                          "audit": ledger.audit(flawed["id"])},
        "revoked_role": {**revoked_dispatch["result"], "audit": ledger.audit(revoked["id"])},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="release-approval-") as temp:
        report = run(Path(temp), args.live, args.interactive)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
