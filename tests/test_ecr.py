import json
import subprocess
import unittest
from unittest.mock import patch

from approval_agent.ecr import resolve_ecr_tag


class EcrTests(unittest.TestCase):
    account = "123456789012"
    registry = f"{account}.dkr.ecr.eu-central-1.amazonaws.com"
    reference = f"{registry}/team/checkout:staging-candidate"

    def response(self, **overrides):
        detail = {
            "registryId": self.account,
            "repositoryName": "team/checkout",
            "imageDigest": "sha256:" + "a" * 64,
            "imageTags": ["staging-candidate", "42"],
            **overrides,
        }
        return subprocess.CompletedProcess([], 0, json.dumps({"imageDetails": [detail]}), "")

    @patch("approval_agent.ecr.subprocess.run")
    def test_resolves_and_pins_tag_using_explicit_registry(self, run):
        run.return_value = self.response()
        self.assertEqual(resolve_ecr_tag(self.reference, "portfolio"), f"{self.registry}/team/checkout@sha256:" + "a" * 64)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["aws", "ecr", "describe-images"])
        for flag, value in {
            "--region": "eu-central-1", "--registry-id": self.account, "--repository-name": "team/checkout",
            "--image-ids": "imageTag=staging-candidate", "--profile": "portfolio",
        }.items():
            self.assertEqual(command[command.index(flag) + 1], value)

    @patch("approval_agent.ecr.subprocess.run")
    def test_rejects_wrong_registry_tag_or_digest(self, run):
        for fields in ({"registryId": "000000000000"}, {"imageTags": ["43"]}, {"imageDigest": "invalid"}):
            with self.subTest(fields=fields):
                run.return_value = self.response(**fields)
                with self.assertRaisesRegex(ValueError, "matching image"):
                    resolve_ecr_tag(self.reference)

    @patch("approval_agent.ecr.subprocess.run")
    def test_bad_reference_never_invokes_aws(self, run):
        with self.assertRaisesRegex(ValueError, "private ECR reference"):
            resolve_ecr_tag("docker.io/checkout:latest")
        run.assert_not_called()
