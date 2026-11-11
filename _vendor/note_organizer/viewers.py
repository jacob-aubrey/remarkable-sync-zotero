"""Viewer adapters with a saved user choice and a safe system-default fallback."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

VIEWERS = {"system": "Windows default", "edge": "Microsoft Edge", "chrome": "Google Chrome",
           "firefox": "Mozilla Firefox", "adobe": "Adobe Acrobat / Reader", "sumatra": "SumatraPDF",
           "custom": "Other application"}
EXECUTABLES = {"edge": ("msedge.exe",), "chrome": ("chrome.exe",), "firefox": ("firefox.exe",),
               "adobe": ("Acrobat.exe", "AcroRd32.exe"), "sumatra": ("SumatraPDF.exe",)}
INSTALL_PATHS = {"edge": ("Microsoft/Edge/Application/msedge.exe",),
                 "chrome": ("Google/Chrome/Application/chrome.exe",),
                 "firefox": ("Mozilla Firefox/firefox.exe",),
                 "adobe": ("Adobe/Acrobat DC/Acrobat/Acrobat.exe", "Adobe/Acrobat Reader DC/Reader/AcroRd32.exe"),
                 "sumatra": ("SumatraPDF/SumatraPDF.exe",)}


def viewer_type(executable):
    name = Path(executable).name.casefold()
    return next((key for key, names in EXECUTABLES.items() if name in [n.casefold() for n in names]), "custom")


def executable_from_command(command):
    # Read only the executable; never execute a registry shell command or its arguments.
    match = re.match(r'^\s*(?:"([^"]+\.exe)"|(.+?\.exe)(?:\s|$))', command, re.I)
    return os.path.expandvars(match.group(1) or match.group(2)) if match else ""


def registry_read(hive_name, key, name="", view=0):
    if os.name != "nt":
        return ""
    import winreg
    try:
        with winreg.OpenKey(getattr(winreg, hive_name), key, 0, winreg.KEY_READ | view) as handle:
            value = winreg.QueryValueEx(handle, name)[0]
            return value if isinstance(value, str) else ""
    except OSError:
        return ""


def default_executable():
    # Ask the shell for the effective association. UserChoice and HKCR can
    # retain stale handlers that differ from Windows Settings / Explorer.
    # On failure, let the shell open the file; never guess another reader.
    if os.name != "nt":
        return ""
    import ctypes
    from ctypes import wintypes
    try:
        query = ctypes.WinDLL("shlwapi").AssocQueryStringW
        query.argtypes = [wintypes.DWORD, ctypes.c_int, wintypes.LPCWSTR, wintypes.LPCWSTR,
                          wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        query.restype = ctypes.c_long
        size = wintypes.DWORD()
        if query(0, 2, ".pdf", "open", None, ctypes.byref(size)) not in (0, 1) or not 0 < size.value <= 32768:
            return ""
        buffer = ctypes.create_unicode_buffer(size.value)
        if query(0, 2, ".pdf", "open", buffer, ctypes.byref(size)) != 0:
            return ""
        return buffer.value
    except (OSError, AttributeError):
        return ""


def registry_candidates(names):
    candidates = []
    for name in names:
        for hive in ("HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE"):
            for view in (0x0100, 0x0200):  # KEY_WOW64_64KEY / KEY_WOW64_32KEY
                value = registry_read(hive, "Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + name, view=view)
                if value:
                    candidates.append(os.path.expandvars(value.strip('"')))
    return candidates


def find_viewer(viewer="system"):
    if viewer == "system":
        default = default_executable()
        return default if default and Path(default).is_file() else None
    if viewer not in EXECUTABLES:
        return None
    candidates = registry_candidates(EXECUTABLES[viewer])
    candidates.extend(shutil.which(name) or "" for name in EXECUTABLES[viewer])
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidates.extend(str(Path(base) / relative) for relative in INSTALL_PATHS[viewer])
    return next((p for p in candidates if p and viewer_type(p) == viewer and Path(p).is_file()), None)


def viewer_command(executable, path, page):
    kind = viewer_type(executable)
    path = Path(path).resolve()
    # A properly encoded file URI preserves spaces, #, Unicode, and the page fragment.
    # Passing it directly to Edge avoids Windows forwarding it to another PDF reader.
    if kind in {"edge", "chrome", "firefox"}:
        return [str(executable), path.as_uri() + f"#page={page}"]
    if kind == "adobe":
        return [str(executable), "/A", f"page={page}", str(path)]
    if kind == "sumatra":
        return [str(executable), "-reuse-instance", "-page", str(page), str(path)]
    return [str(executable), str(path)]


def open_system_pdf(path):
    if os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)], close_fds=True)


def open_pdf_page(path, page, preference=None):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Source PDF is unavailable: {path}")
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ValueError("The source PDF page must be a positive integer.")
    preference = preference or {"viewer": "system"}
    viewer = preference.get("viewer", "system")
    if viewer not in VIEWERS:
        raise ValueError("Check the PDF default in Windows Settings.")
    executable = preference.get("path", "") if viewer == "custom" else find_viewer(viewer)
    if viewer == "system" and (not executable or viewer_type(executable) == "custom"):
        # Let Windows honor the complete association (including DDE or app-specific flags).
        open_system_pdf(path)
        return f"Opened in your default viewer. Navigate to PDF page {page}; this viewer's page-jump command is not supported.", False
    if not executable or (viewer == "custom" and not Path(executable).is_file()):
        raise FileNotFoundError(f"{VIEWERS[viewer]} was not found. Check the PDF default in Windows Settings.")
    subprocess.Popen(viewer_command(executable, path, page), close_fds=True)
    kind = viewer_type(executable)
    if kind == "custom":
        return f"Opened in {Path(executable).name}. Navigate to PDF page {page}; this viewer's page-jump command is not supported.", False
    return f"Opening PDF page {page} in {VIEWERS[kind]}.", True
