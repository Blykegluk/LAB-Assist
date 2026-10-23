"""Valeurs « scellées » : chiffrées avec une clé que seul le serveur possède.

Le dépôt est public et les variables d'environnement de l'hébergeur ne sont pas toujours modifiables.
Pour transmettre une valeur secrète (par ex. l'adresse de la base), on la fait chiffrer par le serveur
lui-même (POST /cash/admin/seal) et on versionne le résultat : sans la clé, il est illisible.

Clé : dérivée de CASH_SEAL_KEY, ou à défaut d'OPENAI_API_KEY (déjà présent sur le serveur, jamais
versionné). Si cette variable change, les valeurs scellées deviennent illisibles et le code revient
aux variables d'environnement habituelles.

Chiffrement : flux HMAC-SHA256 en mode compteur + étiquette HMAC-SHA256 (bibliothèque standard).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Optional

PREFIX = "v1."


def _keys() -> Optional[tuple[bytes, bytes]]:
    material = os.getenv("CASH_SEAL_KEY") or os.getenv("OPENAI_API_KEY")
    if not material:
        return None
    base = hashlib.sha256(("lab-assist-seal:" + material).encode("utf-8")).digest()
    return hmac.new(base, b"enc", hashlib.sha256).digest(), hmac.new(base, b"mac", hashlib.sha256).digest()


def available() -> bool:
    return _keys() is not None


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def seal(plaintext: str) -> str:
    keys = _keys()
    if not keys:
        raise RuntimeError("Aucune clé de scellement disponible (CASH_SEAL_KEY ou OPENAI_API_KEY)")
    enc, mac = keys
    nonce = os.urandom(16)
    pt = plaintext.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(pt, _stream(enc, nonce, len(pt))))
    tag = hmac.new(mac, nonce + ct, hashlib.sha256).digest()
    return PREFIX + base64.urlsafe_b64encode(nonce + ct + tag).decode("ascii")


def unseal(token: Optional[str]) -> Optional[str]:
    """Retourne la valeur en clair, ou None si absente, altérée ou scellée avec une autre clé."""
    if not token or not token.startswith(PREFIX):
        return None
    keys = _keys()
    if not keys:
        return None
    enc, mac = keys
    try:
        raw = base64.urlsafe_b64decode(token[len(PREFIX):].encode("ascii"))
    except Exception:
        return None
    if len(raw) < 16 + 32:
        return None
    nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(mac, nonce + ct, hashlib.sha256).digest()):
        return None
    pt = bytes(a ^ b for a, b in zip(ct, _stream(enc, nonce, len(ct))))
    return pt.decode("utf-8")
