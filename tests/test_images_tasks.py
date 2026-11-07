import io
import json

import pytest
from PIL import Image

from rps import ids
from rps.config import ConfigError
from rps.images import ImageError, load_picture, shrink_image
from rps.tasks import FilePlanner, SpecLine, TaskSpec, load_task_specs


def picture(size=(400, 200), mode="RGB", fmt="JPEG"):
    image = Image.new(mode, size, (200, 30, 30) if mode == "RGB" else (200, 30, 30, 128))
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def test_large_pictures_are_scaled_to_the_longest_side():
    data, ext, longest = shrink_image(picture((400, 200)), 100)
    assert ext == "jpg" and longest == 100
    result = Image.open(io.BytesIO(data))
    assert result.size == (100, 50)
    assert result.format == "JPEG"


def test_small_jpeg_and_png_are_returned_unchanged():
    jpeg = picture((60, 40))
    assert shrink_image(jpeg, 100) == (jpeg, "jpg", 60)
    png = picture((60, 40), fmt="PNG")
    assert shrink_image(png, 100) == (png, "png", 60)


def test_no_size_limit_keeps_the_original():
    jpeg = picture((400, 200))
    assert shrink_image(jpeg, None) == (jpeg, "jpg", 400)


def test_other_formats_become_jpeg_and_alpha_is_flattened():
    rgba = picture((300, 100), mode="RGBA", fmt="PNG")
    data, ext, longest = shrink_image(rgba, 150)
    assert ext == "jpg" and longest == 150
    assert Image.open(io.BytesIO(data)).mode == "RGB"
    webp = picture((50, 50), fmt="WEBP")
    data, ext, _ = shrink_image(webp, 100)
    assert ext == "jpg" and Image.open(io.BytesIO(data)).format == "JPEG"


def test_unreadable_pictures_raise_image_error(tmp_path):
    with pytest.raises(ImageError):
        shrink_image(b"not a picture", 100)
    with pytest.raises(ImageError):
        load_picture(tmp_path / "missing.jpg", 100)
    path = tmp_path / "p.jpg"
    path.write_bytes(picture((300, 300)))
    assert load_picture(path, 150)[2] == 150


def test_task_spec_id_matches_the_ids_module_and_checks_inputs():
    lines = (SpecLine("001", "a cat"), SpecLine("002", {"text": "a dog"}, item_id="img-2", license="MIT"))
    task = TaskSpec(source="photos", batch="1", route="r1", lines=lines)
    assert task.task_id == ids.task_id("photos", "1", {"001": "a cat", "002": {"text": "a dog"}})
    assert TaskSpec(source="photos", batch="1", route="r1", lines=lines, variant="r1").task_id != task.task_id
    for kwargs in (
        {"route": "bad route"},
        {"purpose": "fun"},
        {"lines": ()},
        {"source": ""},
        {"lines": (SpecLine("001", "a"), SpecLine("001", "b"))},
    ):
        base = dict(source="photos", batch="1", route="r1", lines=lines)
        base.update(kwargs)
        with pytest.raises((ConfigError, ValueError)):
            TaskSpec(**base)
    with pytest.raises(ValueError):
        SpecLine("bad_id", "x")


def test_spec_line_item_id_defaults_to_the_line_id():
    assert SpecLine("007", "x").to_dict()["item_id"] == "007"
    assert SpecLine("007", "x", item_id="img-9").to_dict()["item_id"] == "img-9"


def test_tasks_load_from_a_json_file_and_planner_rereads_it(tmp_path):
    path = tmp_path / "tasks.json"
    first = [{"source": "photos", "batch": "1", "route": "r1", "lines": [{"line_id": "001", "spec": "a"}]}]
    path.write_text(json.dumps(first), encoding="utf-8")
    planner = FilePlanner(path)
    assert [t.batch for t in planner(None)] == ["1"]
    second = first + [{"source": "photos", "batch": "2", "route": "r1", "purpose": "pricing", "lines": [{"line_id": "001", "spec": "b"}]}]
    path.write_text(json.dumps(second), encoding="utf-8")
    assert [(t.batch, t.purpose) for t in planner(None)] == [("1", "production"), ("2", "pricing")]
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_task_specs(path)
    with pytest.raises(ConfigError):
        load_task_specs(tmp_path / "missing.json")
    path.write_text(json.dumps([{"source": "photos"}]), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_task_specs(path)
