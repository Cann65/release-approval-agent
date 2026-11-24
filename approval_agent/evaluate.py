"""Measure whether a reviewer identifies blocked releases and cites the right evidence."""

import argparse
import json
import os
from copy import deepcopy
from datetime import timedelta

from .review import ModelAdvisor, rules_advisor, utc_now, validate_recommendation


def evaluate(advisor):
    now = utc_now()
    revision = "a" * 40
    artifact = "registry.example/checkout@sha256:" + "b" * 64
    passing = {"status": "passed", "revision": revision, "artifact": artifact, "checks": 22}
    healthy = {"service": "checkout", "environment": "staging", "errors": 12,
               "requests": 9500, "collected_at": now.isoformat()}
    cases = [
        ("ready", passing, healthy, "recommend", {"tests", "monitoring"}),
        ("tests-missing", None, healthy, "hold", {"tests"}),
        ("tests-failed", {**passing, "status": "failed"}, healthy, "hold", {"tests"}),
        ("tests-for-another-image", {**passing, "artifact": artifact[:-1] + "c"}, healthy, "hold", {"tests"}),
        ("errors-elevated", passing, {**healthy, "errors": 190}, "hold", {"monitoring"}),
        ("metrics-stale", passing, {**healthy, "collected_at": (now - timedelta(hours=1)).isoformat()},
         "hold", {"monitoring"}),
        ("both-unavailable", None, None, "hold", {"tests", "monitoring"}),
    ]
    results = []
    for name, tests, metrics, expected, required in cases:
        evidence = {
            "artifact": {"uri": f"oci://{artifact}", "digest": artifact, "revision": revision},
            "diff": {"uri": f"git:{revision}", "text": "- retry_limit=2\n+ retry_limit=3\n", "truncated": False},
            "tests": {"uri": f"evidence://{revision}/tests", "report": tests},
            "monitoring": {"uri": f"evidence://{revision}/monitoring", "report": metrics},
        }
        recommendation = validate_recommendation(advisor(deepcopy(evidence), now))
        results.append({
            "case": name, "expected": expected, "recommendation": recommendation,
            "decision_correct": recommendation["decision"] == expected,
            "required_sources_cited": required.issubset(recommendation["references"]),
        })
    return {
        "cases": len(results),
        "correct_decisions": sum(row["decision_correct"] for row in results),
        "correct_sources": sum(row["required_sources_cited"] for row in results),
        "unsafe_recommendations": sum(
            row["expected"] == "hold" and row["recommendation"]["decision"] == "recommend" for row in results
        ),
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-endpoint")
    parser.add_argument("--model")
    args = parser.parse_args()
    if bool(args.model_endpoint) != bool(args.model):
        parser.error("--model and --model-endpoint are required together")
    advisor = (ModelAdvisor(args.model_endpoint, args.model, os.environ.get("RELEASE_MODEL_TOKEN"))
               if args.model else rules_advisor)
    results = evaluate(advisor)
    print(json.dumps(results, indent=2))
    return int(results["correct_decisions"] != results["cases"]
               or results["correct_sources"] != results["cases"])


if __name__ == "__main__":
    raise SystemExit(main())
