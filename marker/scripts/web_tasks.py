from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pypdfium2
from PIL import Image

from marker.logger import get_logger
from marker.providers.registry import provider_from_filepath
from marker.scripts.viewer import (
    ALLOWED_EXTENSIONS,
    IMAGE_EXTENSIONS,
    MODES,
    _parse_page_range_spec,
)
from marker.settings import settings

logger = get_logger()

_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_component(value: str, fallback: str = "document") -> str:
    cleaned = _INVALID_FILENAME_RE.sub("_", str(value or "")).strip().rstrip(". ")
    return cleaned or fallback


def _timestamp_ms() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _task_elapsed_seconds(task: Dict[str, Any]) -> float:
    started = task.get("started_at")
    if not started:
        return 0.0
    try:
        start = datetime.fromisoformat(started)
        finished = task.get("finished_at")
        end = (
            datetime.fromisoformat(finished)
            if finished
            else datetime.now().astimezone()
        )
        return max(0.0, (end - start).total_seconds())
    except Exception:
        return 0.0


class TaskStore:
    """SQLite-backed task and page completion store."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = os.path.abspath(
            db_path
            or os.path.join(settings.HISTORY_DIR, "marker_history.sqlite3")
        )
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    task_name TEXT NOT NULL UNIQUE,
                    original_filename TEXT NOT NULL,
                    input_path TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    markdown_name TEXT NOT NULL,
                    queue_order INTEGER NOT NULL DEFAULT 0,
                    total_pages INTEGER NOT NULL,
                    selected_pages TEXT NOT NULL,
                    status TEXT NOT NULL,
                    options TEXT NOT NULL,
                    current_page INTEGER,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_pages (
                    task_id TEXT NOT NULL,
                    page_index INTEGER NOT NULL,
                    result_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, page_index),
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_status_created
                    ON tasks(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_task_pages_task
                    ON task_pages(task_id, page_index);
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "queue_order" not in columns:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN queue_order "
                    "INTEGER NOT NULL DEFAULT 0"
                )
                conn.execute(
                    """
                    UPDATE tasks
                    SET queue_order = CAST(substr(task_name, 1, 17) AS INTEGER)
                    """
                )

    @staticmethod
    def _row_to_task(
        row: sqlite3.Row, completed_pages: Optional[List[int]] = None
    ) -> Dict[str, Any]:
        task = dict(row)
        task["selected_pages"] = json.loads(task.get("selected_pages") or "[]")
        task["options"] = json.loads(task.get("options") or "{}")
        task["completed_pages"] = completed_pages or []
        task["completed_count"] = len(task["completed_pages"])
        task["total_selected"] = len(task["selected_pages"])
        task["elapsed_seconds"] = round(_task_elapsed_seconds(task), 1)
        task["queue_position"] = None
        return task

    def _queue_positions(self, conn: sqlite3.Connection) -> Dict[str, int]:
        rows = conn.execute(
            """
            SELECT id, status
            FROM tasks
            WHERE status IN ('running', 'queued')
            ORDER BY
                CASE status WHEN 'running' THEN 0 ELSE 1 END,
                queue_order,
                task_name,
                id
            """
        ).fetchall()
        positions: Dict[str, int] = {}
        queued_position = 0
        for row in rows:
            if row["status"] == "running":
                positions[row["id"]] = 0
            else:
                queued_position += 1
                positions[row["id"]] = queued_position
        return positions

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            completed = [
                item["page_index"]
                for item in conn.execute(
                    """
                    SELECT page_index FROM task_pages
                    WHERE task_id = ? ORDER BY page_index
                    """,
                    (task_id,),
                ).fetchall()
            ]
            task = self._row_to_task(row, completed)
            task["queue_position"] = self._queue_positions(conn).get(task_id)
            return task

    def list_tasks(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.*,
                       (SELECT COUNT(*) FROM task_pages p WHERE p.task_id = t.id)
                           AS completed_count
                FROM tasks t
                ORDER BY
                    CASE t.status
                        WHEN 'running' THEN 0
                        WHEN 'queued' THEN 1
                        WHEN 'completed' THEN 3
                        ELSE 2
                    END,
                    CASE WHEN t.status = 'queued' THEN t.queue_order ELSE 0 END,
                    CASE WHEN t.status != 'queued' THEN t.task_name ELSE '' END DESC,
                    t.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            positions = self._queue_positions(conn)
            tasks = []
            for row in rows:
                task = self._row_to_task(row)
                task["completed_count"] = int(row["completed_count"] or 0)
                task["queue_position"] = positions.get(task["id"])
                tasks.append(task)
            return tasks

    def create_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, task_name, original_filename, input_path, output_dir,
                    markdown_name, queue_order, total_pages, selected_pages,
                    status, options, current_page, error, created_at,
                    started_at, finished_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task["id"],
                    task["task_name"],
                    task["original_filename"],
                    task["input_path"],
                    task["output_dir"],
                    task["markdown_name"],
                    task.get("queue_order", 0),
                    task["total_pages"],
                    _dump_json(task["selected_pages"]),
                    task["status"],
                    _dump_json(task["options"]),
                    task.get("current_page"),
                    task.get("error"),
                    task["created_at"],
                    task.get("started_at"),
                    task.get("finished_at"),
                    task["updated_at"],
                ),
            )
        return self.get_task(task["id"])

    def claim_next_queued(self) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            running = conn.execute(
                "SELECT 1 FROM tasks WHERE status = 'running' LIMIT 1"
            ).fetchone()
            if running is not None:
                conn.commit()
                return None
            row = conn.execute(
                """
                SELECT id FROM tasks
                WHERE status = 'queued'
                ORDER BY queue_order, task_name, id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            now = _now_iso()
            conn.execute(
                """
                UPDATE tasks
                SET status = 'running',
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?,
                    error = NULL
                WHERE id = ? AND status = 'queued'
                """,
                (now, now, row["id"]),
            )
            conn.commit()
            return self.get_task(row["id"])
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def set_current_page(self, task_id: str, page_index: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET current_page = ?, updated_at = ?
                WHERE id = ?
                """,
                (page_index, _now_iso(), task_id),
            )

    def mark_page_complete(
        self, task_id: str, page_index: int, result_path: str
    ) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO task_pages (
                    task_id, page_index, result_path, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (task_id, page_index, result_path, now),
            )
            conn.execute(
                """
                UPDATE tasks
                SET current_page = ?, updated_at = ?
                WHERE id = ?
                """,
                (page_index, now, task_id),
            )

    def remove_page(self, task_id: str, page_index: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM task_pages WHERE task_id = ? AND page_index = ?",
                (task_id, page_index),
            )

    def page_result_path(self, task_id: str, page_index: int) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT result_path FROM task_pages
                WHERE task_id = ? AND page_index = ?
                """,
                (task_id, page_index),
            ).fetchone()
            return row["result_path"] if row else None

    def complete_task(self, task_id: str) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'completed', current_page = NULL,
                    finished_at = ?, updated_at = ?, error = NULL
                WHERE id = ?
                """,
                (now, now, task_id),
            )

    def fail_task(self, task_id: str, error: str) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'failed', finished_at = ?, updated_at = ?,
                    error = ?
                WHERE id = ?
                """,
                (now, now, error[:4000], task_id),
            )

    def retry_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'queued', error = NULL, finished_at = NULL,
                    started_at = NULL, current_page = NULL, updated_at = ?
                WHERE id = ? AND status IN ('failed', 'queued', 'cancelled')
                """,
                (_now_iso(), task_id),
            )
        return self.get_task(task_id)

    def update_queued_task(
        self,
        task_id: str,
        options: Dict[str, Any],
        selected_pages: List[int],
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT t.status,
                       (SELECT COUNT(*) FROM task_pages p WHERE p.task_id = t.id)
                           AS completed_count
                FROM tasks t
                WHERE t.id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] != "queued" or row["completed_count"]:
                raise ValueError("只有尚未开始的排队任务可以修改参数。")
            conn.execute(
                """
                UPDATE tasks
                SET options = ?, selected_pages = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (
                    _dump_json(options),
                    _dump_json(selected_pages),
                    _now_iso(),
                    task_id,
                ),
            )
        return self.get_task(task_id)

    def cancel_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] in ("running", "cancelling"):
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'cancelling', error = '正在终止任务',
                        updated_at = ?
                    WHERE id = ? AND status IN ('running', 'cancelling')
                    """,
                    (now, task_id),
                )
            elif row["status"] == "queued":
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'cancelled', error = '任务已取消',
                        finished_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (now, now, task_id),
                )
        return self.get_task(task_id)

    def mark_cancelled(self, task_id: str) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'cancelled', error = '任务已取消',
                    finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, task_id),
            )

    def recover_running(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = 'queued', current_page = NULL, updated_at = ?
                WHERE status = 'running'
                """,
                (_now_iso(),),
            )
            conn.execute(
                """
                UPDATE tasks
                SET status = 'cancelled', error = '任务已取消',
                    finished_at = ?, updated_at = ?
                WHERE status = 'cancelling'
                """,
                (_now_iso(), _now_iso()),
            )
            return cursor.rowcount

    def reorder_queued(self, task_ids: List[str]) -> bool:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM tasks
                WHERE status = 'queued'
                ORDER BY queue_order, task_name, id
                """
            ).fetchall()
            current_ids = [row["id"] for row in rows]
            if len(task_ids) != len(current_ids) or set(task_ids) != set(
                current_ids
            ):
                raise ValueError("排队任务列表已变化，请刷新后重试。")
            now = _now_iso()
            for index, task_id in enumerate(task_ids, start=1):
                conn.execute(
                    """
                    UPDATE tasks
                    SET queue_order = ?, updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (index, now, task_id),
                )
            return True

    def delete_task(self, task_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return False
            if row["status"] in ("running", "cancelling"):
                raise ValueError("请先终止正在运行的任务，再删除。")
            conn.execute(
                "DELETE FROM task_pages WHERE task_id = ?", (task_id,)
            )
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return True


class TaskManager:
    """Single-worker conversion queue with persisted per-page results."""

    def __init__(
        self,
        engine,
        store: Optional[TaskStore] = None,
        input_dir: Optional[str] = None,
        output_dir: Optional[str] = None,
        history_dir: Optional[str] = None,
    ):
        self.engine = engine
        self.store = store or TaskStore()
        self.input_dir = os.path.abspath(input_dir or settings.INPUT_DIR)
        self.output_dir = os.path.abspath(output_dir or settings.OUTPUT_DIR)
        self.history_dir = os.path.abspath(history_dir or settings.HISTORY_DIR)
        self.page_root = os.path.join(self.history_dir, "pages")
        os.makedirs(self.input_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.page_root, exist_ok=True)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lifecycle_lock = threading.Lock()
        self._timestamp_lock = threading.Lock()
        self._last_timestamp = ""
        self._cancel_events: Dict[str, threading.Event] = {}

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._cancel_events.clear()
            self.store.recover_running()
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="marker-web-tasks",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _next_timestamp(self) -> str:
        with self._timestamp_lock:
            while True:
                timestamp = _timestamp_ms()
                if timestamp != self._last_timestamp:
                    self._last_timestamp = timestamp
                    return timestamp
                time.sleep(0.001)

    def create_task(
        self,
        data: bytes,
        filename: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not data:
            raise ValueError("上传文件为空。")

        options = dict(options or {})
        raw_name = os.path.basename(filename or "document.pdf")
        ext = os.path.splitext(raw_name)[1].lower()
        if not ext:
            ext = ".pdf" if data[:5] == b"%PDF-" else ".png"
        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(f"不支持的文件类型：{ext}")

        stem = _safe_component(Path(raw_name).stem, "document")
        timestamp = self._next_timestamp()
        task_name = f"{timestamp}_{stem}{ext}"
        input_name = f"{stem}_{timestamp}{ext}"
        input_path = os.path.join(self.input_dir, input_name)
        task_id = uuid.uuid4().hex
        output_dir = os.path.join(self.output_dir, _safe_component(task_name))
        markdown_name = f"{stem}.md"
        page_dir = os.path.join(self.page_root, task_id)

        os.makedirs(os.path.dirname(input_path), exist_ok=True)
        with open(input_path, "wb") as f:
            f.write(data)

        try:
            total_pages = self._count_pages(input_path, ext, data)
            page_range = str(options.get("page_range") or "")
            max_pages = max(1, int(options.get("max_pages") or 1000))
            selected_pages = _parse_page_range_spec(
                page_range, total_pages, max_pages
            )
            if not selected_pages:
                selected_pages = [0]
            os.makedirs(os.path.join(output_dir, "images"), exist_ok=True)
            os.makedirs(page_dir, exist_ok=True)

            normalized_options = {
                "mode": str(options.get("mode") or "auto"),
                "dpi": int(options.get("dpi") or 300),
                "max_pages": max_pages,
                "page_range": page_range,
                "force_ocr": bool(options.get("force_ocr", False)),
                "use_llm": bool(options.get("use_llm", False)),
                "strip_existing_ocr": bool(
                    options.get("strip_existing_ocr", False)
                ),
                "keep_headers_footers": bool(
                    options.get("keep_headers_footers", False)
                ),
            }
            now = _now_iso()
            task = {
                "id": task_id,
                "task_name": task_name,
                "original_filename": raw_name,
                "input_path": input_path,
                "output_dir": output_dir,
                "markdown_name": markdown_name,
                "queue_order": int(timestamp),
                "total_pages": total_pages,
                "selected_pages": selected_pages,
                "status": "queued",
                "options": normalized_options,
                "current_page": None,
                "error": None,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "updated_at": now,
            }
            task = self.store.create_task(task)
        except Exception:
            try:
                os.remove(input_path)
            except OSError:
                pass
            raise

        self._wake.set()
        return task

    @staticmethod
    def _count_pages(filepath: str, ext: str, data: bytes) -> int:
        if ext == ".pdf" or data[:5] == b"%PDF-":
            pdf_doc = pypdfium2.PdfDocument(filepath)
            try:
                return max(1, len(pdf_doc))
            finally:
                pdf_doc.close()
        if ext in IMAGE_EXTENSIONS:
            with Image.open(filepath) as image:
                return max(1, int(getattr(image, "n_frames", 1)))
        provider_cls = provider_from_filepath(filepath)
        with provider_cls(filepath) as provider:
            return max(1, len(provider))

    def list_tasks(self) -> List[Dict[str, Any]]:
        return self.store.list_tasks()

    def get_task(self, task_id: str) -> Dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError("任务不存在。")
        return task

    def retry_task(self, task_id: str) -> Dict[str, Any]:
        self._cancel_events.pop(task_id, None)
        task = self.store.retry_task(task_id)
        if task is None:
            raise ValueError("任务不存在。")
        self._wake.set()
        return task

    def update_task_options(
        self, task_id: str, options: Dict[str, Any]
    ) -> Dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] != "queued" or task["completed_pages"]:
            raise ValueError("只有尚未开始的排队任务可以修改参数。")

        mode = str(options.get("mode") or "auto").lower()
        if mode not in MODES:
            raise ValueError(f"不支持的模式：{mode}")
        dpi = max(72, min(300, int(options.get("dpi") or 300)))
        max_pages = max(1, int(options.get("max_pages") or 1000))
        page_range = str(options.get("page_range") or "")
        selected_pages = _parse_page_range_spec(
            page_range, task["total_pages"], max_pages
        )
        if not selected_pages:
            raise ValueError("页码范围没有有效页面。")
        normalized = {
            "mode": mode,
            "dpi": dpi,
            "max_pages": max_pages,
            "page_range": page_range,
            "force_ocr": bool(options.get("force_ocr", False)),
            "use_llm": bool(options.get("use_llm", False)),
            "strip_existing_ocr": bool(
                options.get("strip_existing_ocr", False)
            ),
            "keep_headers_footers": bool(
                options.get("keep_headers_footers", False)
            ),
        }
        updated = self.store.update_queued_task(
            task_id, normalized, selected_pages
        )
        if updated is None:
            raise ValueError("任务不存在。")
        return updated

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        task = self.store.cancel_task(task_id)
        if task is None:
            raise ValueError("任务不存在。")
        if task["status"] in ("running", "cancelling"):
            self._cancel_events.setdefault(
                task_id, threading.Event()
            ).set()
        return task

    def delete_task(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if task["status"] in ("running", "cancelling"):
            raise ValueError("请先终止正在运行的任务，再删除。")
        if not self.store.delete_task(task_id):
            raise ValueError("任务不存在。")
        self._cancel_events.pop(task_id, None)
        for path in (
            task.get("input_path"),
            task.get("output_dir"),
            os.path.join(self.page_root, task_id),
        ):
            if not path:
                continue
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.isfile(path):
                    os.remove(path)
            except OSError as exc:
                logger.warning(f"Failed to remove task path {path}: {exc}")
        return True

    def reorder_queued(self, task_ids: List[str]) -> List[Dict[str, Any]]:
        self.store.reorder_queued(task_ids)
        return self.list_tasks()

    def page_result(self, task_id: str, page_index: int) -> Dict[str, Any]:
        path = self.store.page_result_path(task_id, page_index)
        if not path or not os.path.isfile(path):
            raise ValueError("该页尚未转换完成。")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def page_window(
        self, task_id: str, page: int = 1, page_size: int = 10
    ) -> Dict[str, Any]:
        task = self.get_task(task_id)
        page_size = max(1, min(50, int(page_size)))
        selected_pages = task["selected_pages"]
        total_selected = len(selected_pages)
        total_pages = max(1, math.ceil(total_selected / page_size))
        page = max(1, min(total_pages, int(page)))
        start = (page - 1) * page_size
        completed = set(task["completed_pages"])
        items = []
        for page_index in selected_pages[start : start + page_size]:
            result = None
            if page_index in completed:
                try:
                    result = self.page_result(task_id, page_index)
                except ValueError:
                    result = None
            items.append(
                {
                    "page_index": page_index,
                    "page": page_index + 1,
                    "completed": result is not None,
                    "result": result,
                }
            )
        return {
            "task": task,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total_selected": total_selected,
            "completed_count": len(completed),
            "items": items,
        }

    def page_image_path(self, task_id: str, page_index: int) -> str:
        return os.path.join(
            self.page_root, task_id, f"{int(page_index):04d}.jpg"
        )

    def download_zip(self, task_id: str) -> tuple[bytes, str]:
        task = self.get_task(task_id)
        markdown_path = os.path.join(
            task["output_dir"], task["markdown_name"]
        )
        if not os.path.isfile(markdown_path):
            raise ValueError("该任务尚无可下载的结果。")

        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(markdown_path, arcname=task["markdown_name"])
            images_dir = os.path.join(task["output_dir"], "images")
            image_names = []
            if os.path.isdir(images_dir):
                image_names = sorted(
                    name
                    for name in os.listdir(images_dir)
                    if os.path.isfile(os.path.join(images_dir, name))
                )
            for image_name in image_names:
                zf.write(
                    os.path.join(images_dir, image_name),
                    arcname=f"images/{image_name}",
                )
            if not image_names:
                zf.writestr("images/", b"")
        return archive.getvalue(), f"{task['task_name']}.zip"

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            task = self.store.claim_next_queued()
            if task is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            try:
                finished = self._process_task(task)
                if not finished:
                    break
            except Exception as exc:
                logger.error(f"Web task failed: {task['id']}: {exc}")
                logger.error(traceback.format_exc())
                self.store.fail_task(task["id"], str(exc))
            finally:
                self._cancel_events.pop(task["id"], None)

    def _process_task(self, task: Dict[str, Any]) -> bool:
        task_id = task["id"]
        page_dir = os.path.join(self.page_root, task_id)
        os.makedirs(page_dir, exist_ok=True)
        images_dir = os.path.join(task["output_dir"], "images")
        os.makedirs(images_dir, exist_ok=True)

        completed = set(task["completed_pages"])
        page_markdowns: Dict[int, str] = {}
        for page_index in sorted(completed):
            path = self.store.page_result_path(task_id, page_index)
            if (
                not path
                or not os.path.isfile(path)
                or not os.path.isfile(self.page_image_path(task_id, page_index))
            ):
                completed.discard(page_index)
                self.store.remove_page(task_id, page_index)
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    page_markdowns[page_index] = json.load(f).get(
                        "markdown", ""
                    )
            except Exception:
                completed.discard(page_index)
                self.store.remove_page(task_id, page_index)

        self._write_markdown(task, page_markdowns)
        if self._is_cancelled(task_id):
            self.store.mark_cancelled(task_id)
            return True

        for page_index in task["selected_pages"]:
            if self._stop.is_set():
                return False
            if self._is_cancelled(task_id):
                self.store.mark_cancelled(task_id)
                return True
            if page_index in completed:
                continue

            self.store.set_current_page(task_id, page_index)
            image_path = os.path.join(page_dir, f"{page_index:04d}.jpg")
            result = self.engine.convert_page(
                filepath=task["input_path"],
                total_pages=task["total_pages"],
                n=page_index,
                opts=task["options"],
                images_folder=images_dir,
                display_image_path=image_path,
                display_image_url=(
                    f"/api/tasks/{task_id}/pages/{page_index}/image"
                ),
            )
            result_path = os.path.join(
                page_dir, f"{page_index:04d}.json"
            )
            with open(result_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, separators=(",", ":"))

            self.store.mark_page_complete(task_id, page_index, result_path)
            completed.add(page_index)
            page_markdowns[page_index] = result.get("markdown", "")
            self._write_markdown(task, page_markdowns)
            if self._is_cancelled(task_id):
                self.store.mark_cancelled(task_id)
                return True

        if self._is_cancelled(task_id):
            self.store.mark_cancelled(task_id)
            return True
        self.store.complete_task(task_id)
        return True

    def _is_cancelled(self, task_id: str) -> bool:
        event = self._cancel_events.get(task_id)
        return bool(event and event.is_set())

    @staticmethod
    def _write_markdown(
        task: Dict[str, Any], page_markdowns: Dict[int, str]
    ) -> None:
        os.makedirs(task["output_dir"], exist_ok=True)
        markdown = "\n\n---\n\n".join(
            page_markdowns[page]
            for page in task["selected_pages"]
            if page in page_markdowns
        )
        path = os.path.join(task["output_dir"], task["markdown_name"])
        with open(path, "w", encoding=settings.OUTPUT_ENCODING) as f:
            f.write(markdown)
