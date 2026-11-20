import io
import os
import zipfile

from marker.scripts.viewer import ViewerEngine


def _add_document(engine: ViewerEngine, doc_id: str, stem: str):
    out_folder = os.path.join(engine.output_root, stem)
    engine.docs[doc_id] = {
        "id": doc_id,
        "path": "",
        "filename": f"{stem}.pdf",
        "ext": ".pdf",
        "pages": 2,
        "stem": stem,
        "out_folder": out_folder,
        "images_folder": os.path.join(out_folder, "images"),
        "page_order": [],
        "saved_pages": {},
    }
    return out_folder


def test_viewer_download_zip_contains_markdown_and_images(tmp_path):
    engine = ViewerEngine(output_root=str(tmp_path))
    out_folder = _add_document(engine, "doc", "test")

    engine.prepare("doc", [0, 1])
    with open(
        os.path.join(out_folder, "test.md"), "w", encoding="utf-8"
    ) as markdown_file:
        markdown_file.write("![picture](images/picture.jpeg)")
    with open(
        os.path.join(out_folder, "images", "picture.jpeg"), "wb"
    ) as image_file:
        image_file.write(b"image")

    data, filename = engine.download_zip("doc")

    assert filename == "test.zip"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["test.md", "images/picture.jpeg"]


def test_viewer_download_zip_includes_empty_images_folder(tmp_path):
    engine = ViewerEngine(output_root=str(tmp_path))
    out_folder = _add_document(engine, "doc", "test")

    engine.prepare("doc", [0])
    with open(
        os.path.join(out_folder, "test.md"), "w", encoding="utf-8"
    ) as markdown_file:
        markdown_file.write("No images")

    data, _ = engine.download_zip("doc")

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["test.md", "images/"]
