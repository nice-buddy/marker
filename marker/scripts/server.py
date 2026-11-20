import base64
from contextlib import asynccontextmanager
import io
import os
import tempfile
import threading
from typing import Annotated, Any, Dict, Literal, Optional
import urllib.parse

import click
from pydantic import BaseModel, Field

try:
    from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
    from fastapi.concurrency import run_in_threadpool
    from starlette.responses import HTMLResponse, JSONResponse, Response

    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
    FastAPI = None  # type: ignore[assignment]
    Response = Any  # type: ignore[assignment,misc]
    Request = Any  # type: ignore[assignment,misc]
    UploadFile = Any  # type: ignore[assignment,misc]

    class HTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int = 400, detail: str = ""):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

from marker.config.parser import ConfigParser
from marker.converters.pdf import PdfConverter
from marker.logger import get_logger
from marker.models import create_model_dict
from marker.output import text_from_rendered
from marker.scripts.viewer import MODES, ViewerEngine, render_viewer_html
from marker.scripts.web_tasks import TaskManager
from marker.settings import settings

logger = get_logger()

app_data: Dict[str, Any] = {}

UPLOAD_DIRECTORY = os.path.abspath("./uploads")

ALLOWED_Extensions = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".tiff",
    ".tif",
    ".bmp",
    ".pptx",
    ".docx",
    ".xlsx",
    ".html",
    ".htm",
    ".epub",
}

