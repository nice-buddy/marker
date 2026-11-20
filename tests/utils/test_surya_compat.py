import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from marker.utils.surya_compat import (
    _process_is_alive,
    _server_is_alive,
    _stop_process,
    _utf8_read_text,
    apply_surya_windows_compat,
    apply_windows_utf8_compat,
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


def test_utf8_read_text_defaults_encoding(monkeypatch, tmp_path):
    import marker.utils.surya_compat as compat

    calls = []

    def fake_read_text(path, encoding=None, errors=None, *args, **kwargs):
        calls.append(encoding)
        return "ok"

    monkeypatch.setattr(compat, "_ORIGINAL_READ_TEXT", fake_read_text)

    assert _utf8_read_text(tmp_path / "metadata.json") == "ok"
    assert calls == ["utf-8"]


@pytest.mark.skipif(os.name != "nt", reason="Windows-only compatibility patch")
def test_apply_surya_windows_compat():
    assert apply_surya_windows_compat()
    assert apply_surya_windows_compat()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only compatibility patch")
def test_windows_read_text_defaults_to_utf8(tmp_path):
    assert apply_windows_utf8_compat()
    path = tmp_path / "metadata.json"
    path.write_text('{"name": "测试"}', encoding="utf-8")

    assert "测试" in path.read_text()
