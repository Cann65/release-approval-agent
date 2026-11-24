"""Separate idempotent staging control plane with a Docker runtime adapter."""

import json
import re
import sqlite3
import subprocess
from contextlib import closing, contextmanager
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic, sleep
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .review import ARTIFACT, NoRedirect, canonical, operation_id


OPERATION = re.compile(r"[a-f0-9]{64}")


class DockerRuntime:
    def name(self, operation):
        return f"approval-demo-{operation[:32]}"

    def inspect(self, operation, scope):
        name = self.name(operation)
        output = subprocess.run(["docker", "inspect", "--type", "container", name],
                                capture_output=True, text=True, timeout=10)
        if output.returncode:
            return None
        info = json.loads(output.stdout)[0]
        if (info["Config"]["Image"] != scope["artifact"]
                or info["Config"]["Labels"].get("release.operation") != operation
                or not info["State"]["Running"]):
            return None
        published = info["NetworkSettings"]["Ports"].get("8080/tcp") or []
        port = next((entry["HostPort"] for entry in published if entry["HostIp"] == "127.0.0.1"), None)
        if port is None:
            return None
        url = f"http://127.0.0.1:{int(port)}/version"
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(url, timeout=1) as response:
                if response.status != 200:
                    return None
        except OSError:
            return None
        return {"container": name, "url": url}

    def start(self, operation, scope):
        subprocess.run(
            ["docker", "run", "--detach", "--pull=always", "--name", self.name(operation),
             "--label", f"release.operation={operation}", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--publish", "127.0.0.1::8080",
             scope["artifact"]],
            check=True, capture_output=True, text=True, timeout=45,
        )
        deadline = monotonic() + 10
        while monotonic() < deadline:
            result = self.inspect(operation, scope)
            if result:
                return result
            sleep(.25)
        return None


class Target:
    def __init__(self, database, repository, runtime):
        self.database, self.repository, self.runtime = database, repository, runtime
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS operations (
                        id TEXT PRIMARY KEY, scope TEXT NOT NULL, result TEXT
                        )""")

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.database, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def validate(self, operation, scope):
        if (not OPERATION.fullmatch(operation) or not isinstance(scope, dict)
                or set(scope) != {"artifact", "service", "environment", "evidence_sha"}):
            raise ValueError("invalid operation or scope")
        if (scope["service"] != "checkout" or scope["environment"] != "staging"
                or not OPERATION.fullmatch(scope["evidence_sha"]) or not ARTIFACT.fullmatch(scope["artifact"])
                or scope["artifact"].split("@", 1)[0] != self.repository or operation != operation_id(scope)):
            raise ValueError("artifact or deployment target falls outside the allowlist")

    def apply(self, operation, scope):
        self.validate(operation, scope)
        with self.transaction() as db:
            prior = db.execute("SELECT scope FROM operations WHERE id=?", (operation,)).fetchone()
            if prior and prior[0] != canonical(scope):
                raise ValueError("operation ID already binds a different scope")
            if prior is None:
                db.execute("INSERT INTO operations VALUES (?,?,NULL)", (operation, canonical(scope)))
        if prior is not None:
            return self.lookup(operation)
        try:
            result = self.runtime.start(operation, scope)
        except (subprocess.SubprocessError, OSError):
            result = None
        return self.record(operation, scope, result)

    def record(self, operation, scope, result):
        if result is not None:
            with self.transaction() as db:
                db.execute("UPDATE operations SET result=? WHERE id=? AND result IS NULL",
                           (canonical(result), operation))
        return {"operation": operation, "scope": scope, "state": "applied" if result is not None else "pending",
                "deployment": result}

    def lookup(self, operation):
        if not OPERATION.fullmatch(operation):
            raise ValueError("invalid operation ID")
        with self.transaction() as db:
            row = db.execute("SELECT scope,result FROM operations WHERE id=?", (operation,)).fetchone()
        if row is None:
            return None
        scope = json.loads(row[0])
        result = json.loads(row[1]) if row[1] else self.runtime.inspect(operation, scope)
        return self.record(operation, scope, result)


class Client:
    def __init__(self, endpoint, token, *, authorization=None):
        url = urlsplit(endpoint)
        if (url.scheme != "http" or url.hostname != "127.0.0.1" or url.port is None
                or url.username is not None or url.path not in ("", "/") or url.query or url.fragment):
            raise ValueError("the demo staging control plane must use a loopback URL and port")
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.authorization = authorization

    def apply(self, operation, scope):
        if self.authorization is None:
            raise PermissionError("deployment requires a signing key")
        return self.request(operation, "PUT", {
            "scope": scope,
            "authorization": self.authorization.issue(operation, scope),
        })

    def lookup(self, operation):
        return self.request(operation, "GET")

    def request(self, operation, method, payload=None):
        body = canonical(payload).encode() if payload is not None else None
        request = Request(self.endpoint + "/operations/" + operation, data=body, method=method,
                          headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=60) as response:
            return json.load(response)


def serve(target, token, *, authorization, host="127.0.0.1", port=0):
    if not token or host != "127.0.0.1":
        raise ValueError("the demo requires a bearer token and loopback binding")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self, write):
            if not compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
                return self.reply(401, {"error": "unauthorized"})
            operation = self.path.removeprefix("/operations/")
            if not OPERATION.fullmatch(operation) or self.path != "/operations/" + operation:
                return self.reply(404, {"error": "operation not found"})
            try:
                if write:
                    self.connection.settimeout(5)
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= length <= 4096:
                        raise ValueError("invalid request size")
                    request = json.loads(self.rfile.read(length))
                    if (not isinstance(request, dict) or set(request) != {"scope", "authorization"}
                            or not authorization.accepts(operation, request["scope"], request["authorization"])):
                        return self.reply(403, {"error": "valid deployment authorization required"})
                    result = target.apply(operation, request["scope"])
                else:
                    result = target.lookup(operation)
            except (ValueError, TypeError, RecursionError):
                return self.reply(400, {"error": "invalid operation"})
            except (OSError, subprocess.SubprocessError):
                return self.reply(503, {"error": "operation state unavailable"})
            if result is None:
                return self.reply(404, {"error": "operation not found"})
            return self.reply(200, result)

        def reply(self, code, data):
            body = canonical(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self.handle_request(False)

        def do_PUT(self):
            self.handle_request(True)

    return ThreadingHTTPServer((host, port), Handler)
