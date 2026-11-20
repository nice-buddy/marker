"""Windows compatibility patches for Surya/Indra runtime behavior."""

import os
from pathlib import Path
from typing import Any, Callable

from marker.logger import get_logger

logger = get_logger()

_ORIGINAL_READ_TEXT = Path.read_text


def _utf8_read_text(
    path: Path, encoding: str | None = None, errors: str | None = None, *args, **kwargs
):
    return _ORIGINAL_READ_TEXT(
        path,
        encoding=encoding or "utf-8",
        errors=errors,
        *args,
        **kwargs,
    )


def apply_windows_utf8_compat() -> bool:
    """Default ``Path.read_text`` to UTF-8 on Windows.

    Indra reads model metadata with ``Path.read_text()`` and no encoding. On
    Windows that defaults to GBK, which cannot decode the UTF-8 model JSON.
    """
    if os.name != "nt":
        return False
    if getattr(Path, "_marker_utf8_default", False):
        return True
    Path.read_text = _utf8_read_text
    Path._marker_utf8_default = True
    logger.info("Applied Windows UTF-8 default for Path.read_text.")
    return True


def _process_is_alive(pid: int) -> bool:
    import psutil

    try:
        return psutil.Process(pid).is_running()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        return True
    except Exception:
        return True


def _stop_process(pid: int, name: str) -> None:
    import psutil

    try:
        process = psutil.Process(pid)
        process.terminate()
        try:
            process.wait(timeout=10)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        logger.info(f"Stopped {name} (pid {pid})")
    except psutil.NoSuchProcess:
        pass
    except Exception as exc:
        logger.warning(f"Failed to stop {name} (pid {pid}): {exc}")


def _server_is_alive(
    handle: Any, fallback: Callable[[Any], bool]
) -> bool:
    if handle.cleanup_kind == "process" and handle.pid:
        return _process_is_alive(handle.pid)
    return fallback(handle)


def apply_surya_windows_compat() -> bool:
    """Replace Surya's POSIX-only waitpid/kill checks with psutil on Windows."""
    if os.name != "nt":
        return False

    try:
        from surya.inference.backends import spawn as surya_spawn
    except Exception:
        return False

    if getattr(surya_spawn, "_marker_windows_compat", False):
        return True

    original_server_is_alive = getattr(surya_spawn, "_server_is_alive", None)
    original_stop_process = getattr(surya_spawn, "_stop_process", None)
    if original_server_is_alive is None or original_stop_process is None:
        return False

    def _patched_server_is_alive(handle):
        return _server_is_alive(handle, original_server_is_alive)

    surya_spawn._server_is_alive = _patched_server_is_alive
    surya_spawn._stop_process = _stop_process
    surya_spawn._marker_windows_compat = True
    logger.info(
        "Applied Windows compatibility patch for Surya llama.cpp server lifecycle."
    )
    return True
