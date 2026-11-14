"""A project's override of the picture table (routing.image_policy): it takes effect everywhere at once, and the package's table is unchanged."""

from __future__ import annotations

import io
import json

import pytest

from rps import routing
from rps.config import Config, ConfigError
from rps.routing import ROUTE_COLUMNS, SOURCE_CLASSES, allowed_image, check_image_allowed, ImageRoutingError
from rps.tasks import SpecLine, TaskSpec

from harness import World, task_spec
from helpers import RecordingWriter, make_config

OPEN = {"routing": {"image_policy": {"deepfashion": {"claude": True, "openai": True, "gemini": True}}}}


def deepfashion_config(**extra):
    sources = {"photos": {"image_size": 64, "license": "CC BY-NC", "dataset": "photos", "image_class": "deepfashion"}}
    review = {
        "packet_size": 3,
        "reviewers": [{"id": "rev-a", "family": "openai"}, {"id": "rev-b", "family": "deepseek"}],
        "provider_families": {"fake": "writer-family"},
        "policy": {"audit_fraction": 0.0},
    }
    return make_config(sources=sources, review=review, **extra)


def test_the_default_table_refuses_deepfashion_for_every_column_and_the_config_adds_nothing():
    config = make_config()
    assert config.image_policy == {} and config.image_table() is routing.IMAGE_POLICY
    for column in ROUTE_COLUMNS:
        assert not allowed_image("deepfashion", column)
        assert not config.allowed_image("deepfashion", column)
        with pytest.raises(ImageRoutingError):
            config.check_image_allowed("deepfashion", column)
    assert "routing" not in config.to_dict() and "closed_tasks" not in config.to_dict()


def test_the_block_opens_the_named_columns_and_leaves_the_others_closed():
    config = make_config(**OPEN)
    for column in ("claude", "openai", "gemini"):
        assert config.allowed_image("deepfashion", column)
        config.check_image_allowed("deepfashion", column)
    assert not config.allowed_image("deepfashion", "deepseek")
    assert not config.allowed_image("hico", "claude")
    assert config.allowed_image("coco", "claude")
    assert not allowed_image("deepfashion", "claude")  # the package's own table is untouched
    assert Config.from_dict(config.to_dict()).image_table() == config.image_table()
    closed = make_config(routing={"image_policy": {"coco": {"claude": False}}})
    assert not closed.allowed_image("coco", "claude") and closed.allowed_image("coco", "openai")


@pytest.mark.parametrize(
    "block, text",
    [
        ({"image_policy": {"nonsense": {"claude": True}}}, "not a source class"),
        ({"image_policy": {"coco": {"llama": True}}}, "not a provider column"),
        ({"image_policy": {"coco": {"claude": "yes"}}}, "expected true or false"),
        ({"image_policy": {"coco": {"claude": 1}}}, "expected true or false"),
        ({"image_policy": {"synthetic": {"claude": True}}}, "can never be opened"),
        ({"image_policy": {"coco": True}}, "expected an object"),
        ({"image_policy": []}, "expected an object"),
        ({"images": {}}, "unknown"),
    ],
)
def test_a_bad_block_is_a_configuration_error_with_a_clear_message(block, text):
    with pytest.raises(ConfigError, match=text):
        make_config(routing=block)
    assert set(SOURCE_CLASSES) >= {"synthetic"}


def test_synthetic_can_be_closed_but_never_opened():
    config = make_config(routing={"image_policy": {"synthetic": {"claude": False}}})
    assert not config.allowed_image("synthetic", "claude")


def test_a_writer_call_carries_the_picture_where_the_block_allows_and_the_task_is_refused_where_it_does_not(tmp_path):
    world = World(tmp_path / "open", config=deepfashion_config(**OPEN), writer_class=RecordingWriter)
    controller = world.controller()
    controller.add_task(task_spec(("001",), image="p.jpg"))
    world.drive(controller)
    (call,) = controller.writers["fake-batch"].calls
    assert call.image is not None and call.image_class == "deepfashion"  # the adapters ask the same view of the table
    assert call.image_policy is not None and call.image_policy["deepfashion"]["openai"]
    from rps.providers.common import picture_for

    assert picture_for(call, "openai") is not None
    with pytest.raises(ImageRoutingError):
        picture_for(call, "deepseek")
    # without the block the same task is refused before anything is recorded
    plain = World(tmp_path / "plain", config=deepfashion_config(), writer_class=RecordingWriter).controller()
    with pytest.raises(ConfigError, match="may not be sent"):
        plain.add_task(task_spec(("001",), image="p.jpg"))


