"""Resolve a private ECR image tag through the AWS CLI credential chain."""

import json
import re
import subprocess


ECR_IMAGE = re.compile(
    r"(?P<registry>(?P<account>[0-9]{12})\.dkr\.ecr\."
    r"(?P<region>[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+)\.amazonaws\.com)/"
    r"(?P<repository>[a-z0-9]+(?:[._/-][a-z0-9]+)*):(?P<tag>[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127})"
)


def resolve_ecr_tag(reference, profile=None):
    image = ECR_IMAGE.fullmatch(reference)
    if image is None:
        raise ValueError("provide a private ECR reference ending in :tag")

    command = [
        "aws", "ecr", "describe-images", "--region", image["region"],
        "--registry-id", image["account"], "--repository-name", image["repository"],
        "--image-ids", f"imageTag={image['tag']}", "--output", "json",
        "--no-cli-pager", "--no-cli-auto-prompt",
    ]
    if profile:
        command.extend(["--profile", profile])
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
    except FileNotFoundError as exc:
        raise ValueError("AWS CLI v2 is required to resolve an ECR tag") from exc
    except subprocess.SubprocessError as exc:
        raise ValueError("ECR tag lookup failed; check the tag, profile, region and ecr:DescribeImages access") from exc

    try:
        (detail,) = json.loads(result.stdout)["imageDetails"]
        digest = detail["imageDigest"]
        tags = detail["imageTags"]
        if (detail["registryId"] != image["account"] or detail["repositoryName"] != image["repository"]
                or not isinstance(tags, list) or image["tag"] not in tags
                or re.fullmatch(r"sha256:[a-f0-9]{64}", digest) is None):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("ECR did not return one matching image with a valid SHA-256 digest") from exc
    return f"{image['registry']}/{image['repository']}@{digest}"
