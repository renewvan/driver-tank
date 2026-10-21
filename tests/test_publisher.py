import pytest

import driver_tank.publisher as publisher_module
from driver_tank.config import load_config
from driver_tank.publisher import HEALTH_TOPIC, INFO_TOPIC, Publisher

INFO = '{"device": "tank", "version": "v1"}'


class FakeClient:
    """Records what the broker would receive, in order."""

    def __init__(self, *args, **kwargs) -> None:
        self.published: list[tuple[str, str, bool]] = []
        self.will: tuple[str, str, bool] | None = None

    def username_pw_set(self, *args) -> None: ...
    def reconnect_delay_set(self, **kwargs) -> None: ...
    def subscribe(self, *args, **kwargs) -> None: ...
    def loop_stop(self) -> None: ...
    def disconnect(self) -> None: ...

    def will_set(self, topic, payload, qos, retain) -> None:
        self.will = (topic, payload, retain)

    def publish(self, topic, payload, qos, retain) -> None:
        self.published.append((topic, payload, retain))


@pytest.fixture
def publisher(monkeypatch) -> Publisher:
    monkeypatch.setattr(publisher_module.mqtt, "Client", FakeClient)
    return Publisher(load_config().mqtt, info=INFO)


def connect(publisher: Publisher) -> FakeClient:
    client = publisher._client
    publisher._on_connect(client, None, {}, 0)
    return client


def test_info_is_published_retained_immediately_before_online(publisher: Publisher) -> None:
    client = connect(publisher)
    assert client.published == [(INFO_TOPIC, INFO, True), (HEALTH_TOPIC, "online", True)]


def test_info_is_published_again_on_every_reconnect(publisher: Publisher) -> None:
    client = connect(publisher)
    client.published.clear()
    connect(publisher)
    assert client.published == [(INFO_TOPIC, INFO, True), (HEALTH_TOPIC, "online", True)]


def test_failed_connect_publishes_nothing(publisher: Publisher) -> None:
    client = publisher._client
    publisher._on_connect(client, None, {}, 5)
    assert client.published == []


def test_info_is_never_cleared_when_the_device_goes_offline(publisher: Publisher) -> None:
    client = connect(publisher)
    client.published.clear()
    publisher.disconnect()
    assert client.published == [(HEALTH_TOPIC, "offline", True)]
    # the broker publishes the last will on an unclean death: offline on health only
    assert client.will == (HEALTH_TOPIC, "offline", True)
