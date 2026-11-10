from __future__ import annotations

import json
from collections import deque
from threading import RLock
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm


class SigningKeyStore:
    """Keep the active RSA key and one previous key to model safe key rotation."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._keys: deque[tuple[str, rsa.RSAPrivateKey]] = deque(maxlen=2)
        self.rotate()

    @property
    def current_kid(self) -> str:
        with self._lock:
            return self._keys[0][0]

    def rotate(self) -> str:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        kid = f"mock-{uuid4().hex[:12]}"
        with self._lock:
            self._keys.appendleft((kid, private_key))
        return kid

    def jwks(self) -> dict[str, list[dict]]:
        with self._lock:
            keys = list(self._keys)
        documents = []
        for kid, private_key in keys:
            document = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
            document.update({"kid": kid, "use": "sig", "alg": "RS256"})
            documents.append(document)
        return {"keys": documents}

    def sign(self, claims: dict, *, publish_key: bool = True) -> str:
        if publish_key:
            with self._lock:
                kid, private_key = self._keys[0]
        else:
            kid = f"unknown-{uuid4().hex[:12]}"
            private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return jwt.encode(
            claims,
            private_key,
            algorithm="RS256",
            headers={"kid": kid, "typ": "JWT"},
        )
