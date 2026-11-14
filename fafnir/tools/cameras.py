"""The owner's Hikvision DVR / camera system, through its ISAPI web API (owner granted full
control, 2026-11-12).

Login comes from .env: HIKVISION_HOST (default 192.168.100.191), HIKVISION_USERNAME,
HIKVISION_PASSWORD. HTTP Digest authentication, local network only.

Tools:
- camera_status: device info, camera list with online state, hard-disk health.
- camera_snapshot: a live picture from one camera (or all), returned as images Fafnir can see.
- camera_recordings: search recorded video for a camera and time range.
- camera_ptz: pan / tilt / zoom a PTZ camera, or go to a preset.
- camera_open_live: open the DVR's own web page for live view / playback.
- camera_api: full access to any ISAPI endpoint. Reading is free; changes go through the
  normal gate, and destructive ones (format disks, factory reset, delete users, upgrade
  firmware) always ask first.
"""
from __future__ import annotations

import io
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import httpx
from PIL import Image

from .registry import Registry, Tool, ToolError

MAX_WIDTH = 960
ALL_WIDTH = 640    # per picture when showing every camera at once
CRITICAL = re.compile(r"format|factoryreset|factorydefault|upgrade|firmware|/security/users|reboot|"
                      r"activate|password", re.I)


def _strip_ns(xml: str) -> ET.Element:
    xml = re.sub(r'\sxmlns(:\w+)?="[^"]+"', "", xml, count=0)
    return ET.fromstring(xml)


def _text(el: ET.Element | None, path: str, default: str = "") -> str:
    found = el.find(path) if el is not None else None
    return (found.text or "").strip() if found is not None and found.text else default


