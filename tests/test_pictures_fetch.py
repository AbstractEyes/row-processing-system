"""The fetching image loader: a manifest in the state store, links tried in order, a picture kept on the disk. No network."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from rps.config import Config
from rps.images import ImageError
from rps.pictures import MANIFEST_PREFIX, FetchingImageLoader
from rps.store import LocalStore


def jpeg(size=(40, 20)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 200, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Opener:
    """A fake fetch function: answers by link from a script of (status, body) or an exception, and records every call."""

    def __init__(self, script):
        self.script = {url: list(steps) for url, steps in script.items()}
        self.calls = []

    def __call__(self, url, *, timeout, max_bytes):
        self.calls.append(url)
        steps = self.script.get(url, [(404, b"")])
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def world(tmp_path):
    store = LocalStore(tmp_path / "state", fsync=False)
    config = Config.from_dict({"work_dir": str(tmp_path / "work")})
    source = SimpleNamespace(images_dir=str(tmp_path / "images"), image_size=64)
    line = SimpleNamespace(image="coco/001.jpg")
    return SimpleNamespace(store=store, config=config, source=source, line=line, tmp=tmp_path, clock=Clock())


def manifest(world, name, *entries):
    world.store.write_bytes(f"{MANIFEST_PREFIX}{name}.jsonl", "".join(json.dumps(e) + "\n" for e in entries).encode())


def loader(world, opener, **kwargs):
    kwargs.setdefault("pause", 0)
    return FetchingImageLoader(world.config, world.store, opener=opener, clock=world.clock, sleep=lambda s: None, **kwargs)


def test_a_cached_file_is_loaded_and_never_fetched_again(world):
    manifest(world, "a", {"image": "coco/001.jpg", "urls": ["https://x/1.jpg"]})
    opener = Opener({"https://x/1.jpg": [(200, jpeg())]})
    load = loader(world, opener)
    first = load(world.source, world.line)
    assert first[1] == "jpg" and first[2] == 40
    assert (world.tmp / "images" / "coco" / "001.jpg").is_file()
    assert not list((world.tmp / "images" / "coco").glob("*.part"))
    assert load(world.source, world.line) == first
    assert opener.calls == ["https://x/1.jpg"]
    # a file already on the disk before the loader ran is not fetched at all
    other = SimpleNamespace(image="coco/002.jpg")
    (world.tmp / "images" / "coco" / "002.jpg").write_bytes(jpeg((10, 10)))
    assert load(world.source, other)[2] == 10 and opener.calls == ["https://x/1.jpg"]


def test_the_work_folder_holds_the_pictures_when_the_source_names_no_folder(world):
    manifest(world, "a", {"image": "coco/001.jpg", "urls": ["https://x/1.jpg"]})
    source = SimpleNamespace(images_dir=None, image_size=None)
    loader(world, Opener({"https://x/1.jpg": [(200, jpeg())]}))(source, world.line)
    assert (world.tmp / "work" / "images" / "coco" / "001.jpg").is_file()


def test_the_first_links_404_goes_to_the_second(world):
    manifest(world, "a", {"image": "coco/001.jpg", "urls": ["https://x/gone.jpg", "http://y/there.jpg"]})
    opener = Opener({"https://x/gone.jpg": [(404, b"")], "http://y/there.jpg": [(200, jpeg())]})
    assert loader(world, opener)(world.source, world.line)[2] == 40
    assert opener.calls == ["https://x/gone.jpg", "http://y/there.jpg"]  # a 404 is not tried again


def test_a_timeout_and_a_5xx_are_tried_again_and_then_succeed(world):
    manifest(world, "a", {"image": "coco/001.jpg", "urls": ["https://x/1.jpg"]})
    opener = Opener({"https://x/1.jpg": [TimeoutError("slow"), (503, b""), (200, jpeg())]})
    sleeps = []
    load = FetchingImageLoader(world.config, world.store, opener=opener, pause=0.5, sleep=sleeps.append, clock=world.clock)
    assert load(world.source, world.line)[2] == 40
    assert opener.calls == ["https://x/1.jpg"] * 3
    assert sleeps == [0.5, 1.0, 0.5]  # a growing wait between tries, then the pause after the write


def test_every_link_failing_raises_an_error_that_names_the_image_and_what_happened(world):
    manifest(world, "a", {"image": "coco/001.jpg", "urls": ["https://x/1.jpg", "https://y/2.jpg"]})
    opener = Opener({"https://x/1.jpg": [(403, b"")], "https://y/2.jpg": [TimeoutError("slow")]})
    with pytest.raises(ImageError) as caught:
        loader(world, opener, tries=2)(world.source, world.line)
    text = str(caught.value)
    assert "coco/001.jpg" in text and "HTTP 403" in text and "TimeoutError" in text and "2 tries" in text
    assert opener.calls == ["https://x/1.jpg", "https://y/2.jpg", "https://y/2.jpg"]
    assert not (world.tmp / "images" / "coco" / "001.jpg").exists()


def test_an_image_missing_from_the_manifest_raises_and_a_later_manifest_file_is_found_on_the_next_miss(world):
    opener = Opener({"https://x/1.jpg": [(200, jpeg())]})
    load = loader(world, opener)
    with pytest.raises(ImageError, match="no link"):
        load(world.source, world.line)
    manifest(world, "b", {"image": "coco/001.jpg", "urls": ["https://x/1.jpg"]})
    with pytest.raises(ImageError, match="no link"):
        load(world.source, world.line)  # read again at most once a minute: not yet
    world.clock.now += 61
    assert load(world.source, world.line)[2] == 40


def test_a_later_manifest_line_for_the_same_image_wins(world):
    manifest(world, "a", {"image": "coco/001.jpg", "urls": ["https://old/1.jpg"]}, {"image": "coco/001.jpg", "urls": ["https://new/1.jpg"]})
    manifest(world, "b", {"image": "coco/009.jpg", "urls": ["https://x/9.jpg"]})
    opener = Opener({"https://new/1.jpg": [(200, jpeg())]})
    loader(world, opener)(world.source, world.line)
    assert opener.calls == ["https://new/1.jpg"]


def test_a_link_that_is_not_http_or_https_is_refused(world):
    manifest(world, "a", {"image": "coco/001.jpg", "urls": ["file:///etc/passwd", "ftp://x/1.jpg"]})
    opener = Opener({})
    with pytest.raises(ImageError, match="not an http or https link"):
        loader(world, opener)(world.source, world.line)
    assert opener.calls == []


def test_a_body_over_the_limit_is_refused(world):
    manifest(world, "a", {"image": "coco/001.jpg", "urls": ["https://x/big.jpg"]})
    with pytest.raises(ImageError, match="larger than 100 bytes"):
        loader(world, Opener({"https://x/big.jpg": [(200, b"0" * 101)]}), max_bytes=100)(world.source, world.line)
    assert not (world.tmp / "images" / "coco" / "001.jpg").exists()


def test_bytes_that_are_not_a_picture_are_not_kept(world):
    manifest(world, "a", {"image": "coco/001.jpg", "urls": ["https://x/1.jpg"]})
    with pytest.raises(ImageError, match="coco/001.jpg"):
        loader(world, Opener({"https://x/1.jpg": [(200, b"<html>not a picture</html>")]}))(world.source, world.line)
    assert not (world.tmp / "images" / "coco" / "001.jpg").exists()


def test_a_store_link_reads_the_picture_from_the_state_store(world):
    world.store.write_bytes("private/pictures/001.jpg", jpeg())
    urls = ["store:private/pictures/missing.jpg", "store:private/pictures/001.jpg", "https://x/1.jpg"]
    manifest(world, "a", {"image": "coco/001.jpg", "urls": urls})
    opener = Opener({})
    assert loader(world, opener)(world.source, world.line)[2] == 40  # a missing path goes on to the next link
    assert opener.calls == []  # the second link answered before any web link was tried
    assert (world.tmp / "images" / "coco" / "001.jpg").is_file()


def test_a_store_link_goes_on_to_a_web_link_and_one_over_the_limit_is_refused(world):
    manifest(world, "a", {"image": "coco/001.jpg", "urls": ["store:nope/1.jpg", "https://x/1.jpg"]})
    assert loader(world, Opener({"https://x/1.jpg": [(200, jpeg())]}))(world.source, world.line)[2] == 40
    world.store.write_bytes("private/big.jpg", b"0" * 200)
    manifest(world, "b", {"image": "coco/002.jpg", "urls": ["store:private/big.jpg"]})
    with pytest.raises(ImageError, match="larger than 100 bytes"):
        loader(world, Opener({}), max_bytes=100)(world.source, SimpleNamespace(image="coco/002.jpg"))
