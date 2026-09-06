from types import SimpleNamespace

from utils.docker_images import get_image_tags


def test_get_image_tags_without_filter_returns_serializable_tags():
    result = get_image_tags(
        [
            SimpleNamespace(tags=["hummingbot/hummingbot:latest", "hummingbot/hummingbot:dev"]),
            SimpleNamespace(tags=[]),
            SimpleNamespace(tags=["hummingbot/gateway:latest"]),
        ]
    )

    assert result == [
        "hummingbot/hummingbot:latest",
        "hummingbot/hummingbot:dev",
        "hummingbot/gateway:latest",
    ]


def test_get_image_tags_with_filter_returns_matching_tags():
    result = get_image_tags(
        [
            SimpleNamespace(tags=["hummingbot/hummingbot:latest", "hummingbot/hummingbot:dev"]),
            SimpleNamespace(tags=[]),
            SimpleNamespace(tags=["hummingbot/gateway:latest"]),
        ],
        image_name="gateway",
    )

    assert result == ["hummingbot/gateway:latest"]