MAX_UPLOAD_SIZE = int(
    os.environ.get("MARKER_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024))
)


def _get_models() -> Dict[str, Any]:
    if "models" not in app_data or app_data["models"] is None:
        app_data["models"] = create_model_dict()
    return app_data["models"]


viewer_engine = ViewerEngine(model_supplier=_get_models)
task_manager: Optional[TaskManager] = None
task_manager_lock = threading.Lock()


def _query_bool(params, name: str) -> bool:
    return str(params.get(name, "")).lower() in {"1", "true", "yes", "on"}


def _get_task_manager() -> TaskManager:
    global task_manager
    with task_manager_lock:
        if task_manager is None:
            task_manager = TaskManager(viewer_engine)
        task_manager.start()
        return task_manager


def _get_allowed_dir() -> str:
    return os.path.realpath(os.environ.get("MARKER_ALLOWED_DIR", UPLOAD_DIRECTORY))


def _validate_local_filepath(filepath: str) -> str:
    if not filepath or not isinstance(filepath, str):
        raise HTTPException(status_code=400, detail="Filepath is required.")
    allowed_root = _get_allowed_dir()
    real_path = os.path.realpath(filepath)
    try:
        common = os.path.commonpath([real_path, allowed_root])
    except ValueError:
        raise HTTPException(
            status_code=403, detail="Access to the requested file path is not allowed."
        )
    if common != allowed_root:
        raise HTTPException(
            status_code=403, detail="Access to the requested file path is not allowed."
        )
    ext = os.path.splitext(real_path)[1].lower()
    if ext not in ALLOWED_Extensions:
        raise HTTPException(status_code=400, detail="Unsupported file type.")
    if not os.path.isfile(real_path):
        raise HTTPException(status_code=404, detail="File not found.")
    return real_path


@asynccontextmanager
async def lifespan(app: FastAPI):
    app_data["models"] = create_model_dict()
    _get_task_manager().start()

    yield

    if task_manager is not None:
        task_manager.stop()
    if "models" in app_data:
        from marker.models import shutdown_models

        shutdown_models(app_data["models"])
        del app_data["models"]


class ViewerPageParams(BaseModel):
    id: str
    n: int
    opts: Dict[str, Any] = Field(default_factory=dict)


class ViewerPrepareParams(BaseModel):
    id: str
    pages: list[int] = Field(default_factory=list)


class TaskReorderParams(BaseModel):
    task_ids: list[str] = Field(default_factory=list)


if _HAS_FASTAPI:
    app = FastAPI(
        title="Marker API & Interactive Viewer",
        version="2.2.1",
        lifespan=lifespan,
    )

    @app.get("/")
    async def root():
        return HTMLResponse(render_viewer_html())

    @app.get("/info")
    async def viewer_info():
        return viewer_engine.info(has_api_docs=True)

    @app.post("/upload")
    async def viewer_upload(request: Request):
        viewer_engine.max_upload_bytes = MAX_UPLOAD_SIZE
        raw_header = request.headers.get("X-Filename", "document.pdf")
        filename = urllib.parse.unquote(raw_header)
        body = await request.body()
        if len(body) > MAX_UPLOAD_SIZE:
            return JSONResponse(
                status_code=413,
                content={"error": "Uploaded file exceeds maximum allowed size."},
            )
        try:
            return await run_in_threadpool(viewer_engine.upload, body, filename)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        except Exception:
            logger.exception("Viewer upload failed")
            return JSONResponse(status_code=400, content={"error": "File upload failed."})

    @app.post("/page")
    async def viewer_page(params: ViewerPageParams):
        try:
            return await run_in_threadpool(
                viewer_engine.page, params.id, params.n, params.opts
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        except Exception as exc:
            logger.exception("Viewer page conversion failed")
            return JSONResponse(
                status_code=400, content={"error": f"Conversion failed: {exc}"}
            )

    @app.post("/prepare")
    async def viewer_prepare(params: ViewerPrepareParams):
        try:
            return await run_in_threadpool(
                viewer_engine.prepare, params.id, params.pages
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.get("/download")
    async def viewer_download(id: str):
        try:
            data, filename = await run_in_threadpool(
                viewer_engine.download_zip, id
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        headers = {
            "Content-Disposition": (
                "attachment; filename*=UTF-8''"
                + urllib.parse.quote(filename)
            )
        }
        return Response(
            content=data, media_type="application/zip", headers=headers
        )

    @app.post("/api/tasks")
    async def api_create_task(request: Request):
        filename = urllib.parse.unquote(
            request.headers.get("X-Filename", "document.pdf")
        )
        body = await request.body()
        if len(body) > MAX_UPLOAD_SIZE:
            return JSONResponse(
                status_code=413,
                content={"error": "上传文件超过大小限制。"},
            )
        query = request.query_params
        mode = str(query.get("mode") or "auto").lower()
        if mode not in MODES:
            return JSONResponse(
                status_code=400,
                content={"error": f"不支持的模式：{mode}"},
            )
        try:
            dpi = max(72, min(300, int(query.get("dpi") or 300)))
            max_pages = max(1, int(query.get("max_pages") or 1000))
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"error": "DPI 或最大页数格式不正确。"},
            )
        options = {
            "mode": mode,
            "dpi": dpi,
            "max_pages": max_pages,
            "page_range": query.get("page_range") or "",
            "force_ocr": _query_bool(query, "force_ocr"),
            "use_llm": _query_bool(query, "use_llm"),
            "strip_existing_ocr": _query_bool(
                query, "strip_existing_ocr"
            ),
            "keep_headers_footers": _query_bool(
                query, "keep_headers_footers"
            ),
        }
        try:
            return await run_in_threadpool(
                _get_task_manager().create_task,
                body,
                filename,
                options,
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        except Exception:
            logger.exception("Web task creation failed")
            return JSONResponse(
                status_code=400, content={"error": "创建转换任务失败。"}
            )

    @app.get("/api/tasks")
    async def api_list_tasks():
        return await run_in_threadpool(_get_task_manager().list_tasks)

    @app.post("/api/tasks/reorder")
    async def api_reorder_tasks(params: TaskReorderParams):
        try:
            return await run_in_threadpool(
                _get_task_manager().reorder_queued, params.task_ids
            )
        except ValueError as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.get("/api/tasks/{task_id}")
    async def api_get_task(task_id: str):
        try:
            return await run_in_threadpool(
                _get_task_manager().get_task, task_id
            )
        except ValueError as exc:
            return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.get("/api/tasks/{task_id}/pages")
    async def api_get_task_pages(
        task_id: str, page: int = 1, page_size: int = 10
    ):
        try:
            return await run_in_threadpool(
                _get_task_manager().page_window,
                task_id,
                page,
                page_size,
            )
        except ValueError as exc:
            return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.get("/api/tasks/{task_id}/pages/{page_index}")
    async def api_get_task_page(task_id: str, page_index: int):
        try:
            return JSONResponse(
                await run_in_threadpool(
                    _get_task_manager().page_result, task_id, page_index
                )
            )
        except ValueError as exc:
            return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.get("/api/tasks/{task_id}/pages/{page_index}/image")
    async def api_get_task_page_image(task_id: str, page_index: int):
        image_path = _get_task_manager().page_image_path(task_id, page_index)
        if not os.path.isfile(image_path):
            return JSONResponse(
                status_code=404, content={"error": "页面图片不存在。"}
            )
        with open(image_path, "rb") as f:
            return Response(content=f.read(), media_type="image/jpeg")

    @app.get("/api/tasks/{task_id}/download")
    async def api_download_task(task_id: str):
        try:
            data, filename = await run_in_threadpool(
                _get_task_manager().download_zip, task_id
            )
        except ValueError as exc:
            return JSONResponse(status_code=404, content={"error": str(exc)})
        headers = {
            "Content-Disposition": (
                "attachment; filename*=UTF-8''"
                + urllib.parse.quote(filename)
            )
        }
        return Response(
            content=data, media_type="application/zip", headers=headers
        )

    @app.post("/api/tasks/{task_id}/retry")
    async def api_retry_task(task_id: str):
        try:
            return await run_in_threadpool(
                _get_task_manager().retry_task, task_id
            )
        except ValueError as exc:
            return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.post("/api/tasks/{task_id}/cancel")
    async def api_cancel_task(task_id: str):
        try:
            return await run_in_threadpool(
                _get_task_manager().cancel_task, task_id
            )
        except ValueError as exc:
            return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.delete("/api/tasks/{task_id}")
    async def api_delete_task(task_id: str):
        try:
            await run_in_threadpool(
                _get_task_manager().delete_task, task_id
            )
            return {"success": True}
        except ValueError as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})
else:
    app = None


OutputFormat = Literal["markdown", "json", "html", "chunks"]
ConversionMode = Literal[
    "auto", "balanced", "fast", "indra", "no_ocr", "vllm", "surya", "accurate"
]
OcrBackend = Literal["auto", "surya", "indra"]


class CommonParams(BaseModel):
    filepath: Annotated[
        Optional[str],
        Field(
            description="The path to the file to convert (must reside in the allowed directory)."
        ),
    ]
    page_range: Annotated[
        Optional[str],
        Field(
            description="Page range to convert, specify comma separated page numbers or ranges. Example: 0,5-10,20",
            example=None,
        ),
    ] = None
    force_ocr: Annotated[
        bool,
        Field(
            description="Force OCR on all pages of the PDF. Defaults to False."
        ),
    ] = False
    disable_ocr: Annotated[
        bool,
        Field(
            description="Never call OCR — pure text-layer extraction. Defaults to False."
        ),
    ] = False
    use_llm: Annotated[
        bool,
        Field(
            description="Enable higher quality hybrid processing with LLMs. Defaults to False."
        ),
    ] = False
    strip_existing_ocr: Annotated[
        bool,
        Field(
            description="Strip existing OCR text from the PDF and re-OCR. Defaults to False."
        ),
    ] = False
    keep_headers_footers: Annotated[
        bool,
        Field(
            description="Keep running page headers and footers in the output. Defaults to False."
        ),
    ] = False
    paginate_output: Annotated[
        bool,
        Field(
            description="Whether to paginate the output. Defaults to False."
        ),
    ] = False
    output_format: Annotated[
        OutputFormat,
        Field(
            description="The format to output the text in. Can be 'markdown', 'json', 'html', or 'chunks'. Defaults to 'markdown'."
        ),
    ] = "markdown"
    mode: Annotated[
        Optional[ConversionMode],
        Field(
            description="Conversion mode: 'no_ocr', 'fast', 'balanced', or 'vllm' (or 'auto' for device default)."
        ),
    ] = None
    ocr_backend: Annotated[
        Optional[OcrBackend],
        Field(
            description="OCR backend: 'auto', 'indra', or 'surya'."
        ),
    ] = None


def _run_conversion_sync(params: CommonParams, resolved_path: str) -> dict:
    options = params.model_dump()
    options["filepath"] = resolved_path
    if options.get("mode") == "auto":
        options["mode"] = None
    if options.pop("keep_headers_footers", False):
        options["keep_pageheader_in_output"] = True
        options["keep_pagefooter_in_output"] = True
    config_parser = ConfigParser(options)
    config_dict = config_parser.generate_config_dict()
    config_dict["pdftext_workers"] = 1
    converter_cls = PdfConverter
    converter = converter_cls(
        config=config_dict,
        artifact_dict=_get_models(),
        processor_list=config_parser.get_processors(),
        renderer=config_parser.get_renderer(),
        llm_service=config_parser.get_llm_service(),
    )
    rendered = converter(resolved_path)
    text, _, images = text_from_rendered(rendered)
    metadata = rendered.metadata

    encoded = {}
    for k, v in images.items():
        byte_stream = io.BytesIO()
        v.save(byte_stream, format=settings.OUTPUT_IMAGE_FORMAT)
        encoded[k] = base64.b64encode(byte_stream.getvalue()).decode(
            settings.OUTPUT_ENCODING
        )

    return {
        "format": params.output_format,
        "output": text,
        "images": encoded,
        "metadata": metadata,
        "success": True,
    }


async def _convert_pdf(params: CommonParams):
    if params.output_format not in ("markdown", "json", "html", "chunks"):
        raise HTTPException(status_code=400, detail="Invalid output format.")
    resolved_path = _validate_local_filepath(params.filepath)
    try:
        return await run_in_threadpool(_run_conversion_sync, params, resolved_path)
    except Exception:
        logger.exception("Document conversion failed")
        return {
            "success": False,
            "error": "Document conversion failed.",
        }


if _HAS_FASTAPI:

    @app.post("/marker")
    async def convert_pdf(params: CommonParams):
        return await _convert_pdf(params)

    @app.post("/marker/upload")
    async def convert_pdf_upload(
        page_range: Optional[str] = Form(default=None),
        mode: Optional[ConversionMode] = Form(default=None),
        ocr_backend: Optional[OcrBackend] = Form(default=None),
        force_ocr: Optional[bool] = Form(default=False),
        disable_ocr: Optional[bool] = Form(default=False),
        use_llm: Optional[bool] = Form(default=False),
        strip_existing_ocr: Optional[bool] = Form(default=False),
        keep_headers_footers: Optional[bool] = Form(default=False),
        paginate_output: Optional[bool] = Form(default=False),
        output_format: Optional[OutputFormat] = Form(default="markdown"),
        file: UploadFile = File(
            ...,
            description="The PDF or document file to convert.",
            media_type="application/pdf",
        ),
    ):
        raw_name = os.path.basename(file.filename or "upload.pdf")
        ext = os.path.splitext(raw_name)[1].lower() or ".pdf"
        if ext not in ALLOWED_Extensions:
            raise HTTPException(status_code=400, detail="Unsupported file type.")

        os.makedirs(UPLOAD_DIRECTORY, exist_ok=True)
        fd, upload_path = tempfile.mkstemp(dir=UPLOAD_DIRECTORY, suffix=ext)
        try:
            total_bytes = 0
            with os.fdopen(fd, "wb") as upload_file:
                fd = None
                while True:
                    chunk = await file.read(65536)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > MAX_UPLOAD_SIZE:
                        raise HTTPException(
                            status_code=413,
                            detail="Uploaded file exceeds maximum allowed size.",
                        )
                    upload_file.write(chunk)

            params = CommonParams(
                filepath=upload_path,
                page_range=page_range,
                mode=mode,
                ocr_backend=ocr_backend,
                force_ocr=bool(force_ocr),
                disable_ocr=bool(disable_ocr),
                use_llm=bool(use_llm),
                strip_existing_ocr=bool(strip_existing_ocr),
                keep_headers_footers=bool(keep_headers_footers),
                paginate_output=bool(paginate_output),
                output_format=output_format or "markdown",
            )
            return await _convert_pdf(params)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if os.path.exists(upload_path):
                try:
                    os.remove(upload_path)
                except OSError:
                    pass


@click.command()
@click.option("--port", type=int, default=8000, help="Port to run the server on")
@click.option("--host", type=str, default="127.0.0.1", help="Host to run the server on")
def server_cli(port: int, host: str):
    if _HAS_FASTAPI and app is not None:
        try:
            import uvicorn

            print(
                f"Marker Server & Viewer running on http://{host}:{port} (API docs at http://{host}:{port}/docs)",
                flush=True,
            )
            uvicorn.run(app, host=host, port=port)
            return
        except ImportError:
            pass
    from marker.scripts.viewer import viewer_cli

    viewer_cli(port=port, host=host)
