"""Explicit recycling of one reviewed local PDF, preserving saved research data."""
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import uuid

from .storage import canonical, digest_file


# IFileOperation (Windows 8+): require recycling, fail instead of asking Windows
# to silently skip errors, and record this user action in Explorer's undo stack.
# https://learn.microsoft.com/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperation-setoperationflags
RECYCLE_FLAGS = 0x00080000 | 0x00100000 | 0x20000000 | 0x0400 | 0x0010 | 0x0004 | 0x2000


def reviewed_pdf(path, root, expected_digest=""):
    """Reject unavailable, redirected, out-of-scope or changed selections."""
    path, root = Path(path).absolute(), Path(root).resolve()
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root):
        raise ValueError("The selected PDF is outside the folder that was checked. Check the library again.")
    if path.is_symlink() or path.resolve() != path or not path.is_file() or path.suffix.casefold() != ".pdf":
        raise ValueError("Choose an available local PDF without a redirected file path.")
    digest = digest_file(path)
    if expected_digest and digest != expected_digest:
        raise ValueError("This PDF changed since the library check. Check the library again before deleting it.")
    return {"path": canonical(path), "root": str(root), "digest": digest}


def _hresult(value):
    if value < 0:
        raise OSError(f"Windows could not complete this recycle operation (0x{value & 0xffffffff:08X}). "
                      "Check the original location and Recycle Bin before retrying.")
    return value


def _guid(value):
    return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le)


def _com_call(pointer, index, argument_types=(), *arguments):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argument_types)(vtable[index])
    return function(pointer, *arguments)


@contextmanager
def _hold_reviewed_pdf(path, expected_digest):
    """Deny editors while the verified handle's file is being recycled.

    Delete sharing is necessary for the Shell's recycle rename; write sharing
    is deliberately absent. An already-open editor also prevents this guard
    from acquiring its handle, rather than recycling a changing document.
    """
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.ReadFile.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000 | 0x10000, 1 | 4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError("The PDF is unavailable or open for editing. Close its editor and check the library again; the file was retained.")
    try:
        digest, count = hashlib.sha256(), wintypes.DWORD()
        buffer = ctypes.create_string_buffer(1024 * 1024)
        while True:
            if not kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not count.value:
                break
            digest.update(buffer.raw[:count.value])
        if expected_digest and digest.hexdigest() != expected_digest:
            raise ValueError("This PDF changed after confirmation. The file was retained; check the library again.")
        yield
    finally:
        kernel.CloseHandle(handle)


def _recycle_windows(path, expected_digest=""):
    """Recycle exactly one fixed-drive file; never fall back to permanent delete."""
    if os.name != "nt":
        raise OSError("Recycling local PDFs requires Windows. The file was retained.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    volume = ctypes.create_unicode_buffer(32768)
    get_volume = kernel.GetVolumePathNameW
    get_volume.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    get_volume.restype = wintypes.BOOL
    drive_type = kernel.GetDriveTypeW
    drive_type.argtypes = (wintypes.LPCWSTR,)
    drive_type.restype = wintypes.UINT
    if not get_volume(str(path), volume, len(volume)) or drive_type(volume.value) != 3:
        raise OSError("Only PDFs on a local fixed drive can be recycled here. The file was retained.")
    ole, shell = ctypes.OleDLL("ole32"), ctypes.OleDLL("shell32")
    # IFileOperation is STA-only. Workflow workers are normally uninitialized;
    # an incompatible apartment fails before any file operation is queued.
    ole.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole.CoInitializeEx.restype = ctypes.c_long
    initialized = _hresult(ole.CoInitializeEx(None, 2))
    operation, item = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        ole.CoCreateInstance.argtypes = (ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        ole.CoCreateInstance.restype = ctypes.c_long
        _hresult(ole.CoCreateInstance(_guid("3ad05575-8857-4850-9277-11b85bdb8e09"), None, 1,
                                     _guid("947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8"), ctypes.byref(operation)))
        _hresult(_com_call(operation, 5, (wintypes.DWORD,), RECYCLE_FLAGS))
        shell.SHCreateItemFromParsingName.argtypes = (wintypes.LPCWSTR, ctypes.c_void_p,
                                                      ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        shell.SHCreateItemFromParsingName.restype = ctypes.c_long
        _hresult(shell.SHCreateItemFromParsingName(str(path), None,
            _guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"), ctypes.byref(item)))
        # Vtable slots from the Windows SDK's IFileOperation interface.
        _hresult(_com_call(operation, 18, (ctypes.c_void_p, ctypes.c_void_p), item, None))
        with _hold_reviewed_pdf(path, expected_digest):
            result = _com_call(operation, 21)
            aborted = wintypes.BOOL()
            _hresult(_com_call(operation, 22, (ctypes.POINTER(wintypes.BOOL),), ctypes.byref(aborted)))
            _hresult(result)
            if aborted.value:
                raise OSError("Windows stopped this recycle operation. Check the original location and Recycle Bin before retrying.")
    finally:
        if item:
            _com_call(item, 2)
        if operation:
            _com_call(operation, 2)
        if initialized in (0, 1):
            ole.CoUninitialize()


def recycle_pdf(store, selection):
    """Recheck a confirmed selection and retain notes, identities and history."""
    proof = reviewed_pdf(selection["path"], selection["root"], selection["digest"])
    from .remarkable_sync import excluded
    if excluded(store, proof["path"]):
        raise ValueError("Managed cache and tablet files cannot be deleted from this review.")
    _recycle_windows(proof["path"], proof["digest"])
    if Path(proof["path"]).exists():
        raise OSError("Windows did not recycle the selected PDF. Its saved library record was left unchanged.")
    with store.connect() as db:
        # Preserve annotation and identity history so restoring the original
        # location from Recycle Bin can revive its existing library entry.
        rows = db.execute("SELECT id,project_id FROM documents WHERE path=?", (proof["path"],)).fetchall()
        db.execute("UPDATE documents SET missing=1 WHERE path=?", (proof["path"],))
        for row in rows:
            db.execute("UPDATE projects SET dirty=1 WHERE id=?", (row["project_id"],))
        db.execute("DELETE FROM scan_errors WHERE path=?", (proof["path"],))
    return proof["path"]


def recycle_pdfs(store, selections, progress=None):
    """Recheck a reviewed batch before any deletion; report individual failures."""
    from .remarkable_sync import excluded
    proofs = {}
    for selection in selections:
        proof = reviewed_pdf(selection["path"], selection["root"], selection["digest"])
        if excluded(store, proof["path"]):
            raise ValueError("Managed cache and tablet files cannot be deleted from this review.")
        proofs[proof["path"]] = proof
    result = {"deleted": [], "failed": []}
    for index, proof in enumerate(proofs.values(), 1):
        try:
            result["deleted"].append(recycle_pdf(store, proof))
        except Exception as exc:
            result["failed"].append({"path": proof["path"], "error": str(exc)})
        if progress:
            progress(index, len(proofs), proof["path"])
    return result
