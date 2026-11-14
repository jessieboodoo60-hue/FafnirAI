"""Accounts and sign-in for the FafnirAI app API.

- Passwords: scrypt (salted, memory-hard), never stored or logged in plain text.
- Owner accounts must also give a 6-digit code from an authenticator app (TOTP, RFC 6238). The TOTP
  secret is stored encrypted with the vault key.
- Sign-in returns a short-lived access token (15 minutes) and a refresh token (30 days). Every refresh
  replaces the refresh token; presenting an old one again means it was copied, so the whole session is
  revoked. Only SHA-256 hashes of tokens are stored.
- Five wrong passwords or codes for one account (or from one address) within 15 minutes lock sign-in
  for 15 minutes.
- Roles: owner (everything), viewer (read only).

Accounts are created on the PC itself (python main.py --app-user-add NAME), never over the network.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .. import vault

ACCESS_SECONDS = 15 * 60
REFRESH_SECONDS = 30 * 24 * 3600
LOCK_AFTER, LOCK_WINDOW, LOCK_SECONDS = 5, 15 * 60, 15 * 60
ROLES = {"owner": {"read", "chat", "control", "admin"}, "viewer": {"read"}}
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401, mfa_required: bool = False):
        super().__init__(message)
        self.status, self.mfa_required = status, mfa_required


@dataclass
class Principal:
    user_id: int
    username: str
    role: str
    session_id: str

    def can(self, perm: str) -> bool:
        return perm in ROLES.get(self.role, set())


# ---- passwords and codes -----------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${h.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got.hex(), h)
    except Exception:
        return False


def totp(secret_b32: str, at: float | None = None, step: int = 30) -> str:
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    counter = int((at if at is not None else time.time()) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def check_totp(secret_b32: str, code: str) -> bool:
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    now = time.time()
    return len(code) == 6 and any(hmac.compare_digest(totp(secret_b32, now + d * 30), code) for d in (-1, 0, 1))


def _h(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def password_problem(password: str) -> str | None:
    if len(password) < 10:
        return "Use at least 10 characters."
    if password.lower() in ("password12", "1234567890", "qwertyuiop") or len(set(password)) < 5:
        return "That password is too easy to guess."
    return None


# ---- the store ---------------------------------------------------------------------------------
class AuthStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                pw_hash TEXT NOT NULL, role TEXT NOT NULL, totp_enc TEXT, created REAL NOT NULL, disabled INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, device TEXT,
                refresh_hash TEXT, prev_hash TEXT, created REAL, last_used REAL, expires REAL, revoked INTEGER DEFAULT 0,
                ip TEXT);
            CREATE INDEX IF NOT EXISTS s_refresh ON sessions(refresh_hash);
            CREATE INDEX IF NOT EXISTS s_prev ON sessions(prev_hash);
        """)
        self._access: dict[str, tuple[float, Principal]] = {}     # token hash -> (expiry, who); memory only
        self._fails: dict[str, list[float]] = {}

    # ---- accounts (PC-side only) ----
    def add_user(self, username: str, password: str, role: str = "owner") -> str | None:
        """Create an account. Returns the TOTP secret for owners (to put in an authenticator app)."""
        if role not in ROLES:
            raise ValueError("Role must be owner or viewer.")
        problem = password_problem(password)
        if problem:
            raise ValueError(problem)
        secret = base64.b32encode(os.urandom(20)).decode().rstrip("=") if role == "owner" else None
        enc = base64.b64encode(vault.encrypt(secret.encode())).decode() if secret else None
        with self._lock:
            try:
                self._db.execute("INSERT INTO users(username, pw_hash, role, totp_enc, created) VALUES(?,?,?,?,?)",
                                 (username.strip(), hash_password(password), role, enc, time.time()))
            except sqlite3.IntegrityError:
                raise ValueError(f"There is already an account called {username}.")
        return secret

    def set_password(self, username: str, password: str) -> None:
        problem = password_problem(password)
        if problem:
            raise ValueError(problem)
        with self._lock:
            cur = self._db.execute("UPDATE users SET pw_hash=? WHERE username=?", (hash_password(password), username))
            if not cur.rowcount:
                raise ValueError(f"No account called {username}.")
            uid = self._db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
        self.revoke_all(uid)   # a new password signs every device out

    def remove_user(self, username: str) -> None:
        with self._lock:
            row = self._db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            if not row:
                raise ValueError(f"No account called {username}.")
            self.revoke_all(row[0])
            self._db.execute("DELETE FROM users WHERE id=?", (row[0],))

    def users(self) -> list[dict]:
        rows = self._db.execute("SELECT id, username, role, totp_enc IS NOT NULL, created FROM users").fetchall()
        return [{"id": r[0], "username": r[1], "role": r[2], "mfa": bool(r[3]),
                 "created": time.strftime("%Y-%m-%d", time.localtime(r[4]))} for r in rows]

    @staticmethod
    def otpauth_uri(username: str, secret: str) -> str:
        return f"otpauth://totp/FafnirAI:{quote(username)}?secret={secret}&issuer=FafnirAI&digits=6&period=30"

    # ---- lockout ----
    def _locked(self, *keys: str) -> bool:
        now = time.time()
        for k in keys:
            self._fails[k] = [t for t in self._fails.get(k, []) if now - t < max(LOCK_WINDOW, LOCK_SECONDS)]
            recent = [t for t in self._fails[k] if now - t < LOCK_WINDOW]
            if len(recent) >= LOCK_AFTER and now - recent[-1] < LOCK_SECONDS:
                return True
        return False

    def _fail(self, *keys: str) -> None:
        for k in keys:
            self._fails.setdefault(k, []).append(time.time())

    # ---- sign-in and tokens ----
    def login(self, username: str, password: str, code: str = "", device: str = "", ip: str = "") -> dict:
        ukey, ikey = f"u:{(username or '').lower()}", f"ip:{ip}"
        with self._lock:
            if self._locked(ukey, ikey):
                raise AuthError("Too many failed attempts. Try again in 15 minutes.", 429)
            row = self._db.execute("SELECT id, username, pw_hash, role, totp_enc, disabled FROM users WHERE username=?",
                                   ((username or "").strip(),)).fetchone()
            if not row or row[5] or not check_password(password or "", row[2]):
                if not row:
                    check_password(password or "", hash_password("timing-equaliser"))   # same work either way
                self._fail(ukey, ikey)
                raise AuthError("Wrong username or password.")
            uid, uname, _, role, totp_enc, _ = row
            if totp_enc:
                secret = vault.decrypt(base64.b64decode(totp_enc)).decode()
                if not code:
                    raise AuthError("Enter the 6-digit code from your authenticator app.", 401, mfa_required=True)
                if not check_totp(secret, code):
                    self._fail(ukey, ikey)
                    raise AuthError("That code is wrong or expired.", 401, mfa_required=True)
            self._fails.pop(ukey, None)
            sid = secrets.token_urlsafe(12)
            refresh = secrets.token_urlsafe(32)
            now = time.time()
            self._db.execute("INSERT INTO sessions(id, user_id, device, refresh_hash, created, last_used, expires, ip) "
                             "VALUES(?,?,?,?,?,?,?,?)", (sid, uid, (device or "unknown device")[:80], _h(refresh), now, now,
                                                         now + REFRESH_SECONDS, ip))
            return self._issue(Principal(uid, uname, role, sid), refresh)

    def _issue(self, who: Principal, refresh: str) -> dict:
        access = secrets.token_urlsafe(32)
        self._access[_h(access)] = (time.time() + ACCESS_SECONDS, who)
        return {"access_token": access, "expires_in": ACCESS_SECONDS, "refresh_token": refresh,
                "role": who.role, "username": who.username, "session_id": who.session_id}

    def refresh(self, refresh_token: str) -> dict:
        h = _h(refresh_token or "")
        with self._lock:
            row = self._db.execute("SELECT s.id, s.user_id, s.revoked, s.expires, u.username, u.role, u.disabled "
                                   "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.refresh_hash=?", (h,)).fetchone()
            if not row:
                stolen = self._db.execute("SELECT id FROM sessions WHERE prev_hash=?", (h,)).fetchone()
                if stolen:   # an already-used refresh token came back: someone has a copy
                    self._revoke(stolen[0])
                    raise AuthError("This sign-in was used twice, so it has been ended. Sign in again.")
                raise AuthError("Sign in again.")
            sid, uid, revoked, expires, uname, role, disabled = row
            if revoked or disabled or expires < time.time():
                raise AuthError("Sign in again.")
            new = secrets.token_urlsafe(32)
            self._db.execute("UPDATE sessions SET prev_hash=refresh_hash, refresh_hash=?, last_used=? WHERE id=?",
                             (_h(new), time.time(), sid))
            return self._issue(Principal(uid, uname, role, sid), new)

    def principal(self, access_token: str) -> Principal:
        entry = self._access.get(_h(access_token or ""))
        if not entry or entry[0] < time.time():
            self._access.pop(_h(access_token or ""), None)
            raise AuthError("Sign in again." if entry is None else "Session expired; refresh it.")
        who = entry[1]
        row = self._db.execute("SELECT revoked FROM sessions WHERE id=?", (who.session_id,)).fetchone()
        if not row or row[0]:
            raise AuthError("This device was signed out.")
        return who

    def _revoke(self, sid: str) -> None:
        self._db.execute("UPDATE sessions SET revoked=1 WHERE id=?", (sid,))
        for k, (_, who) in list(self._access.items()):
            if who.session_id == sid:
                self._access.pop(k, None)

    def revoke(self, user_id: int, sid: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT 1 FROM sessions WHERE id=? AND user_id=?", (sid, user_id)).fetchone()
            if row:
                self._revoke(sid)
            return bool(row)

    def revoke_all(self, user_id: int) -> int:
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT id FROM sessions WHERE user_id=? AND revoked=0", (user_id,))]
            for sid in ids:
                self._revoke(sid)
            return len(ids)

    def sessions(self, user_id: int) -> list[dict]:
        rows = self._db.execute("SELECT id, device, created, last_used, ip FROM sessions WHERE user_id=? AND revoked=0 "
                                "AND expires>? ORDER BY last_used DESC", (user_id, time.time())).fetchall()
        fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))   # noqa: E731
        return [{"id": r[0], "device": r[1], "signed_in": fmt(r[2]), "last_used": fmt(r[3]), "address": r[4]} for r in rows]
