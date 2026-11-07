"""The picture routing table: which classes of picture may go to which provider."""

import pytest

from rps.routing import (
    IMAGE_POLICY,
    SOURCE_CLASSES,
    ImageRoutingError,
    allowed_image,
    check_image_allowed,
    route_column,
)

ALLOWED = {
    "public_common": {"openai", "gemini", "deepseek"},
    "coco": {"openai", "gemini", "deepseek"},
    "ffhq": {"openai", "gemini", "deepseek"},
    "deepfashion": set(),
    "hico": set(),
    "synthetic": set(),
}


@pytest.mark.parametrize("source_class", SOURCE_CLASSES)
@pytest.mark.parametrize("provider, column", [("anthropic", "claude"), ("openai", "openai"), ("gemini", "gemini"), ("deepseek", "deepseek")])
def test_the_table_matches_the_specification_for_every_class_and_provider(source_class, provider, column):
    assert allowed_image(source_class, provider) is (column in ALLOWED[source_class])


def test_a_claude_route_never_gets_a_picture_of_any_class():
    for source_class in SOURCE_CLASSES:
        for route in ("anthropic", "claude", "haiku", "sonnet", "haiku-batch", "claude-haiku-5-5"):
            assert allowed_image(source_class, route) is False


def test_deepfashion_and_hico_never_reach_any_route():
    for source_class in ("deepfashion", "hico"):
        for route in ("anthropic", "openai", "gemini", "deepseek", "luna-batch", "flash-batch", "deepseek-sync", "fake"):
            assert allowed_image(source_class, route) is False
            with pytest.raises(ImageRoutingError):
                check_image_allowed(source_class, route)


def test_a_class_missing_from_the_table_is_refused():
    for unknown in ("mystery", "", None, 7, "PUBLIC_COMMON"):
        assert allowed_image(unknown, "openai") is False
    with pytest.raises(ImageRoutingError, match="not in the routing table"):
        check_image_allowed("mystery", "openai")


def test_a_picture_needs_a_class():
    with pytest.raises(ImageRoutingError, match="needs a source class"):
        check_image_allowed(None, "openai")
    with pytest.raises(ImageRoutingError, match="needs a source class"):
        check_image_allowed("", "openai")


def test_a_route_that_is_not_recognized_is_refused():
    for unknown in ("mystery", "", None, 7):
        assert allowed_image("public_common", unknown) is False
    with pytest.raises(ImageRoutingError, match="not in the routing table"):
        check_image_allowed("public_common", "mystery-route")


@pytest.mark.parametrize(
    "name, column",
    [
        ("anthropic", "claude"), ("haiku-batch", "claude"), ("Claude", "claude"),
        ("openai", "openai"), ("luna-batch", "openai"), ("luna_sync", "openai"), ("sol", "openai"),
        ("gemini", "gemini"), ("flash-batch", "gemini"), ("argon.sync", "gemini"),
        ("deepseek", "deepseek"), ("deepseek-sync", "deepseek"),
        ("fake", "openai"), ("fake-batch", "openai"),
    ],
)
def test_route_names_map_to_their_column(name, column):
    assert route_column(name) == column


def test_the_policy_table_has_a_row_for_every_class_and_a_cell_for_every_column():
    assert set(IMAGE_POLICY) == set(SOURCE_CLASSES)
    for row in IMAGE_POLICY.values():
        assert set(row) == {"claude", "openai", "gemini", "deepseek"}


def test_synthetic_specs_have_no_picture_to_send():
    with pytest.raises(ImageRoutingError, match="no picture"):
        check_image_allowed("synthetic", "openai")
