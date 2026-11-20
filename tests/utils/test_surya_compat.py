import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from marker.utils.surya_compat import (
    _process_is_alive,
    _server_is_alive,
    _stop_process,
    apply_surya_windows_compat,
)

pytestmark = pytest.mark.cpu


def test_process_lifecycle_helpers():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    try:
        handle = SimpleNamespace(
            cleanup_kind="process", pid=process.pid, cleanup_id=""
        )
        assert _process_is_alive(process.pid)
        assert _server_is_alive(handle, fallback=lambda _: False)

        _stop_process(process.pid, "test-server")
        process.wait(timeout=5)

        assert not _process_is_alive(process.pid)
        assert not _server_is_alive(handle, fallback=lambda _: False)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only compatibility patch")
def test_apply_surya_windows_compat():
    assert apply_surya_windows_compat()
    assert apply_surya_windows_compat()
