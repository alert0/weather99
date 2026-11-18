"""Short-lived QWeather JWTs signed locally with Ed25519; private keys never leave disk."""

import base64
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path


def base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class JWTSigner:
    developer_id: str
    project_id: str
    credential_id: str
    private_key_file: Path = field(repr=False)

    def __post_init__(self):
        if not re.fullmatch(r"Q[A-Z0-9]{9}", self.developer_id):
            raise ValueError("invalid QWeather developer ID")
        for value in (self.project_id, self.credential_id):
            if not re.fullmatch(r"[A-Z0-9]{10}", value):
                raise ValueError("invalid QWeather project or credential ID")

    def token(self) -> str:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            raise ValueError(
                "install JWT signing support: pip install -e .[qweather-jwt]"
            ) from None
        key = serialization.load_pem_private_key(self.private_key_file.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError("QWeather requires an Ed25519 private key")
        now = int(time.time())
        header = {"alg": "EdDSA", "kid": self.credential_id}
        payload = {
            "iss": self.developer_id,
            "sub": self.project_id,
            "iat": now - 30,
            "exp": now + 900,
        }
        message = ".".join(
            base64url(json.dumps(value, separators=(",", ":")).encode())
            for value in (header, payload)
        )
        return message + "." + base64url(key.sign(message.encode("ascii")))
