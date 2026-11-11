"""Windows account protection, adapted from the existing Paper Sorter."""
import base64
import ctypes
import os

class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _as_bool(value: object, default: bool = False) -> bool:
    """Read legacy JSON booleans without treating the string ``"false"`` as true."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return default


def _blob_from_bytes(value: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _protect_for_current_windows_user(secret: str) -> str:
    """Encrypt a setting with Windows DPAPI so it is tied to this Windows account."""
    if os.name != "nt":
        raise OSError("Windows data protection is unavailable on this platform.")

    source, source_buffer = _blob_from_bytes(secret.encode("utf-8"))
    result = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    protect = crypt32.CryptProtectData
    protect.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_DataBlob),
    ]
    protect.restype = ctypes.c_bool
    if not protect(ctypes.byref(source), "AI Paper Sorter API key", None, None, None, 0x1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        protected = ctypes.string_at(result.pbData, result.cbData)
        return base64.b64encode(protected).decode("ascii")
    finally:
        # CryptProtectData allocates output with LocalAlloc.
        kernel32.LocalFree(result.pbData)
        del source_buffer


def _unprotect_for_current_windows_user(encoded_secret: str) -> str:
    if os.name != "nt":
        raise OSError("Windows data protection is unavailable on this platform.")

    source, source_buffer = _blob_from_bytes(base64.b64decode(encoded_secret.encode("ascii")))
    result = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_DataBlob),
    ]
    unprotect.restype = ctypes.c_bool
    if not unprotect(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.pbData, result.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(result.pbData)
        del source_buffer
