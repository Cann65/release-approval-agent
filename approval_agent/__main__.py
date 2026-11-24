"""Operate the local approval ledger and the separate staging control plane."""

import argparse
import json
import os
import sys
from pathlib import Path

from .authorization import DeploymentAuthorization
from .ecr import resolve_ecr_tag
from .ledger import Ledger
from .review import ModelAdvisor, collect, rules_advisor
from .target import Client, DockerRuntime, Target, serve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("approvals.db"))
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("review")
    review.add_argument("--repo", type=Path, required=True)
    for key in ("base", "revision"):
        review.add_argument("--" + key, required=True)
    image = review.add_mutually_exclusive_group(required=True)
    image.add_argument("--artifact", help="immutable OCI image reference")
    image.add_argument("--ecr-tag", help="private AWS ECR image reference ending in :tag")
    review.add_argument("--aws-profile", help="optional AWS CLI profile for ECR")
    for key in ("tests", "monitoring"):
        review.add_argument("--" + key, type=Path)
    review.add_argument("--model-endpoint")
    review.add_argument("--model")

    role = commands.add_parser("role")
    role.add_argument("actor")
    role.add_argument("action", choices=("grant", "revoke"))
    approve = commands.add_parser("approve")
    approve.add_argument("review_id")
    approve.add_argument("--actor", required=True)
    approve.add_argument("--artifact", required=True)
    for name, help_text in (
        ("execute", "dispatch an approved release"),
        ("reconcile", "check an operation without sending a deployment request"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("review_id")
        command.add_argument("--target", default="http://127.0.0.1:8114")
    audit = commands.add_parser("audit")
    audit.add_argument("review_id")
    target = commands.add_parser("target")
    target.add_argument("--port", type=int, default=8114)
    target.add_argument("--repository", required=True)
    args = parser.parse_args()

    authorization = None
    if args.command in ("execute", "reconcile", "target"):
        token = os.environ.get("STAGING_TARGET_TOKEN")
        if not token:
            parser.error("set STAGING_TARGET_TOKEN for the staging connection")
    if args.command in ("execute", "target"):
        try:
            authorization = DeploymentAuthorization.from_hex(os.environ.get("STAGING_SIGNING_KEY", ""))
        except ValueError as exc:
            parser.error(str(exc))
    if args.command == "target":
        with serve(Target(args.db, args.repository, DockerRuntime()), token,
                   authorization=authorization, port=args.port) as server:
            print(f"http://127.0.0.1:{server.server_port}", flush=True)
            server.serve_forever()
        return 0
    ledger = Ledger(args.db)
    if args.command == "role":
        ledger.set_role(args.actor, "staging", args.action == "grant")
        return 0
    try:
        if args.command == "review":
            if bool(args.model_endpoint) != bool(args.model):
                parser.error("--model and --model-endpoint are required together")
            if args.aws_profile and not args.ecr_tag:
                parser.error("--aws-profile requires --ecr-tag")
            reports = [json.loads(path.read_text()) if path else None for path in (args.tests, args.monitoring)]
            artifact = resolve_ecr_tag(args.ecr_tag, args.aws_profile) if args.ecr_tag else args.artifact
            evidence = collect(args.repo, args.base, args.revision, artifact, *reports)
            if args.ecr_tag:
                evidence["artifact"]["resolved_from"] = args.ecr_tag
            advisor = (ModelAdvisor(args.model_endpoint, args.model, os.environ.get("RELEASE_MODEL_TOKEN"))
                       if args.model else rules_advisor)
            output = ledger.review(evidence, advisor)
        elif args.command == "approve":
            output = ledger.approve(args.review_id, args.actor, args.artifact)
        elif args.command in ("execute", "reconcile"):
            client = Client(args.target, token, authorization=authorization)
            output = ledger.dispatch(args.review_id, client, reconcile_only=args.command == "reconcile")
        else:
            output = ledger.audit(args.review_id)
    except (ValueError, KeyError, OSError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(output, indent=2, allow_nan=False))
    if args.command in ("execute", "reconcile") and output["state"] != "deployed":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
