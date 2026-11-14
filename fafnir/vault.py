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


_key_cache: dict[str, bytes] = {}


def get_key(create: bool = True) -> bytes:
    """The 32-byte data key. Created on first use (and a one-time recovery copy written).
    Unwrapped once per run and kept in this process's memory only."""
    if "k" in _key_cache:
        return _key_cache["k"]
    if KEY_FILE.exists():
        _key_cache["k"] = _dpapi(KEY_FILE.read_bytes(), protect=False)
        return _key_cache["k"]
    if not create:
        raise OSError("No vault key yet.")
    key = os.urandom(32)
    _key_cache["k"] = key
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


def _write_recovery(key: bytes) -> None:
    RECOVERY_FILE.write_text(
        "FAFNIR RECOVERY KEY\n===================\n"
        "This key unlocks Fafnir's encrypted data and backups on ANY computer.\n\n"
        "1. Copy the key below into a password manager, or onto a USB stick kept somewhere safe.\n"
        "2. Then DELETE this file.\n\n"
        f"{base64.urlsafe_b64encode(key).decode()}\n\n"
        "Restore a backup with:  python main.py --restore-backup <file> --key <this key>\n", encoding="utf-8")


def rotate(files: list) -> dict:
    """Replace the data key. Every file is decrypted with the old key and re-encrypted with a new
    one; nothing is swapped in until every new copy has been proven to decrypt. The old recovery
    key stops working for these files; a new RECOVERY_KEY.txt is written. (Old backups keep
    needing the old key.) Run with Fafnir closed."""
    from pathlib import Path
    old = get_key(create=False)
    new = os.urandom(32)
    staged = []
    for f in (Path(x) for x in files):
        if not f.exists():
            continue
        data = f.read_bytes()
        if data.startswith(MAGIC):                       # a whole encrypted file
            out = encrypt(decrypt(data, old), new)
            check = lambda b, plain=decrypt(data, old): decrypt(b, new) == plain
        else:                                            # a log encrypted line by line
            lines = data.decode("utf-8").splitlines()
            conv = [LINE_PREFIX + base64.b64encode(encrypt(decrypt(base64.b64decode(ln[len(LINE_PREFIX):]), old), new)).decode("ascii")
                    if ln.startswith(LINE_PREFIX) else ln for ln in lines]
            out = ("\n".join(conv) + "\n").encode("utf-8")
            check = lambda b, n=len(lines): len(b.decode("utf-8").splitlines()) == n
        tmp = f.with_name(f.name + ".rotating")
        tmp.write_bytes(out)
        if not check(tmp.read_bytes()):
            for t, _ in staged + [(tmp, f)]:
                t.unlink(missing_ok=True)
            raise OSError(f"Rotation stopped: {f.name} didn't verify. Nothing was changed.")
        staged.append((tmp, f))
    key_tmp = KEY_FILE.with_name(KEY_FILE.name + ".rotating")
    key_tmp.write_bytes(_dpapi(new, protect=True))
    if _dpapi(key_tmp.read_bytes(), protect=False) != new:
        raise OSError("Rotation stopped: the new key didn't verify. Nothing was changed.")
    os.replace(key_tmp, KEY_FILE)                        # commit point
    for tmp, f in staged:
        os.replace(tmp, f)
    _key_cache["k"] = new
    _write_recovery(new)
    return {"files_re_encrypted": len(staged)}


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
LINE_PREFIX = "enc1:"


def is_encrypted(path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def read_text(path) -> str:
    """A text file that may be encrypted (new) or plain (written before encryption was added)."""
    from pathlib import Path
    data = Path(path).read_bytes()
    return decrypt(data).decode("utf-8") if data.startswith(MAGIC) else data.decode("utf-8")


def write_text(path, text: str) -> None:
    """Write a text file encrypted, atomically."""
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(encrypt(text.encode("utf-8")))
    os.replace(tmp, p)


def encrypt_line(text: str) -> str:
    """One line of an append-only log, encrypted on its own (so appending stays cheap)."""
    return LINE_PREFIX + base64.b64encode(encrypt(text.encode("utf-8"))).decode("ascii")


def decrypt_line(line: str) -> str:
    """Reverse of encrypt_line; plain lines (from before) pass through unchanged."""
    return decrypt(base64.b64decode(line[len(LINE_PREFIX):])).decode("utf-8") if line.startswith(LINE_PREFIX) else line
