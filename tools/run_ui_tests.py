"""Run Windows GUI tests on a private, inactive desktop.

The test desktop is never switched to the screen. Tk/WebView2 can exercise
real window focus there without raising windows or moving the user's cursor.
Usage: .venv/Scripts/python.exe tools/run_ui_tests.py -m unittest discover -q
"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if os.name != "nt":
        return subprocess.call([sys.executable, *sys.argv[1:]])
    import msvcrt

    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user.CreateDesktopW.restype = wintypes.HANDLE
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]

    def input_desktop_name():
        handle = user.OpenInputDesktop(0, False, 1)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            name, required = ctypes.create_unicode_buffer(256), wintypes.DWORD()
            if not user.GetUserObjectInformationW(handle, 2, name, ctypes.sizeof(name), ctypes.byref(required)):
                raise ctypes.WinError(ctypes.get_last_error())
            return name.value
        finally:
            user.CloseDesktop(handle)

    class StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    kernel.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInfo)]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    original_desktop = input_desktop_name()
    # A GitHub-hosted VM is already disposable and has no user's workspace.
    # Its service session's WebView2 broker cannot initialize on an alternate
    # desktop (0x80070578). Keep the real renderer tests on the VM's own desktop.
    if os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted":
        environment = {**os.environ, "NOTABLE_TEST_DESKTOP": original_desktop,
                       "NOTABLE_TEST_HOSTED_RUNNER": "1", "PYTHONIOENCODING": "utf-8"}
        print(f"Tests isolated by GitHub-hosted VM; desktop: {original_desktop}", flush=True)
        return subprocess.call([sys.executable, "-u", *sys.argv[1:]], env=environment)
    name = f"NotableTests-{os.getpid()}-{time.time_ns()}"
    desktop = user.CreateDesktopW(name, None, None, 0, 0x10000000, None)
    if not desktop:
        raise ctypes.WinError(ctypes.get_last_error())
    log_dir = Path(__file__).resolve().parents[1] / ".test-tmp"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"ui-tests-{os.getpid()}.log"
    process = ProcessInfo()
    try:
        with log_path.open("wb") as output, open(os.devnull, "rb") as input_file:
            for stream in (output, input_file):
                os.set_inheritable(stream.fileno(), True)
            startup = StartupInfo()
            startup.cb = ctypes.sizeof(startup)
            startup.lpDesktop = "winsta0\\" + name
            startup.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.hStdInput = msvcrt.get_osfhandle(input_file.fileno())
            startup.hStdOutput = startup.hStdError = msvcrt.get_osfhandle(output.fileno())
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline([sys.executable, "-u", *sys.argv[1:]]))
            os.environ["NOTABLE_TEST_DESKTOP"] = name
            os.environ["PYTHONIOENCODING"] = "utf-8"
            if not kernel.CreateProcessW(None, command, None, None, True, 0, None,
                    str(Path(__file__).resolve().parents[1]), ctypes.byref(startup), ctypes.byref(process)):
                raise ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(process.hThread)
        offset = 0
        print(f"Tests isolated on inactive desktop {name}; log: {log_path}", flush=True)
        while True:
            result = kernel.WaitForSingleObject(process.hProcess, 1000)
            with log_path.open("rb") as output:
                output.seek(offset)
                data = output.read()
                offset += len(data)
            if data:
                print(data.decode("utf-8", errors="replace"), end="", flush=True)
            if result == 0:
                break
            if result != 258:
                raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(process.hProcess, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        if input_desktop_name() != original_desktop:
            raise RuntimeError("The active input desktop changed during testing")
        return code.value
    finally:
        if process.hProcess:
            kernel.CloseHandle(process.hProcess)
        user.CloseDesktop(desktop)


if __name__ == "__main__":
    sys.exit(main())
