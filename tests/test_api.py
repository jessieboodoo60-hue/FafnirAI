"""Tests for the app API: sign-in, MFA, lockout, token rotation, permissions, live events, approvals.

    .venv\\Scripts\\python -m pytest tests\\test_api.py -q

Uses a throwaway account database and a stand-in for the engine; nothing touches the real Fafnir,
and nothing is sent anywhere.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fafnir import killswitch  # noqa: E402
from fafnir.server import api as api_mod, auth, hub  # noqa: E402

PW = "correct horse battery"


class FakeEngine:
    def __init__(self):
        self.pending_confirm = None
        self.answers = []
        self.busy = False
        self.state = "idle"
        self.agent = object()
        self.cfg = type("C", (), {"model": type("M", (), {"provider": "test"})()})()
        self.submitted = []

    def submit_text(self, text, source="", reply=None):
        self.submitted.append((text, source))
        threading.Timer(0.05, lambda: reply(f"echo: {text}")).start()

    def answer_confirm(self, approved):
        self.answers.append(approved)
        self.pending_confirm = None

    def interrupt(self):
        pass


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "_log", lambda *a, **k: None)          # keep the real action log clean
    monkeypatch.setattr(killswitch, "_save", lambda: None)               # and the real kill-switch file
    monkeypatch.setattr(killswitch, "_log", lambda *a, **k: None)
    killswitch._state["engaged"] = False
    store = auth.AuthStore(tmp_path / "users.db")
    secret = store.add_user("owner", PW, "owner")
    store.add_user("guest", PW, "viewer")
    eng = FakeEngine()
    client = TestClient(api_mod.create_app(store, eng))
    yield client, store, secret, eng
    killswitch._state["engaged"] = False


def login(client, secret, user="owner", **kw):
    body = {"username": user, "password": PW, "device": "test phone", **kw}
    if secret and "code" not in kw:
        body["code"] = auth.totp(secret)
    return client.post("/auth/login", json=body)


def bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_password_stored_hashed(env, tmp_path):
    _, store, _, _ = env
    rows = sqlite3.connect(tmp_path / "users.db").execute("SELECT pw_hash, totp_enc FROM users").fetchall()
    assert all(PW not in r[0] and r[0].startswith("scrypt$") for r in rows)
    assert rows[0][1] and "FAFNIR-ENC" not in rows[0][1]          # TOTP secret stored encrypted (base64 of ciphertext)


def test_weak_password_refused(env):
    _, store, _, _ = env
    with pytest.raises(ValueError):
        store.add_user("weak", "short", "owner")


def test_wrong_password_and_lockout(env):
    client, _, secret, _ = env
    for _ in range(5):
        assert client.post("/auth/login", json={"username": "owner", "password": "nope-nope-nope"}).status_code == 401
    r = login(client, secret)
    assert r.status_code == 429, "locked after five failures, even with the right password"


def test_owner_needs_authenticator_code(env):
    client, _, secret, _ = env
    r = client.post("/auth/login", json={"username": "owner", "password": PW})
    assert r.status_code == 401 and r.json()["mfa_required"]
    assert login(client, secret, code="000000").status_code == 401
    assert login(client, secret).status_code == 200


def test_viewer_has_no_code_but_cannot_control(env):
    client, _, _, _ = env
    r = login(client, None, user="guest")
    assert r.status_code == 200
    tok = r.json()["access_token"]
    assert client.get("/api/status", headers=bearer(tok)).status_code == 200
    assert client.post("/api/killswitch", json={"engage": True}, headers=bearer(tok)).status_code == 403
    assert client.post("/api/chat", json={"text": "hi"}, headers=bearer(tok)).status_code == 403
    assert client.put("/api/integrations/gemini", json={"enabled": True}, headers=bearer(tok)).status_code == 403


def test_every_api_route_needs_a_token(env):
    client, _, _, _ = env
    for method, path in [("get", "/api/status"), ("get", "/api/now"), ("get", "/api/health"), ("post", "/api/chat"),
                         ("get", "/api/approvals"), ("post", "/api/killswitch"), ("get", "/api/integrations"),
                         ("get", "/auth/sessions"), ("post", "/auth/logout-all")]:
        r = getattr(client, method)(path, headers=bearer("made-up-token"))
        assert r.status_code == 401, (method, path, r.status_code)
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_refresh_rotates_and_reuse_ends_the_session(env):
    client, _, secret, _ = env
    first = login(client, secret).json()
    second = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]}).json()
    assert second["refresh_token"] != first["refresh_token"]
    assert client.get("/api/status", headers=bearer(second["access_token"])).status_code == 200
    stolen = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert stolen.status_code == 401 and "used twice" in stolen.json()["detail"]
    assert client.get("/api/status", headers=bearer(second["access_token"])).status_code == 401, "whole session ended"
    assert client.post("/auth/refresh", json={"refresh_token": second["refresh_token"]}).status_code == 401


def test_access_token_expires(env, monkeypatch):
    client, _, secret, _ = env
    tok = login(client, secret).json()["access_token"]
    real = time.time
    monkeypatch.setattr(auth.time, "time", lambda: real() + auth.ACCESS_SECONDS + 5)
    assert client.get("/api/status", headers=bearer(tok)).status_code == 401


def test_logout_and_logout_all(env):
    client, _, secret, _ = env
    a = login(client, secret).json()
    b = login(client, secret).json()
    assert len(client.get("/auth/sessions", headers=bearer(a["access_token"])).json()["sessions"]) == 2
    assert client.post("/auth/logout", headers=bearer(a["access_token"])).status_code == 200
    assert client.get("/api/status", headers=bearer(a["access_token"])).status_code == 401
    assert client.get("/api/status", headers=bearer(b["access_token"])).status_code == 200
    c = login(client, secret).json()
    assert client.post("/auth/logout-all", headers=bearer(c["access_token"])).json()["devices_signed_out"] == 2
    assert client.get("/api/status", headers=bearer(b["access_token"])).status_code == 401


def test_password_change_signs_everyone_out(env):
    client, store, secret, _ = env
    tok = login(client, secret).json()["access_token"]
    store.set_password("owner", "another long password")
    assert client.get("/api/status", headers=bearer(tok)).status_code == 401


def test_chat_goes_to_the_engine(env):
    client, _, secret, eng = env
    tok = login(client, secret).json()["access_token"]
    r = client.post("/api/chat", json={"text": "what time is it"}, headers=bearer(tok))
    assert r.json()["reply"] == "echo: what time is it"
    assert eng.submitted == [("what time is it", "phone:app")]
    assert client.post("/api/chat", json={"text": "x" * 5000}, headers=bearer(tok)).status_code == 422


def test_approvals_and_kill_switch(env):
    client, _, secret, eng = env
    tok = login(client, secret).json()["access_token"]
    assert client.get("/api/approvals", headers=bearer(tok)).json()["pending"] == []
    eng.pending_confirm = "Send the email to Sam."
    assert client.get("/api/approvals", headers=bearer(tok)).json()["pending"][0]["action"] == "Send the email to Sam."
    assert client.post("/api/approvals/current", json={"approve": False}, headers=bearer(tok)).json()["approved"] is False
    assert eng.answers == [False]
    assert client.post("/api/killswitch", json={"engage": True}, headers=bearer(tok)).json()["kill_switch"] is True
    assert client.post("/api/killswitch", json={"engage": False}, headers=bearer(tok)).json()["kill_switch"] is False


def test_integrations_need_a_key_first(env):
    client, _, secret, _ = env
    tok = login(client, secret).json()["access_token"]
    items = {i["id"]: i for i in client.get("/api/integrations", headers=bearer(tok)).json()["integrations"]}
    assert "higgsfield" in items and items["higgsfield"]["active"] is False
    r = client.put("/api/integrations/higgsfield", json={"enabled": True}, headers=bearer(tok))
    if not items["higgsfield"]["ready"]:
        assert r.status_code == 400 and "HIGGSFIELD_API_KEY" in r.json()["detail"]


def test_websocket_needs_token_then_streams_events(env):
    client, _, secret, _ = env
    with client.websocket_connect("/api/events") as ws:
        ws.send_json({"token": "made-up"})
        with pytest.raises(Exception):
            ws.receive_json()
    tok = login(client, secret).json()["access_token"]
    with client.websocket_connect("/api/events") as ws:
        ws.send_json({"token": tok, "after": 10 ** 9})
        assert ws.receive_json()["type"] == "hello"
        hub.publish("notice", text="Backup finished.")
        ev = ws.receive_json()
        assert ev["type"] == "notice" and ev["text"] == "Backup finished."


def test_rate_limit(env, monkeypatch):
    client, _, secret, _ = env
    monkeypatch.setattr(api_mod, "RATE_PER_MINUTE", 5)
    tok = login(client, secret).json()["access_token"]
    codes = [client.get("/api/now", headers=bearer(tok)).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and 429 in codes[5:]
