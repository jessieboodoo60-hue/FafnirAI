"""Pick the few tools a request needs.

A local 8B model at full speed has room for roughly ten tool descriptions, not Fafnir's 87
(13,700 tokens). Each tool's name, description and a few hint phrases are embedded once
(fafnir/embed.py, CPU); a request is embedded (7 ms) and the closest tools are offered to the
model. Tools that only make sense together are pulled in as a family, and the tools used in the
previous turn stay available so follow-ups ("and the next one") keep working.

Measured 2026-11-13 on tests/brain_cases.json: the expected tool was in a 10-tool shortlist for
32 of 34 requests before hints were added.
"""
from __future__ import annotations

import numpy as np

from .embed import embed
from .tools.registry import Registry

# Extra words people actually say, for tools whose descriptions don't contain them.
HINTS = {
    "camera_snapshot": "look outside, what cars or people are in the driveway, gate, pool, backyard, front door, "
                       "who is there, check the cameras, see the security camera picture",
    "camera_status": "is the DVR or security system working, recording, cameras online, hard disk",
    "camera_open_live": "show the live camera view on screen",
    "screen_look": "what is on my screen, look at the screen, see the window, read what's displayed",
    "mouse_click": "click a button, press the Start button, select something on screen, double click, right click",
    "keyboard_type": "type text into the open window, write this in the box",
    "keyboard_keys": "press a key or shortcut, press enter, ctrl c, alt tab",
    "computer_media": "volume up or down, mute, pause or play music, next song, previous track",
    "computer_launch_app": "open or start a program or app such as Notepad, Chrome, Spotify, Steam",
    "computer_close_app": "close, quit or exit a program",
    "computer_system_info": "free disk space, memory, CPU, how full is my drive, computer specs",
    "heartbeat_pause": "stop notifications, do not disturb, be quiet for an hour",
    "notices_list": "any notices, alerts or reminders waiting for me",
    "conversation_search": "what did we talk about earlier, yesterday, last week",
    "google_tasks_add": "add a task or to-do, remind me to do something",
    "google_tasks_list": "what is on my to-do list, my tasks",
    "gmail_search": "check my email, unread emails, latest email, inbox",
    "calendar_list_events": "what's on my calendar, my schedule, meetings today or tomorrow",
    "private_files_add": "add this file to my vault, put my document in the vault, store my scan safely, lock this file away",
    "private_files_remove": "remove a document from my vault, take it out of the vault, delete it from the vault",
    "private_files_list": "what is in my vault, list my private documents",
    "private_files_unlock": "passport, national ID card, driver's permit, birth certificate, personal information, "
                            "my ID number, identity documents",
    "calculate": "what is 17 times 23, multiply, divide, add up, percentage of, how much is 15 percent, sum, total, maths, arithmetic",
    "health_status": "how are you doing, run diagnostics, is everything working, system check, why are you slow, status report",
    "privacy_report": "what have you sent out, did anything leave my PC, what data did you share, outbound requests",
    "software_install": "install a program, app or game",
    "software_uninstall": "uninstall or remove a program, app or game",
}

# If one of these is shortlisted, its partners come too.
FAMILIES = [
    {"screen_look", "mouse_click", "keyboard_type", "keyboard_keys"},
    {"camera_snapshot", "camera_status"},
    {"software_search", "software_install"},
    {"software_search", "software_uninstall"},
    {"gmail_search", "gmail_read"},
    {"gmail_send", "contacts_search"},
    {"memory_search", "memory_forget", "memory_update"},
    {"private_files_add", "private_files_remove", "private_files_list"},
    {"computer_find_files", "computer_read_text_file"},
]


class ToolRouter:
    def __init__(self, registry: Registry):
        self.schemas = {s["name"]: s for s in registry.schemas() if "input_schema" in s}
        self.names = list(self.schemas)
        docs = [f"{n.replace('_', ' ')}: {self.schemas[n].get('description', '')} {HINTS.get(n, '')}" for n in self.names]
        self._vectors = embed(docs)

    def pick(self, text: str, k: int = 10, keep: list[str] | None = None) -> list[str]:
        """Names of the tools to offer for this request, most relevant first."""
        scores = embed([text], query=True)[0] @ self._vectors.T
        chosen = [self.names[i] for i in np.argsort(-scores)[:k]]
        for fam in FAMILIES:                      # partners of the top few only, to stay small
            if fam & set(chosen[:4]):
                chosen += [n for n in fam if n not in chosen and n in self.schemas]
        for n in keep or []:
            if n in self.schemas and n not in chosen:
                chosen.append(n)
        return chosen[:k + 6]
