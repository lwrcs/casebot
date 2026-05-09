import base64
import os

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# 32 random bytes, base64-encoded. Generate once with:
#   python -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
_MASTER_KEY: bytes | None = None


def _master_key() -> bytes:
    global _MASTER_KEY
    if _MASTER_KEY is None:
        raw = os.environ["ENCRYPTION_MASTER_KEY"]
        _MASTER_KEY = base64.urlsafe_b64decode(raw.encode())
        if len(_MASTER_KEY) != 32:
            raise ValueError("ENCRYPTION_MASTER_KEY must be 32 bytes base64-encoded")
    return _MASTER_KEY


def _derive_key(discord_id: str) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"casebot:user:{discord_id}".encode(),
    )
    return base64.urlsafe_b64encode(hkdf.derive(_master_key()))


def encrypt(discord_id: str, plaintext: str) -> bytes:
    return Fernet(_derive_key(discord_id)).encrypt(plaintext.encode())


def decrypt(discord_id: str, ciphertext: bytes) -> str:
    return Fernet(_derive_key(discord_id)).decrypt(ciphertext).decode()
