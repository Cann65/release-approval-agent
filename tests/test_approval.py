import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from approval_agent.demo import DemoRuntime, DropReply, run, specimen
from approval_agent.evaluate import evaluate
from approval_agent.ledger import Ledger
from approval_agent.review import ModelAdvisor, collect, operation_id, rules_advisor, utc_now
from approval_agent.target import Client, Target, serve


class CountingRuntime(DemoRuntime):
    def __init__(self):
        super().__init__()
        self.starts = 0

    def start(self, operation, scope):
        self.starts += 1
        return super().start(operation, scope)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = utc_now()
        self.ledger = Ledger(self.root / "ledger.db", lambda: self.now)
        self.ledger.set_role("alice", "staging", True)
        self.repository = "registry.example/checkout"
        self.image = self.repository + "@sha256:" + "a" * 64
        self.other_image = self.repository + "@sha256:" + "b" * 64
        self.revision = "c" * 40
        self.runtime = CountingRuntime()
        self.target = Target(self.root / "target.db", self.repository, self.runtime)

    def evidence(self, tests=True):
        return {
            "artifact": {"digest": self.image, "revision": self.revision, "uri": f"oci://{self.image}"},
            "diff": {"text": "+service-health-check", "uri": "git:fixture", "truncated": False},
            "tests": {"report": {
                "status": "passed", "revision": self.revision, "artifact": self.image, "checks": 2
            } if tests else None, "uri": "evidence://fixture/tests"},
            "monitoring": {"report": {"service": "checkout", "environment": "staging",
                                      "requests": 9500, "errors": 10, "collected_at": self.now.isoformat()},
                           "uri": "evidence://fixture/monitoring"},
        }

    def server(self, server):
        self.enterContext(server)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_review_and_approval_bind_exact_digest(self):
        repo, revisions = specimen(self.root)
        evidence = collect(repo, revisions[0], revisions[1], self.image,
                           self.evidence()["tests"]["report"] | {"revision": revisions[1]},
                           self.evidence()["monitoring"]["report"])
        review = self.ledger.review(evidence, rules_advisor)
        self.assertIn("+42", review["evidence"]["diff"]["text"])
        self.assertIn("42", review["recommendation"]["summary"])
        with self.assertRaises(ValueError):
            self.ledger.approve(review["id"], "alice", self.other_image)
        with self.assertRaises(PermissionError):
            self.ledger.approve(review["id"], "bob", self.image)
        self.ledger.approve(review["id"], "alice", self.image)
        self.assertEqual(self.ledger.dispatch(review["id"], self.target)["scope"]["artifact"], self.image)

    def test_unsafe_model_recommendation_remains_visible_and_is_blocked(self):
        review = self.ledger.review(self.evidence(False), lambda *_: {
            "decision": "recommend", "summary": "Staging metrics look healthy.", "references": ["monitoring"]
        })
        self.ledger.approve(review["id"], "alice", self.image)
        blocked = self.ledger.dispatch(review["id"], self.target)
        self.assertEqual(blocked["recommendation"]["decision"], "recommend")
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["result"]["blockers"][0]["source"], "tests")
        self.assertEqual(self.runtime.starts, 0)

    def test_advisor_cannot_mutate_the_bound_evidence(self):
        def advisor(evidence, _):
            evidence["tests"]["report"] = self.evidence()["tests"]["report"]
            return {"decision": "recommend", "summary": "All tests passed.", "references": ["tests"]}

        review = self.ledger.review(self.evidence(False), advisor)
        self.assertIsNone(review["evidence"]["tests"]["report"])
        self.ledger.approve(review["id"], "alice", self.image)
        self.assertEqual(self.ledger.dispatch(review["id"], self.target)["state"], "blocked")

    def test_current_role_is_checked_after_approval_and_audited(self):
        review = self.ledger.review(self.evidence(), rules_advisor)
        self.ledger.approve(review["id"], "alice", self.image)
        self.ledger.set_role("alice", "staging", False)
        self.assertEqual(self.ledger.dispatch(review["id"], self.target)["state"], "blocked")
        audit = self.ledger.audit(review["id"])
        self.assertEqual([row["event"] for row in audit][-3:],
                         ["approval_granted", "role_revoked", "dispatch_blocked"])
        self.assertEqual(audit[-1]["detail"]["blockers"][0]["source"], "roles")
        self.assertEqual(self.runtime.starts, 0)

    def test_stale_telemetry_is_rechecked_at_execution(self):
        review = self.ledger.review(self.evidence(), rules_advisor)
        self.ledger.approve(review["id"], "alice", self.image)
        self.now += timedelta(minutes=31)
        blocked = self.ledger.dispatch(review["id"], self.target)
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["result"]["blockers"][0]["source"], "monitoring")

    def test_response_loss_reconciles_over_http_after_restart(self):
        endpoint = self.server(serve(self.target, "synthetic-token"))
        client = Client(endpoint, "synthetic-token")
        review = self.ledger.review(self.evidence(), rules_advisor)
        self.ledger.approve(review["id"], "alice", self.image)
        pending = self.ledger.dispatch(review["id"], DropReply(client))
        self.assertEqual(pending["state"], "unknown")
        restored = Ledger(self.ledger.database, lambda: self.now)
        final = restored.dispatch(review["id"], client)
        self.assertEqual(final["state"], "deployed")
        self.assertEqual(pending["operation"], final["operation"])
        self.assertEqual(restored.dispatch(review["id"], client)["state"], "deployed")
        self.assertEqual(self.runtime.starts, 1)

    def test_concurrent_dispatch_and_target_retries_start_one_container(self):
        review = self.ledger.review(self.evidence(), rules_advisor)
        self.ledger.approve(review["id"], "alice", self.image)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.ledger.dispatch(review["id"], self.target), range(6)))
        result = self.ledger.get(review["id"])
        self.assertEqual(result["state"], "deployed")
        self.target.apply(result["operation"], review["scope"])
        with self.assertRaises(ValueError):
            self.target.apply("f" * 64, review["scope"])
        stage = {**review["scope"], "environment": "production"}
        with self.assertRaises(ValueError):
            self.target.apply(operation_id(stage), stage)
        self.assertEqual(self.runtime.starts, 1)

    def test_model_adapter_sees_evidence_and_returns_a_cited_answer(self):
        calls = []

        class Provider(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(json.loads(data["messages"][1]["content"]))
                advice = {"decision": "hold", "summary": "Test result missing.", "references": ["tests"]}
                result = {"choices": [{"message": {"content": json.dumps(advice)}}]}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())

            def log_message(self, *args):
                pass

        endpoint = self.server(ThreadingHTTPServer(("127.0.0.1", 0), Provider))
        advisor = ModelAdvisor(endpoint + "/v1/chat/completions", "synthetic")
        review = self.ledger.review(self.evidence(False), advisor)
        self.assertEqual(review["recommendation"]["references"], ["tests"])
        self.assertIsNone(calls[0]["evidence"]["tests"]["report"])

    def test_demo_and_evaluation(self):
        report = run(self.root)
        self.assertEqual(report["deployed"]["observed_version"], "42")
        self.assertNotEqual(report["tag_after_approval"], report["deployed"]["scope"]["artifact"])
        self.assertEqual(report["deployed"]["states"], ["unknown", "deployed"])
        result = evaluate(rules_advisor)
        self.assertEqual(result["correct_decisions"], 7)
        self.assertEqual(result["correct_sources"], 7)
        self.assertEqual(result["unsafe_recommendations"], 0)