def first_packet_entry(tmp_path, family, image_class="deepfashion", **extra):
    review = {
        "packet_size": 3,
        "reviewers": [{"id": "rev-a", "family": family}],
        "provider_families": {"fake": "writer-family"},
        "policy": {"audit_fraction": 0.0},
    }
    sources = {"photos": {"image_size": 64, "license": "CC BY-NC", "dataset": "photos", "image_class": image_class}}
    world = World(tmp_path, config=make_config(sources=sources, review=review, **extra))
    controller = world.controller()
    controller.add_task(task_spec(("001",), image="p.jpg"))
    for _ in range(6):
        controller.step()
    paths = [path for path in world.state_store.list("packets/") if path.endswith("packet.json")]
    assert len(paths) == 1
    return json.loads(world.state_store.read_text(paths[0]))["rows"][0]


def test_a_review_packet_carries_the_picture_for_an_allowed_family_and_withholds_it_from_a_refused_one(tmp_path):
    allowed = first_packet_entry(tmp_path / "a", "openai", **OPEN)
    assert allowed["image"] and allowed["image_withheld"] is False
    refused = first_packet_entry(tmp_path / "b", "deepseek", **OPEN)  # a column the block leaves closed
    assert refused["image"] is None and refused["image_withheld"] is True


# ---------------------------------------------------------------------------- a museum's open-access pictures

MUSEUM_SOURCE = {"image_size": 64, "license": "CC0-1.0", "dataset": "museum-open", "image_class": "museum"}
MUSEUM_LINK = "https://images.museum.example/open-access/000123.jpg"


def test_a_museum_source_is_set_up_in_the_configuration_alone_and_an_unknown_class_is_still_refused(tmp_path):
    config = make_config(sources={"museum-open": dict(MUSEUM_SOURCE)})
    assert config.source("museum-open").image_class == "museum" and config.image_policy == {}  # no override is needed
    assert all(config.allowed_image("museum", column) for column in ROUTE_COLUMNS)
    assert SpecLine("001", "a vase", image="x.jpg", image_class="museum").image_class == "museum"  # a line may name it too
    for unknown in ("gallery", "Museum"):
        with pytest.raises(ConfigError, match="must be one of"):
            make_config(sources={"museum-open": dict(MUSEUM_SOURCE, image_class=unknown)})
        with pytest.raises(ConfigError, match="must be one of"):
            SpecLine("001", "a vase", image="x.jpg", image_class=unknown)
    closed = make_config(sources={"museum-open": dict(MUSEUM_SOURCE)}, routing={"image_policy": {"museum": {"deepseek": False}}})
    assert not closed.allowed_image("museum", "deepseek") and closed.allowed_image("museum", "claude")  # an owner may close a cell


def test_a_museum_picture_is_fetched_from_the_museums_own_host_and_may_go_to_every_provider(tmp_path):
    from PIL import Image

    from rps.pictures import MANIFEST_PREFIX, FetchingImageLoader
    from rps.providers.common import picture_for

    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), (120, 90, 60)).save(buffer, format="JPEG")
    config = make_config(sources={"museum-open": dict(MUSEUM_SOURCE, images_dir=str(tmp_path / "images"))})
    world = World(tmp_path / "run", config=config, writer_class=RecordingWriter)
    entry = {"image": "museum-open/000123.jpg", "urls": [MUSEUM_LINK]}
    world.state_store.write_bytes(f"{MANIFEST_PREFIX}museum.jsonl", (json.dumps(entry) + "\n").encode())
    asked = []

    def opener(url, *, timeout, max_bytes):
        asked.append(url)
        return 200, buffer.getvalue()

    loader = FetchingImageLoader(config, world.state_store, opener=opener, pause=0, sleep=lambda seconds: None)
    controller = world.controller(image_loader=loader)
    line = SpecLine("001", "spec for 001", item_id="object-000123", image="museum-open/000123.jpg")
    controller.add_task(TaskSpec(source="museum-open", batch="1", route="fake-batch", lines=(line,)))
    world.drive(controller)
    (call,) = controller.writers["fake-batch"].calls
    assert asked == [MUSEUM_LINK] and (tmp_path / "images" / "museum-open" / "000123.jpg").is_file()
    assert call.image == buffer.getvalue() and call.image_class == "museum"
    for provider in ("anthropic", "openai", "gemini", "deepseek"):
        assert picture_for(call, provider) == (call.image, "jpg")  # every adapter may send it


@pytest.mark.parametrize("family", ["anthropic", "openai", "google", "deepseek"])
def test_a_review_packet_carries_a_museum_picture_to_every_family(tmp_path, family):
    entry = first_packet_entry(tmp_path, family, image_class="museum")
    assert entry["image"] and entry["image_withheld"] is False
