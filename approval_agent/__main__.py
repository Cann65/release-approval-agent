"""Operate the local approval ledger and the separate staging control plane."""

import argparse
import json
import os
import sys
from pathlib import Path

from .ledger import Ledger
from .review import ModelAdvisor, collect, rules_advisor
from .target import Client, DockerRuntime, Target, serve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("approvals.db"))
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("review")
    review.add_argument("--repo", type=Path, required=True)
    for key in ("base", "revision", "artifact"):
        review.add_argument("--" + key, required=True)
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
    execute = commands.add_parser("execute")
    execute.add_argument("review_id")
    execute.add_argument("--target", default="http://127.0.0.1:8114")
    audit = commands.add_parser("audit")
    audit.add_argument("review_id")
    target = commands.add_parser("target")
    target.add_argument("--port", type=int, default=8114)
    target.add_argument("--repository", required=True)
    args = parser.parse_args()

    if args.command in ("execute", "target"):
        token = os.environ.get("STAGING_TARGET_TOKEN")
        if not token:
            parser.error("set STAGING_TARGET_TOKEN in the executor environment")
    if args.command == "target":
        with serve(Target(args.db, args.repository, DockerRuntime()), token, port=args.port) as server:
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
            reports = [json.loads(path.read_text()) if path else None for path in (args.tests, args.monitoring)]
            evidence = collect(args.repo, args.base, args.revision, args.artifact, *reports)
            advisor = (ModelAdvisor(args.model_endpoint, args.model, os.environ.get("RELEASE_MODEL_TOKEN"))
                       if args.model else rules_advisor)
            output = ledger.review(evidence, advisor)
        elif args.command == "approve":
            output = ledger.approve(args.review_id, args.actor, args.artifact)
        elif args.command == "execute":
            output = ledger.dispatch(args.review_id, Client(args.target, token))
        else:
            output = ledger.audit(args.review_id)
    except (ValueError, KeyError, OSError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(output, indent=2, allow_nan=False))
    if args.command == "execute" and output["state"] != "deployed":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
