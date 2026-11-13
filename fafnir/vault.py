"""Keys for encrypting Fafnir's data at rest.

A random 256-bit key is created once and stored in data/vault.key, wrapped by Windows DPAPI:
only this Windows account on this PC can unwrap it (Windows protects DPAPI secrets with your
sign-in, and with the TPM where the account uses one). The key itself never appears in logs,
prompts or tool results.

Because a DPAPI-wrapped key dies with the Windows profile (reinstall, dead disk), the raw key
is also written ONCE to RECOVERY_KEY.txt in the project folder, for the owner to move somewhere
safe (password manager, USB stick) and delete. With it, backups can be restored on any PC.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import os

from .config import ROOT

KEY_FILE = ROOT / "data" / "vault.key"
RECOVERY_FILE = ROOT / "RECOVERY_KEY.txt"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("Windows couldn't " + ("protect" if protect else "unlock") + " the vault key "
                      "(a different Windows account or PC than the one that created it?).")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def get_key(create: bool = True) -> bytes:
    """The 32-byte data key. Created on first use (and a one-time recovery copy written)."""
    if KEY_FILE.exists():
        return _dpapi(KEY_FILE.read_bytes(), protect=False)
    if not create:
        raise OSError("No vault key yet.")
    key = os.urandom(32)
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_bytes(_dpapi(key, protect=True))
    RECOVERY_FILE.write_text(
        "FAFNIR RECOVERY KEY\n"
        "===================\n"
        "This key unlocks Fafnir's encrypted backups on ANY computer. Anyone who has it and a\n"
        "backup file can read your data (API keys, Google login, memory, conversations).\n\n"
        "1. Copy the key below into a password manager, or onto a USB stick kept somewhere safe.\n"
        "2. Then DELETE this file.\n\n"
        f"{base64.urlsafe_b64encode(key).decode()}\n\n"
        "Restore with:  python main.py --restore-backup <file> --key <this key>\n",
        encoding="utf-8")
    return key


def key_from_recovery(text: str) -> bytes:
    key = base64.urlsafe_b64decode(text.strip().encode())
    if len(key) != 32:
        raise ValueError("That isn't a Fafnir recovery key.")
    return key


def encrypt(data: bytes, key: bytes | None = None) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key or get_key()).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes, key: bytes | None = None) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not blob.startswith(MAGIC):
        raise ValueError("Not a Fafnir encrypted file.")
    nonce, body = blob[len(MAGIC):len(MAGIC) + 12], blob[len(MAGIC) + 12:]
    return AESGCM(key or get_key(create=False)).decrypt(nonce, body, MAGIC)  # raises if tampered or wrong key


MAGIC = b"FAFNIR-ENC-1\n"
