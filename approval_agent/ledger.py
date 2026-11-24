"""Durable approval, audit, and dispatch state for one staging service."""

import json
import sqlite3
from contextlib import closing, contextmanager
from urllib.error import HTTPError

from .review import ARTIFACT, canonical, digest, operation_id, policy, utc_now, validate_recommendation


class Ledger:
    def __init__(self, database, clock=utc_now):
        self.database = database
        self.clock = clock
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, evidence TEXT NOT NULL,
                    recommendation TEXT NOT NULL, state TEXT NOT NULL, approver TEXT,
                    operation TEXT, result TEXT
                );
                CREATE TABLE IF NOT EXISTS roles (
                    actor TEXT NOT NULL, environment TEXT NOT NULL, PRIMARY KEY (actor, environment)
                );
                CREATE TABLE IF NOT EXISTS audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
                    review_id TEXT, actor TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL
                );
            """)

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.database, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db

    def event(self, db, review, actor, name, detail):
        db.execute("INSERT INTO audit(time,review_id,actor,event,detail) VALUES (?,?,?,?,?)",
                   (self.clock().isoformat(), review, actor, name, canonical(detail)))

    def set_role(self, actor, environment, allowed):
        if not actor or environment != "staging":
            raise ValueError("expected an actor and the staging environment")
        with self.transaction() as db:
            if allowed:
                db.execute("INSERT OR IGNORE INTO roles VALUES (?,?)", (actor, environment))
            else:
                db.execute("DELETE FROM roles WHERE actor=? AND environment=?", (actor, environment))
            self.event(db, None, "identity-admin", "role_granted" if allowed else "role_revoked",
                       {"actor": actor, "environment": environment})

    def can_approve(self, db, actor, environment):
        return db.execute("SELECT 1 FROM roles WHERE actor=? AND environment=?", (actor, environment)).fetchone() is not None

    def review(self, evidence, advisor):
        artifact = evidence["artifact"]["digest"]
        if not ARTIFACT.fullmatch(artifact):
            raise ValueError("review requires a valid artifact")
        snapshot = canonical(evidence)
        scope = {"artifact": artifact, "service": "checkout", "environment": "staging", "evidence_sha": digest(evidence)}
        review_id = digest(scope)
        with self.transaction() as db:
            prior = db.execute("SELECT 1 FROM reviews WHERE id=?", (review_id,)).fetchone()
        if prior is None:
            recommendation = validate_recommendation(advisor(json.loads(snapshot), self.clock()))
            with self.transaction() as db:
                cursor = db.execute("INSERT OR IGNORE INTO reviews VALUES (?,?,?,?,?,NULL,NULL,NULL)",
                                    (review_id, canonical(scope), snapshot,
                                     canonical(recommendation), "reviewed"))
                if cursor.rowcount:
                    self.event(db, review_id, "review-agent", "review_recorded",
                               {"scope": scope, "recommendation": recommendation})
        return self.get(review_id)

    def get(self, review_id):
        with self.transaction() as db:
            return self.read(db, review_id)

    def read(self, db, review_id):
        row = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise KeyError(review_id)
        return {
            **dict(row),
            **{key: json.loads(row[key]) for key in ("scope", "evidence", "recommendation", "result") if row[key] is not None},
        }

    def approve(self, review_id, actor, expected_artifact):
        with self.transaction() as db:
            review = self.read(db, review_id)
            scope = review["scope"]
            if scope["artifact"] != expected_artifact:
                raise ValueError("approval artifact differs from the immutable review")
            if not self.can_approve(db, actor, scope["environment"]):
                raise PermissionError("actor lacks the staging approver role")
            if review["state"] == "approved" and review["approver"] == actor:
                return review
            if review["state"] != "reviewed":
                raise ValueError("review already has an approval or decision")
            db.execute("UPDATE reviews SET approver=?,state='approved' WHERE id=?", (actor, review_id))
            self.event(db, review_id, actor, "approval_granted", scope)
            return self.read(db, review_id)

    def claim(self, review_id):
        with self.transaction() as db:
            review = self.read(db, review_id)
            state = review["state"]
            if state != "approved":
                return review, False
            scope = review["scope"]
            evidence = review["evidence"]
            if (scope["artifact"] != evidence["artifact"]["digest"]
                    or scope["evidence_sha"] != digest(evidence) or review_id != digest(scope)):
                blockers = [{"reason": "evidence differs from the immutable review", "source": "evidence"}]
            else:
                blockers = policy(evidence, self.clock())
            if not self.can_approve(db, review["approver"], scope["environment"]):
                blockers.append({"reason": "approver currently lacks the staging role", "source": "roles"})
            if blockers:
                self.write_result(db, review_id, "blocked", {"blockers": blockers}, "dispatch_blocked",
                                  {"scope": scope, "approver": review["approver"], "blockers": blockers})
                return self.read(db, review_id), False
            in_flight = db.execute("SELECT 1 FROM reviews WHERE state IN ('dispatching','unknown')").fetchone()
            if in_flight:
                self.event(db, review_id, "deployment-executor", "dispatch_deferred",
                           {"reason": "staging already has an unresolved operation"})
                return review, False
            operation = operation_id(scope)
            db.execute("UPDATE reviews SET operation=?,state='dispatching' WHERE id=?", (operation, review_id))
            self.event(db, review_id, "deployment-executor", "dispatch_claimed",
                       {"operation": operation, "scope": scope, "approver": review["approver"]})
            return self.read(db, review_id), True

    def write_result(self, db, review_id, state, result, event, detail):
        db.execute("UPDATE reviews SET state=?,result=? WHERE id=?", (state, canonical(result), review_id))
        self.event(db, review_id, "deployment-executor", event, detail)

    def resolve(self, review_id, reply):
        with self.transaction() as db:
            review = self.read(db, review_id)
            if review["state"] not in ("dispatching", "unknown"):
                return review
            operation, scope = review["operation"], review["scope"]
            if (isinstance(reply, dict) and reply.get("operation") == operation
                    and reply.get("state") == "applied" and reply.get("scope") == scope):
                self.write_result(db, review_id, "deployed", reply, "deployment_confirmed", reply)
            elif review["state"] != "unknown":
                self.write_result(db, review_id, "unknown", {"operation": operation},
                                  "deployment_unconfirmed", {"operation": operation, "scope": scope})
            return self.read(db, review_id)

    def dispatch(self, review_id, target, *, reconcile_only=False):
        review, claimed = (self.get(review_id), False) if reconcile_only else self.claim(review_id)
        if review["state"] not in ("dispatching", "unknown"):
            return review
        try:
            if claimed:
                reply = target.apply(review["operation"], review["scope"])
            else:
                reply = target.lookup(review["operation"])
        except (OSError, HTTPError, ValueError):
            reply = None
        return self.resolve(review_id, reply)

    def audit(self, review_id):
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM audit WHERE review_id=? OR review_id IS NULL ORDER BY sequence",
                (review_id,),
            ).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]