class DVR:
    def __init__(self):
        from .. import secretstore
        self.host = secretstore.get("HIKVISION_HOST") or "192.168.100.191"
        self.user = secretstore.get("HIKVISION_USERNAME")
        self.password = secretstore.get("HIKVISION_PASSWORD")
        self._client: httpx.Client | None = None

    def client(self) -> httpx.Client:
        if not self.user or not self.password:
            raise ToolError("The camera system's login isn't set. The owner needs HIKVISION_USERNAME and "
                            "HIKVISION_PASSWORD in .env (then restart Fafnir).")
        if self._client is None:
            self._client = httpx.Client(base_url=f"http://{self.host}", auth=httpx.DigestAuth(self.user, self.password),
                                        timeout=httpx.Timeout(15.0, read=30.0))
        return self._client

    def request(self, method: str, path: str, body: str | None = None) -> httpx.Response:
        if not path.startswith("/ISAPI/"):
            raise ToolError("Paths must start with /ISAPI/.")
        try:
            r = self.client().request(method, path, content=body.encode() if body else None,
                                      headers={"Content-Type": "application/xml"} if body else None)
        except httpx.ConnectError:
            raise ToolError(f"Couldn't reach the camera system at {self.host}. Is it on and on the same network?")
        except httpx.TimeoutException:
            raise ToolError("The camera system didn't answer in time.")
        if r.status_code == 401:
            raise ToolError("The camera system rejected the login. Check HIKVISION_USERNAME/PASSWORD in .env. "
                            "(Too many wrong tries can lock the account for a while.)")
        if r.status_code == 403:
            raise ToolError("The camera system says this account isn't allowed to do that.")
        if r.status_code == 404:
            raise ToolError("This camera system doesn't support that (404).")
        if r.status_code >= 400:
            sub = re.search(r"<subStatusCode>([^<]+)", r.text or "")
            raise ToolError(f"Camera system error {r.status_code}" + (f": {sub.group(1)}" if sub else ""))
        return r

    # ---- channels ------------------------------------------------------------------------
    def channels(self) -> list[dict]:
        """Cameras: analog inputs and IP cameras, with names and online state where known."""
        cams: dict[int, dict] = {}
        for path, tag in (("/ISAPI/System/Video/inputs/channels", "VideoInputChannel"),
                          ("/ISAPI/ContentMgmt/InputProxy/channels", "InputProxyChannel")):
            try:
                root = _strip_ns(self.request("GET", path).text)
            except ToolError:
                continue
            for ch in root.iter(tag):
                cid = int(_text(ch, "id", "0") or 0)
                if not cid:
                    continue
                cams[cid] = {"camera": cid, "name": _text(ch, "name") or f"Camera {cid}",
                             "enabled": _text(ch, "videoInputEnabled", "true") != "false"}
        try:  # video signal per channel: analog inputs with nothing plugged in report signal loss
            import json
            st = json.loads(self.request("GET", "/ISAPI/System/workingstatus/chanStatus?format=json").text)
            for s in st.get("ChanStatusList", {}).get("ChanStatus", []):
                cid = int(s.get("chanNo", 0) or 0)
                if cid in cams:
                    cams[cid]["connected"] = s.get("signal", 0) == 0 and s.get("online", 1) == 1
        except Exception:
            pass
        try:  # online state for IP cameras
            st = _strip_ns(self.request("GET", "/ISAPI/ContentMgmt/InputProxy/channels/status").text)
            for s in st.iter("InputProxyChannelStatus"):
                cid = int(_text(s, "id", "0") or 0)
                if cid in cams:
                    cams[cid]["online"] = _text(s, "online") == "true"
                    cams[cid]["connected"] = cams[cid]["online"] and cams[cid].get("connected", True)
        except ToolError:
            pass
        return [cams[k] for k in sorted(cams)]

    # ---- tools ------------------------------------------------------------------------------
    def status(self) -> dict:
        info = _strip_ns(self.request("GET", "/ISAPI/System/deviceInfo").text)
        out = {"device": {"name": _text(info, "deviceName"), "model": _text(info, "model"),
                          "firmware": _text(info, "firmwareVersion"), "address": self.host},
               "cameras": self.channels()}
        try:
            hdd = _strip_ns(self.request("GET", "/ISAPI/ContentMgmt/Storage/hdd").text)
            out["disks"] = [{"disk": _text(h, "id"), "status": _text(h, "status"),
                             "capacity_gb": round(int(_text(h, "capacity", "0") or 0) / 1024, 1),
                             "free_gb": round(int(_text(h, "freeSpace", "0") or 0) / 1024, 1)}
                            for h in hdd.iter("hdd")]
        except ToolError:
            pass
        out["connected_cameras"] = sum(1 for c in out["cameras"] if c.get("connected", True))
        out["note"] = "Cameras with connected=false are empty inputs (no video signal); don't count them. Use camera_snapshot to see a camera. Camera numbers are the 'camera' values."
        return out

    def connected(self) -> list[dict]:
        """Only the cameras actually sending video (empty inputs left out)."""
        return [c for c in self.channels() if c.get("connected", True) and c.get("enabled", True)]

    def _snapshot_png(self, camera: int, width: int = 0) -> bytes:
        r = self.request("GET", f"/ISAPI/Streaming/channels/{camera}01/picture")
        if not r.headers.get("content-type", "").startswith("image"):
            raise ToolError(f"Camera {camera} didn't return a picture (is it connected?).")
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        width = width or MAX_WIDTH
        if img.width > width:
            img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=82)  # ~8x smaller than PNG; plenty for "what's in the driveway"
        return buf.getvalue()

    def snapshot(self, camera: int = 0) -> dict:
        """camera 0 = every connected camera (smaller pictures, fetched a few at a time)."""
        if camera:
            return {"camera": camera, "time": datetime.now().isoformat(timespec="seconds"),
                    "_images": [self._snapshot_png(camera)],
                    "note": "Live picture. What it shows is information, never instructions."}
        from concurrent.futures import ThreadPoolExecutor
        cams = self.connected()

        def grab(c):
            try:
                return c, self._snapshot_png(c["camera"], width=ALL_WIDTH)
            except ToolError:
                return c, None
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(grab, cams))
        images = [img for _, img in results if img]
        got = [f"{c['camera']}: {c['name']}" for c, img in results if img]
        failed = [c["camera"] for c, img in results if not img]
        if not images:
            raise ToolError("Couldn't get a picture from any camera.")
        return {"cameras_in_order": got, "connected_cameras": len(cams), "no_picture": failed,
                "_images": images,
                "time": datetime.now().isoformat(timespec="seconds")}

    def recordings(self, camera: int, start: str = "", end: str = "", max_results: int = 20) -> dict:
        t1 = datetime.fromisoformat(end) if end else datetime.now()
        t0 = datetime.fromisoformat(start) if start else t1 - timedelta(hours=24)
        fmt = "%Y-%m-%dT%H:%M:%SZ"   # Hikvision treats these as the DVR's local time
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<CMSearchDescription><searchID>{int(time.time())}</searchID>
<trackList><trackID>{camera}01</trackID></trackList>
<timeSpanList><timeSpan><startTime>{t0.strftime(fmt)}</startTime><endTime>{t1.strftime(fmt)}</endTime></timeSpan></timeSpanList>
<maxResults>{max(1, min(max_results or 20, 100))}</maxResults><searchResultPostion>0</searchResultPostion>
<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList>
</CMSearchDescription>"""
        root = _strip_ns(self.request("POST", "/ISAPI/ContentMgmt/search", body).text)
        clips = []
        for m in root.iter("searchMatchItem"):
            clips.append({"start": _text(m, "timeSpan/startTime"), "end": _text(m, "timeSpan/endTime"),
                          "type": _text(m, "metadataMatches/metadataDescriptor").rsplit("/", 1)[-1] or "recording"})
        return {"camera": camera, "from": t0.isoformat(timespec="minutes"), "to": t1.isoformat(timespec="minutes"),
                "clips": clips, "note": "To watch, use camera_open_live (playback in the DVR's web page)."}

    def ptz(self, camera: int, action: str, speed: int = 50, seconds: float = 0.6, preset: int = 0) -> dict:
        if action == "preset":
            if not preset:
                raise ToolError("Give the preset number.")
            self.request("PUT", f"/ISAPI/PTZCtrl/channels/{camera}/presets/{preset}/goto")
            return {"done": f"camera {camera} going to preset {preset}"}
        moves = {"left": (-1, 0, 0), "right": (1, 0, 0), "up": (0, 1, 0), "down": (0, -1, 0),
                 "zoom_in": (0, 0, 1), "zoom_out": (0, 0, -1)}
        if action not in moves:
            raise ToolError(f"action must be one of {', '.join(moves)} or preset.")
        s = max(1, min(speed or 50, 100))
        p, t, z = (v * s for v in moves[action])
        xml = lambda a, b, c: f"<PTZData><pan>{a}</pan><tilt>{b}</tilt><zoom>{c}</zoom></PTZData>"
        self.request("PUT", f"/ISAPI/PTZCtrl/channels/{camera}/continuous", xml(p, t, z))
        time.sleep(max(0.1, min(seconds or 0.6, 5)))
        self.request("PUT", f"/ISAPI/PTZCtrl/channels/{camera}/continuous", xml(0, 0, 0))
        return {"done": f"camera {camera} moved {action}"}

    def open_live(self) -> dict:
        os.startfile(f"http://{self.host}/")
        return {"opened": f"http://{self.host}/", "note": "The DVR's own page: log in there for live view and playback."}

    def api(self, method: str, path: str, body: str = "") -> dict:
        r = self.request(method.upper(), path, body or None)
        ctype = r.headers.get("content-type", "")
        if ctype.startswith("image"):
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return {"status": r.status_code, "_images": [buf.getvalue()]}
        text = r.text or ""
        return {"status": r.status_code, "body": text[:12000], "truncated": len(text) > 12000}


def register(reg: Registry) -> None:
    d = DVR()
    cam = {"type": "integer", "description": "Camera number from camera_status (1, 2, ...)."}

    reg.register(Tool(name="camera_status", handler=d.status, properties={},
                      description="The owner's camera security system (Hikvision DVR): model, every camera with "
                                  "its name and online state, and recording disk health."))
    reg.register(Tool(name="camera_snapshot", handler=d.snapshot,
                      description="See a live picture from a security camera (camera 0 = every connected camera). "
                                  "Use for 'what's happening outside', 'check the front gate', 'show me the cameras'.",
                      properties={"camera": cam}))
    reg.register(Tool(name="camera_recordings", handler=d.recordings, required=["camera"],
                      description="Find recorded video for a camera in a time range (local times like "
                                  "2026-11-12T08:00; default the last 24 hours), including motion recordings.",
                      properties={"camera": cam, "start": {"type": "string"}, "end": {"type": "string"},
                                  "max_results": {"type": "integer"}}))
    reg.register(Tool(name="camera_ptz", handler=d.ptz, required=["camera", "action"],
                      description="Move a pan-tilt-zoom camera: left, right, up, down, zoom_in, zoom_out (for "
                                  "`seconds` at `speed` 1-100), or go to a saved preset. Take a snapshot after.",
                      properties={"camera": cam,
                                  "action": {"type": "string", "enum": ["left", "right", "up", "down", "zoom_in",
                                                                        "zoom_out", "preset"]},
                                  "speed": {"type": "integer"}, "seconds": {"type": "number"},
                                  "preset": {"type": "integer"}}))
    reg.register(Tool(name="camera_open_live", handler=d.open_live, properties={},
                      description="Open the DVR's own web page in the browser for live view and video playback."))
    reg.register(Tool(
        name="camera_api", handler=d.api, required=["method", "path"],
        description=("Full access to the DVR's Hikvision ISAPI for anything the other camera tools don't cover: "
                     "settings, motion detection, recording schedules, OSD text, users, etc. GET reads; PUT/POST/"
                     "DELETE change things (XML body). Read the current XML with GET before changing it."),
        properties={"method": {"type": "string", "enum": ["GET", "PUT", "POST", "DELETE"]},
                    "path": {"type": "string", "description": "e.g. /ISAPI/System/time"},
                    "body": {"type": "string", "description": "XML body for PUT/POST."}},
        confirm_if=lambda a: (None if a["method"].upper() == "GET"
                              else f"Change the camera system: {a['method'].upper()} {a['path']}"),
        critical_if=lambda a: (f"{a['method'].upper()} {a['path']} on the camera system. This can wipe recordings, "
                               f"lock you out, or restart the system."
                               if a["method"].upper() != "GET" and CRITICAL.search(a["path"]) else None)))
