"""ctypes bridge for libmspack's raw LZX decoder."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path


class MspackLzxUnavailable(RuntimeError):
    pass


def _candidate_paths() -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    paths: list[Path] = []
    env = os.environ.get("STRATA_MSPACK_LZX_DLL")
    if env:
        paths.append(Path(env))
    paths.extend(
        [
            root / "native" / "build" / "mspack_lzx.dylib",
            root / "native" / "build" / "libmspack_lzx.dylib",
            root / "native" / "build" / "mspack_lzx.so",
            root / "native" / "build" / "libmspack_lzx.so",
            root / "native" / "build" / "x64" / "mspack_lzx.dll",
            root / "native" / "build" / "x86" / "mspack_lzx.dll",
            root / "native" / "mspack_lzx.dll",
        ]
    )
    return paths


def _load() -> ctypes.CDLL:
    errors: list[str] = []
    for path in _candidate_paths():
        if not path.exists():
            continue
        try:
            dll = ctypes.CDLL(str(path))
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        fn = dll.strata_lzx_decompress
        fn.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_size_t,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        fn.restype = ctypes.c_int
        return dll
    detail = "; ".join(errors) if errors else "no candidate DLL found"
    raise MspackLzxUnavailable(
        "mspack_lzx native library is not available; set STRATA_MSPACK_LZX_DLL "
        f"to a libmspack LZX shim path ({detail})"
    )


_DLL: ctypes.CDLL | None = None
_WINDOW_BITS: int | None = None


def init(window_bits: int) -> None:
    global _WINDOW_BITS
    _WINDOW_BITS = window_bits


def reset() -> None:
    pass


def decompress(
    data: bytes,
    uncompressed_length: int,
    window_bits: int | None = None,
    reset_interval: int = 0,
) -> bytes:
    """Decompress one LZX reset chunk with libmspack."""
    global _DLL
    if uncompressed_length < 0:
        raise ValueError("uncompressed_length must be non-negative")
    if window_bits is None:
        if _WINDOW_BITS is None:
            raise RuntimeError("mspack_lzx.init(window_bits) must be called first")
        window_bits = _WINDOW_BITS
    if _DLL is None:
        _DLL = _load()

    src = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    dst = (ctypes.c_ubyte * uncompressed_length)()
    written = ctypes.c_size_t(0)
    err = _DLL.strata_lzx_decompress(
        src,
        len(data),
        dst,
        uncompressed_length,
        window_bits,
        reset_interval,
        ctypes.byref(written),
    )
    if err != 0:
        raise RuntimeError(
            f"libmspack LZX decompression failed: err={err} written={written.value}"
        )
    if written.value != uncompressed_length:
        raise RuntimeError(
            "libmspack LZX decompression returned a short buffer: "
            f"{written.value} != {uncompressed_length}"
        )
    return bytes(dst)


def decompress_stream(
    data: bytes,
    uncompressed_length: int,
    window_bits: int,
    reset_interval: int,
) -> bytes:
    return decompress(data, uncompressed_length, window_bits, reset_interval)
