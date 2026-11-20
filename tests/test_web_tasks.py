import os
import re

import pytest

from marker.scripts.web_tasks import TaskManager, TaskStore

pytestmark = pytest.mark.cpu


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.fail_on = None

    def convert_page(
        self,
        filepath,
        total_pages,
        n,
        opts,
        images_folder,
        display_image_path=None,
        display_image_url=None,
    ):
        self.calls.append(n)
        if self.fail_on == n:
            raise RuntimeError("simulated page failure")
        os.makedirs(images_folder, exist_ok=True)
        image_name = f"page_{n}.jpg"
        with open(os.path.join(images_folder, image_name), "wb") as f:
            f.write(b"image")
        if display_image_path:
            os.makedirs(os.path.dirname(display_image_path), exist_ok=True)
            with open(display_image_path, "wb") as f:
                f.write(b"display")
        return {
            "page": n + 1,
            "page_index": n,
            "w": 100,
            "h": 100,
            "img": display_image_url or "data:image/jpeg;base64,AA==",
            "source": "surya",
            "mode_used": opts.get("mode", "auto"),
            "blocks": [],
            "lines": [],
            "markdown": f"# Page {n + 1}\n\n![](images/{image_name})",
            "preview_html": f"<p>Page {n + 1}</p>",
            "html": "",
            "chunks": [],
            "json": {},
            "images_count": 1,
            "timings": {"total_ms": 1},
        }


def _make_manager(tmp_path, monkeypatch):
    store = TaskStore(str(tmp_path / "history" / "tasks.sqlite3"))
    manager = TaskManager(
        FakeEngine(),
        store=store,
        input_dir=str(tmp_path / "inputs"),
        output_dir=str(tmp_path / "outputs"),
        history_dir=str(tmp_path / "history"),
    )
    monkeypatch.setattr(
        TaskManager, "_count_pages", staticmethod(lambda *args: 3)
    )
    return manager


def _create_task(manager, filename="测试.pdf"):
    return manager.create_task(
        b"%PDF-1.4 test",
        filename,
        {
            "mode": "vllm",
            "dpi": 300,
            "max_pages": 1000,
            "page_range": "0-2",
        },
    )


def test_task_names_inputs_and_outputs_are_unique(tmp_path, monkeypatch):
    manager = _make_manager(tmp_path, monkeypatch)
    task1 = _create_task(manager)
    task2 = _create_task(manager)

    assert re.fullmatch(r"\d{17}_测试\.pdf", task1["task_name"])
    assert re.fullmatch(r"测试_\d{17}\.pdf", os.path.basename(task1["input_path"]))
    assert task1["task_name"] != task2["task_name"]
    assert task1["output_dir"] != task2["output_dir"]
    assert task1["input_path"] != task2["input_path"]
    assert os.path.isfile(task1["input_path"])


def test_single_worker_queue_positions(tmp_path, monkeypatch):
    manager = _make_manager(tmp_path, monkeypatch)
    task1 = _create_task(manager)
    task2 = _create_task(manager)

    queued = {t["id"]: t for t in manager.list_tasks()}
    assert queued[task1["id"]]["queue_position"] == 1
    assert queued[task2["id"]]["queue_position"] == 2

    claimed = manager.store.claim_next_queued()
    assert claimed["id"] == task1["id"]
    assert manager.store.claim_next_queued() is None

    running = manager.get_task(task1["id"])
    waiting = manager.get_task(task2["id"])
    assert running["status"] == "running"
    assert running["queue_position"] == 0
    assert waiting["status"] == "queued"
    assert waiting["queue_position"] == 1


def test_resume_skips_completed_pages(tmp_path, monkeypatch):
    manager = _make_manager(tmp_path, monkeypatch)
    task = _create_task(manager)
    manager.engine.fail_on = 1

    claimed = manager.store.claim_next_queued()
    with pytest.raises(RuntimeError):
        manager._process_task(claimed)
    manager.store.fail_task(task["id"], "boom")

    failed = manager.get_task(task["id"])
    assert failed["status"] == "failed"
    assert failed["completed_pages"] == [0]

    manager.engine.fail_on = None
    manager.store.retry_task(task["id"])
    claimed = manager.store.claim_next_queued()
    assert manager._process_task(claimed)

    completed = manager.get_task(task["id"])
    assert completed["status"] == "completed"
    assert completed["completed_pages"] == [0, 1, 2]
    assert manager.engine.calls.count(0) == 1

    window = manager.page_window(task["id"], page=1, page_size=2)
    assert window["total_pages"] == 2
    assert window["items"][0]["completed"]
    assert window["items"][0]["result"]["page"] == 1

    markdown_path = os.path.join(
        completed["output_dir"], completed["markdown_name"]
    )
    with open(markdown_path, "r", encoding="utf-8") as f:
        assert "Page 1" in f.read()

    page_files = os.listdir(
        os.path.join(manager.history_dir, "pages", task["id"])
    )
    assert "0000.json" in page_files
    assert "0001.jpg" in page_files


def test_recover_running_task_requeues_it(tmp_path, monkeypatch):
    manager = _make_manager(tmp_path, monkeypatch)
    task = _create_task(manager)
    claimed = manager.store.claim_next_queued()
    assert claimed["status"] == "running"

    assert manager.store.recover_running() == 1
    recovered = manager.get_task(task["id"])
    assert recovered["status"] == "queued"
    assert recovered["current_page"] is None
