import io
import os
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

import marker.scripts.server as server_mod
from marker.scripts.web_tasks import TaskManager, TaskStore

pytestmark = pytest.mark.cpu


class QueueFakeEngine:
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
        os.makedirs(images_folder, exist_ok=True)
        with open(os.path.join(images_folder, f"page_{n}.jpg"), "wb") as f:
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
            "img": display_image_url,
            "source": "surya",
            "mode_used": opts.get("mode", "auto"),
            "blocks": [],
            "lines": [],
            "markdown": f"# Page {n + 1}\n\n![](images/page_{n}.jpg)",
            "preview_html": f"<p>Page {n + 1}</p>",
            "html": "",
            "chunks": [],
            "json": {},
            "images_count": 1,
            "timings": {"total_ms": 1},
        }


def test_task_api_queue_and_paginated_results(tmp_path, monkeypatch):
    manager = TaskManager(
        QueueFakeEngine(),
        store=TaskStore(str(tmp_path / "history" / "tasks.sqlite3")),
        input_dir=str(tmp_path / "inputs"),
        output_dir=str(tmp_path / "outputs"),
        history_dir=str(tmp_path / "history"),
    )
    monkeypatch.setattr(server_mod, "task_manager", manager)
    monkeypatch.setattr(
        TaskManager, "_count_pages", staticmethod(lambda *args: 3)
    )
    client = TestClient(server_mod.app)

    try:
        response = client.post(
            "/api/tasks",
            params={
                "mode": "vllm",
                "dpi": 300,
                "max_pages": 1000,
                "page_range": "0-2",
            },
            content=b"fake-pdf",
            headers={"X-Filename": "%E6%B5%8B%E8%AF%95.pdf"},
        )
        assert response.status_code == 200
        task = response.json()
        assert task["status"] in {"queued", "running"}

        deadline = time.time() + 5
        while time.time() < deadline:
            current = client.get(f"/api/tasks/{task['id']}").json()
            if current["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)
        assert current["status"] == "completed"
        assert current["completed_count"] == 3

        history = client.get("/api/tasks").json()
        assert history[0]["id"] == task["id"]

        window = client.get(
            f"/api/tasks/{task['id']}/pages",
            params={"page": 1, "page_size": 2},
        ).json()
        assert window["total_pages"] == 2
        assert len(window["items"]) == 2
        assert all(item["completed"] for item in window["items"])
        assert window["items"][0]["result"]["page"] == 1

        page = client.get(f"/api/tasks/{task['id']}/pages/0")
        assert page.status_code == 200
        assert page.json()["markdown"].startswith("# Page 1")

        image = client.get(f"/api/tasks/{task['id']}/pages/0/image")
        assert image.status_code == 200
        assert image.content == b"display"

        download = client.get(f"/api/tasks/{task['id']}/download")
        assert download.status_code == 200
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            names = archive.namelist()
        assert "测试.md" in names
        assert "images/page_0.jpg" in names

        delete = client.delete(f"/api/tasks/{task['id']}")
        assert delete.status_code == 200
        assert delete.json()["success"] is True
        assert client.get("/api/tasks").json() == []
    finally:
        manager.stop()


def test_task_api_reorder_cancel_and_delete(tmp_path, monkeypatch):
    manager = TaskManager(
        QueueFakeEngine(),
        store=TaskStore(str(tmp_path / "history" / "tasks.sqlite3")),
        input_dir=str(tmp_path / "inputs"),
        output_dir=str(tmp_path / "outputs"),
        history_dir=str(tmp_path / "history"),
    )
    monkeypatch.setattr(server_mod, "task_manager", manager)
    monkeypatch.setattr(TaskManager, "start", lambda self: None)
    monkeypatch.setattr(
        TaskManager, "_count_pages", staticmethod(lambda *args: 1)
    )
    client = TestClient(server_mod.app)

    tasks = []
    for name in ("1.png", "2.png", "3.png"):
        response = client.post(
            "/api/tasks",
            params={"mode": "no_ocr", "dpi": 150, "max_pages": 10},
            content=b"image",
            headers={"X-Filename": name},
        )
        assert response.status_code == 200
        tasks.append(response.json())

    reordered = client.post(
        "/api/tasks/reorder",
        json={"task_ids": [tasks[2]["id"], tasks[0]["id"], tasks[1]["id"]]},
    )
    assert reordered.status_code == 200
    queued = [t["id"] for t in reordered.json() if t["status"] == "queued"]
    assert queued == [tasks[2]["id"], tasks[0]["id"], tasks[1]["id"]]

    cancelled = client.post(f"/api/tasks/{tasks[1]['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    deleted = client.delete(f"/api/tasks/{tasks[0]['id']}")
    assert deleted.status_code == 200
    remaining = [t["id"] for t in client.get("/api/tasks").json()]
    assert tasks[0]["id"] not in remaining
    assert tasks[1]["id"] in remaining
    assert tasks[2]["id"] in remaining
