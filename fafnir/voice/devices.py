"""Pick the microphone: the first one in the owner's preference list that is plugged in.

PortAudio (behind sounddevice) reads the device list once, so it never notices a headset
being plugged in or unplugged. To see the live list without disturbing open streams, this
asks Windows directly (winmm), which is cheap enough to poll every couple of seconds.
When the right mic changes, the caller closes its audio streams, `refresh()` re-reads
PortAudio's list, and the streams are reopened on the new device.
"""
from __future__ import annotations

import ctypes
import sys

import sounddevice as sd

MME = 0  # PortAudio host API index for MME on Windows; its names match winmm's (31 chars max)


class _WaveInCaps(ctypes.Structure):
    _fields_ = [("wMid", ctypes.c_ushort), ("wPid", ctypes.c_ushort), ("vDriverVersion", ctypes.c_uint),
                ("szPname", ctypes.c_wchar * 32), ("dwFormats", ctypes.c_uint),
                ("wChannels", ctypes.c_ushort), ("wReserved1", ctypes.c_ushort)]


def live_input_names() -> list[str]:
    """Names of the microphones Windows has right now (no PortAudio involved)."""
    if sys.platform != "win32":
        return [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]
    winmm = ctypes.windll.winmm
    names = []
    for i in range(winmm.waveInGetNumDevs()):
        caps = _WaveInCaps()
        if winmm.waveInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            names.append(caps.szPname)
    return names


class _WaveOutCaps(ctypes.Structure):
    _fields_ = _WaveInCaps._fields_ + [("dwSupport", ctypes.c_uint)]


def default_output_name() -> str:
    """The speaker Windows is playing through right now ('' if unknown)."""
    if sys.platform != "win32":
        return ""
    winmm = ctypes.windll.winmm
    dev, flags = ctypes.c_uint(), ctypes.c_uint()
    # DRVM_MAPPER_PREFERRED_GET on WAVE_MAPPER: the id of the current default playback device
    if winmm.waveOutMessage(ctypes.c_void_p(0xFFFFFFFF), 0x2015, ctypes.byref(dev), ctypes.byref(flags)):
        return ""
    caps = _WaveOutCaps()
    if winmm.waveOutGetDevCapsW(dev.value, ctypes.byref(caps), ctypes.sizeof(caps)):
        return ""
    return caps.szPname


def choose_for(speaker: str, rules: list[tuple[str, str]], preferences: list[str],
               names: list[str]) -> str | None:
    """The mic to use given the speaker in use: the first rule whose speaker matches (and whose
    mic is plugged in) wins; otherwise the plain preference order."""
    for spk, mic in rules:
        if spk.strip() and spk.strip().lower() in speaker.lower():
            found = choose([mic], names)
            if found:
                return found
    return choose(preferences, names)


def choose(preferences: list[str], names: list[str]) -> str | None:
    """The first preferred mic that's present, as its device name; None = Windows default."""
    for pref in preferences:
        p = pref.strip().lower()
        for n in names:
            if p and p in n.lower():
                return n
    return None


def refresh() -> None:
    """Re-read PortAudio's device list. Every audio stream must be closed first."""
    sd._terminate()
    sd._initialize()


def use_input(name: str | None) -> str:
    """Make `name` the input every new stream opens (None = Windows default). Returns its name."""
    if name:
        for i, d in enumerate(sd.query_devices()):
            if d["hostapi"] == MME and d["max_input_channels"] > 0 and d["name"] == name:
                sd.default.device = (i, None)
                return name
    sd.default.device = (None, None)
    try:
        return sd.query_devices(kind="input")["name"]
    except Exception:
        return "none"


def probe_peak(name: str, seconds: float = 0.3) -> int:
    """Loudest sample a mic gives over a short listen (0-1 = switched off / no signal)."""
    import numpy as np
    for i, dev in enumerate(sd.query_devices()):
        if dev["hostapi"] == MME and dev["max_input_channels"] > 0 and dev["name"] == name:
            a = sd.rec(int(16000 * seconds), samplerate=16000, channels=1, dtype="int16", device=i)
            sd.wait()
            return int(np.abs(a).max())
    return 0
