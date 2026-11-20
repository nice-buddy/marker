from PIL import Image

from marker.output import save_output
from marker.renderers.html import HTMLOutput
from marker.renderers.markdown import MarkdownOutput


def test_save_output_uses_relative_images_folder(tmp_path):
    image_name = "_page_0_Picture_1.jpeg"
    rendered = MarkdownOutput(
        markdown=f"Before\n\n![picture]({image_name})\n\nAfter",
        images={image_name: Image.new("RGB", (8, 8), "white")},
        metadata={},
    )
    output_dir = tmp_path / "test"

    save_output(rendered, str(output_dir), "test")

    markdown = (output_dir / "test.md").read_text(encoding="utf-8")
    assert f"](images/{image_name})" in markdown
    assert (output_dir / "images" / image_name).is_file()


def test_save_output_updates_html_image_sources(tmp_path):
    image_name = "_page_0_Picture_1.jpeg"
    rendered = HTMLOutput(
        html=f"<img src='{image_name}'>",
        images={image_name: Image.new("RGB", (8, 8), "white")},
        metadata={},
    )
    output_dir = tmp_path / "test"

    save_output(rendered, str(output_dir), "test")

    html = (output_dir / "test.html").read_text(encoding="utf-8")
    assert f"src='images/{image_name}'" in html
