import io
import json

import pytest
from PIL import Image

from rps import ids, images
from rps.config import ConfigError
from rps.images import ENLARGED_MAX_SIDE, ImageError, enlarge_image, load_picture, shrink_image
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


def test_a_picture_is_enlarged_by_the_scale_and_keeps_its_proportions():
    data, ext, size = enlarge_image(picture((400, 200)), 2)
    assert ext == "jpg" and size == (800, 400)
    result = Image.open(io.BytesIO(data))
    assert result.size == (800, 400) and result.format == "JPEG"
    data, ext, size = enlarge_image(picture((300, 101), fmt="PNG"), 1.5)
    assert ext == "jpg" and size == (450, 152) and Image.open(io.BytesIO(data)).size == (450, 152)


def test_no_side_of_an_enlarged_picture_passes_the_longest_side():
    data, ext, size = enlarge_image(picture((2000, 1000)), 2)
    assert size == (ENLARGED_MAX_SIDE, ENLARGED_MAX_SIDE // 2) and Image.open(io.BytesIO(data)).size == size
    data, ext, size = enlarge_image(picture((100, 400)), 4, max_side=1000)
    assert size == (250, 1000)


def test_a_picture_that_cannot_grow_is_returned_as_it_came():
    jpeg = picture((400, 200))
    assert enlarge_image(jpeg, 1) == (jpeg, "jpg", (400, 200))
    assert enlarge_image(jpeg, 0.5) == (jpeg, "jpg", (400, 200))
    large = picture((3000, 1000))
    assert enlarge_image(large, 2) == (large, "jpg", (3000, 1000))
    png = picture((60, 40), fmt="PNG")
    assert enlarge_image(png, 1) == (png, "png", (60, 40))
    webp = picture((50, 50), fmt="WEBP")  # another format still leaves as a JPEG of its own size
    data, ext, size = enlarge_image(webp, 1)
    assert ext == "jpg" and size == (50, 50) and Image.open(io.BytesIO(data)).format == "JPEG"


def test_an_enlarged_picture_is_not_turned_and_keeps_its_orientation_mark():
    marked = io.BytesIO()
    mark = Image.Exif()
    mark[images.ORIENTATION_TAG] = 6  # a viewer that heeds the mark turns this picture a quarter
    Image.new("RGB", (400, 100), (200, 30, 30)).save(marked, format="JPEG", exif=mark.tobytes())
    data, ext, size = enlarge_image(marked.getvalue(), 2)
    result = Image.open(io.BytesIO(data))
    assert size == (800, 200) and result.size == (800, 200)  # the stored pixels, enlarged: not turned
    assert result.getexif().get(images.ORIENTATION_TAG) == 6  # and the mark is carried over
    assert enlarge_image(marked.getvalue(), 1)[0] == marked.getvalue()  # a picture that does not grow keeps every byte
    plain, _, _ = enlarge_image(picture((400, 100)), 2)
    assert Image.open(io.BytesIO(plain)).getexif().get(images.ORIENTATION_TAG) is None  # no mark is made up


def test_an_enlarged_picture_is_held_under_the_size_limit_or_refused(monkeypatch):
    noisy = Image.effect_noise((600, 400), 80).convert("RGB")
    buffer = io.BytesIO()
    noisy.save(buffer, format="JPEG", quality=95)
    best, _, _ = enlarge_image(buffer.getvalue(), 2)
    monkeypatch.setattr(images, "ENLARGED_MAX_BYTES", len(best) - 1)  # the first quality no longer fits
    lower, ext, size = enlarge_image(buffer.getvalue(), 2)
    assert ext == "jpg" and size == (1200, 800) and len(lower) < len(best)
    monkeypatch.setattr(images, "ENLARGED_MAX_BYTES", 10)  # nothing fits
    with pytest.raises(ImageError, match="larger than"):
        enlarge_image(buffer.getvalue(), 2)


def test_an_enlarged_picture_with_alpha_is_flattened_and_bad_bytes_raise():
    data, ext, size = enlarge_image(picture((100, 50), mode="RGBA", fmt="PNG"), 2)
    assert ext == "jpg" and size == (200, 100) and Image.open(io.BytesIO(data)).mode == "RGB"
    with pytest.raises(ImageError):
        enlarge_image(b"not a picture", 2)


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
