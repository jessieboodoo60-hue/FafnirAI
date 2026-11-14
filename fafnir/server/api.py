"""The FafnirAI app API: what the phone app talks to.

It listens on 127.0.0.1 only. Reaching it from a phone outside the house will go through a private
network (Tailscale) that forwards to this address; nothing here is opened to the internet.

Every /api route needs a valid access token, and each declares the permission it needs (read, chat,
control, admin). Sign-in, refresh-token reuse and every control action are written to the tamper-evident
action log. Requests are rate-limited per device. The interactive docs pages are switched off.
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import audit, integrations, killswitch
from . import hub
from .auth import AuthError, AuthStore, Principal

RATE_PER_MINUTE = 120
CHAT_TIMEOUT = 180


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    code: str = Field(default="", max_length=12)
    device: str = Field(default="", max_length=80)


class Refresh(BaseModel):
    refresh_token: str = Field(max_length=200)


class Chat(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class Approve(BaseModel):
    approve: bool


class KillSwitch(BaseModel):
    engage: bool


class Toggle(BaseModel):
    enabled: bool


def _log(what: str, who: str = "", ok: bool = True) -> None:
    audit.record({"time": datetime.now().isoformat(timespec="seconds"), "tool": "APP_API",
                  "result": f"{what}" + (f" ({who})" if who else ""), "error": not ok})


def create_app(store: AuthStore, engine=None, health=None) -> FastAPI:
    app = FastAPI(title="FafnirAI", docs_url=None, redoc_url=None, openapi_url=None)
    calls: dict[str, list[float]] = {}

    @app.middleware("http")
    async def harden(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(AuthError)
    async def auth_error(_, e: AuthError):
        return JSONResponse({"detail": str(e), "mfa_required": e.mfa_required}, status_code=e.status)

    def who(request: Request) -> Principal:
        h = request.headers.get("authorization", "")
        p = store.principal(h[7:].strip() if h.lower().startswith("bearer ") else "")
        now = time.monotonic()
        recent = calls.setdefault(p.session_id, [])
        recent[:] = [t for t in recent if now - t < 60]
        if len(recent) >= RATE_PER_MINUTE:
            raise HTTPException(429, "Too many requests; slow down.")
        recent.append(now)
        return p

    def need(perm: str):
        def check(p: Principal = Depends(who)) -> Principal:
            if not p.can(perm):
                raise HTTPException(403, f"Your account can't do that ({perm}).")
            return p
        return check

    def ip(request: Request) -> str:
        return request.client.host if request.client else ""

    # ---- sign-in ------------------------------------------------------------------------------
    @app.post("/auth/login")
    def login(body: Login, request: Request):
        try:
            out = store.login(body.username, body.password, body.code, body.device, ip(request))
        except AuthError as e:
            if not e.mfa_required or body.code:
                _log(f"sign-in failed: {e}", body.username, ok=False)
            raise
        _log(f"signed in on {body.device or 'a device'}", body.username)
        return out

    @app.post("/auth/refresh")
    def refresh(body: Refresh):
        try:
            return store.refresh(body.refresh_token)
        except AuthError as e:
            if "used twice" in str(e):
                _log("refresh token reused: session ended", ok=False)
            raise

    @app.post("/auth/logout")
    def logout(p: Principal = Depends(who)):
        store.revoke(p.user_id, p.session_id)
        _log("signed out", p.username)
        return {"ok": True}

    @app.post("/auth/logout-all")
    def logout_all(p: Principal = Depends(who)):
        n = store.revoke_all(p.user_id)
        _log(f"signed out of all {n} devices", p.username)
        return {"ok": True, "devices_signed_out": n}

    @app.get("/auth/sessions")
    def sessions(p: Principal = Depends(who)):
        return {"current": p.session_id, "sessions": store.sessions(p.user_id)}

    @app.delete("/auth/sessions/{sid}")
    def end_session(sid: str, p: Principal = Depends(who)):
        if not store.revoke(p.user_id, sid):
            raise HTTPException(404, "No such session.")
        _log("signed a device out", p.username)
        return {"ok": True}

    # ---- Fafnir -------------------------------------------------------------------------------
    def need_engine():
        if engine is None:
            raise HTTPException(503, "Fafnir isn't running.")
        return engine

    @app.get("/api/status")
    def status(p: Principal = Depends(need("read"))):
        out = {"name": "Fafnir", "kill_switch": killswitch.engaged(), "you": {"username": p.username, "role": p.role}}
        if engine is not None:
            res = getattr(engine, "resources", None)
            out.update(state=engine.state, busy=engine.busy, pending_approval=engine.pending_confirm,
                       brain=(f"local: {getattr(engine.agent, 'active_model', None) or 'unloaded'}"
                              if hasattr(engine.agent, "use_model") else f"cloud: {engine.cfg.model.provider}"),
                       profile=(res.profile if res else None))
        mon = health or (getattr(engine, "health", None) if engine is not None else None)
        if mon is not None:
            s = mon.summary()
            out["health"] = {"overall": s["overall"], "problems": s["problems"]}
        return out

    @app.get("/api/now")
    def now(p: Principal = Depends(need("read"))):
        return hub.last_now()

    @app.get("/api/health")
    def health_report(p: Principal = Depends(need("read"))):
        mon = health or (getattr(engine, "health", None) if engine is not None else None)
        if mon is None:
            from .. import health as h
            return {"overall": "checked without the live app",
                    "everything": [{"what": r["check"], "status": r["status"], "detail": r["detail"]} for r in h.static_report()]}
        return mon.summary()

    @app.get("/api/events/recent")
    def recent(after: int = 0, p: Principal = Depends(need("read"))):
        return {"events": hub.recent(after)}

    @app.post("/api/chat")
    async def chat(body: Chat, p: Principal = Depends(need("chat"))):
        e = need_engine()
        done = threading.Event()
        box: dict = {}

        def reply(text: str) -> None:
            box["text"] = text
            done.set()

        e.submit_text(body.text, source="phone:app", reply=reply)
        ok = await asyncio.get_running_loop().run_in_executor(None, done.wait, CHAT_TIMEOUT)
        if not ok:
            return {"reply": None, "note": "Still working; the answer will arrive on the live feed."}
        return {"reply": box["text"]}

    @app.get("/api/approvals")
    def approvals(p: Principal = Depends(need("read"))):
        e = need_engine()
        return {"pending": [{"id": "current", "action": e.pending_confirm}] if e.pending_confirm else []}

    @app.post("/api/approvals/{aid}")
    def decide(aid: str, body: Approve, p: Principal = Depends(need("control"))):
        e = need_engine()
        if aid != "current" or not e.pending_confirm:
            raise HTTPException(404, "Nothing is waiting for approval.")
        action = e.pending_confirm
        e.answer_confirm(body.approve)
        _log(("approved" if body.approve else "declined") + f" from the app: {action[:80]}", p.username)
        return {"ok": True, "approved": body.approve}

    @app.post("/api/killswitch")
    def kill(body: KillSwitch, p: Principal = Depends(need("control"))):
        if body.engage:
            killswitch.engage(f"app ({p.username})")
        else:
            killswitch.release(f"app ({p.username})")
        return {"kill_switch": killswitch.engaged()}

    @app.post("/api/interrupt")
    def interrupt(p: Principal = Depends(need("control"))):
        need_engine().interrupt()
        return {"ok": True}

    @app.get("/api/integrations")
    def list_integrations(p: Principal = Depends(need("read"))):
        return {"integrations": integrations.status()}

    @app.put("/api/integrations/{pid}")
    def toggle(pid: str, body: Toggle, p: Principal = Depends(need("admin"))):
        try:
            out = integrations.set_enabled(pid, body.enabled)
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        _log(f"integration {pid} turned {'on' if body.enabled else 'off'}", p.username)
        return out

    # ---- live events ---------------------------------------------------------------------------
    @app.websocket("/api/events")
    async def events(ws: WebSocket):
        """First message from the client must be {"token": "<access token>", "after": <last seq seen>}.
        (The token isn't put in the URL, so it can't end up in a log.)"""
        await ws.accept()
        try:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=5)
            p = store.principal(str(hello.get("token", "")))
            if not p.can("read"):
                raise AuthError("Not allowed.", 403)
        except (asyncio.TimeoutError, AuthError, ValueError, KeyError, WebSocketDisconnect):
            await ws.close(code=4401)
            return
        sub = hub.subscribe()
        try:
            await ws.send_json({"type": "hello", "user": p.username, "kill_switch": killswitch.engaged()})
            for ev in hub.recent(int(hello.get("after", 0) or 0)):
                await ws.send_json(ev)
            while True:
                try:
                    ev = await asyncio.wait_for(sub[1].get(), timeout=25)
                except asyncio.TimeoutError:
                    ev = {"type": "ping"}
                try:
                    store.principal(str(hello.get("token", "")))
                except AuthError:   # signed out or expired: the app reconnects with a fresh token
                    await ws.close(code=4401)
                    return
                await ws.send_json(ev)
        except WebSocketDisconnect:
            pass
        finally:
            hub.unsubscribe(sub)

    return app


# ---- running it inside Fafnir ------------------------------------------------------------------
def start_in_background(engine, port: int = 8790, db: Path | None = None) -> threading.Thread | None:
    """Start the API on 127.0.0.1:<port> in a background thread, wired to the live engine."""
    import uvicorn

    from ..config import ROOT
    store = AuthStore(db or ROOT / "data" / "app_users.db")
    hub.tee_engine_events(engine)
    app = create_app(store, engine)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False,
                            ws_ping_interval=20, ws_ping_timeout=20)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None   # not the main thread
    t = threading.Thread(target=server.run, daemon=True, name="app-api")
    t.start()
    print(f"[app api] listening on 127.0.0.1:{port} ({len(store.users())} account(s))", flush=True)
    return t
