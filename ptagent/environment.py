from __future__ import annotations

import importlib.metadata
import os
import platform
import shutil
from pathlib import Path


def find_packet_tracer() -> Path | None:
    configured = os.environ.get("PACKET_TRACER_EXE")
    if configured:
        path = Path(configured)
        return path if path.is_file() and path.name.lower() == "packettracer.exe" else None
    roots = [Path(os.environ.get(key, fallback)) for key, fallback in (
        ("ProgramFiles", "C:/Program Files"), ("ProgramFiles(x86)", "C:/Program Files (x86)"))]
    found = [p for root in roots for p in root.glob("Cisco Packet Tracer*/bin/PacketTracer.exe")]
    return max(found, key=lambda p:p.stat().st_mtime) if found else None


def inspect_environment() -> dict:
    exe = find_packet_tracer()
    version = None
    if exe and os.name == "nt":
        try:
            import win32api
            info = win32api.GetFileVersionInfo(str(exe), "\\")
            ms, ls = info["FileVersionMS"], info["FileVersionLS"]
            version = f"{ms>>16}.{ms&65535}.{ls>>16}.{ls&65535}"
        except Exception:
            version = "Не удалось прочитать метаданные"
    libraries = {}
    for name in ("PySide6-Essentials", "PyAutoGUI", "pywinauto", "opencv-python", "Pillow", "PyMuPDF", "python-docx", "pydantic", "pytest", "mcp", "packet-tracer-mcp"):
        try:
            libraries[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            libraries[name] = None
    return {"windows":platform.platform(), "python":platform.python_version(), "packet_tracer":str(exe) if exe else None,
            "packet_tracer_version":version, "libraries":libraries, "uv":shutil.which("uv")}
