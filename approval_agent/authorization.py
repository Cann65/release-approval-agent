"""Short-lived authorization for a single staging deployment."""

import hmac
import re
import time

from .review import canonical


class DeploymentAuthorization:
    def __init__(self, key, clock=time.time):
        if len(key) != 32:
            raise ValueError("the deployment signing key must be 32 bytes")
        self.key = key
        self.clock = clock

    @classmethod
    def from_hex(cls, value):
        if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("STAGING_SIGNING_KEY must contain exactly 64 hexadecimal characters")
        return cls(bytes.fromhex(value))

    def signature(self, operation, scope, issued_at):
        message = canonical({"operation": operation, "scope": scope, "issued_at": issued_at})
        return hmac.digest(self.key, b"release-approval-agent/deploy/v1\x00" + message.encode(), "sha256").hex()

    def issue(self, operation, scope):
        issued_at = int(self.clock())
        return {"issued_at": issued_at, "signature": self.signature(operation, scope, issued_at)}

    def accepts(self, operation, scope, authorization):
        if not isinstance(authorization, dict) or set(authorization) != {"issued_at", "signature"}:
            return False
        issued_at = authorization["issued_at"]
        signature = authorization["signature"]
        return (
            type(issued_at) is int
            and 0 <= int(self.clock()) - issued_at < 60
            and isinstance(signature, str)
            and re.fullmatch(r"[a-f0-9]{64}", signature) is not None
            and hmac.compare_digest(signature, self.signature(operation, scope, issued_at))
        )
