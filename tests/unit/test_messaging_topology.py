"""Names and retry-queue selection of the RabbitMQ topology (no broker involved)."""

import pytest

from chart_analyzer.infrastructure.messaging.topology import Topology

TOPOLOGY = Topology(prefix="analysis", delays=(5, 30, 120, 30))


def test_names_derive_from_the_prefix() -> None:
    topology = Topology(prefix="t1", delays=(5,))

    assert topology.exchange == "t1"
    assert topology.tasks_queue == "t1.tasks"
    assert topology.retry_queue(5) == "t1.retry.5s"
    assert topology.dead_letter_exchange == "t1.dlx"
    assert topology.dead_letter_queue == "t1.dlq"


def test_repeated_delays_get_one_queue() -> None:
    assert TOPOLOGY.distinct_delays == (5, 30, 120)


@pytest.mark.parametrize(
    ("requested", "key"),
    [
        (5, "retry.5"),
        (30, "retry.30"),
        (120, "retry.120"),
        (0, "retry.5"),
        (6, "retry.30"),
        (31, "retry.120"),
        (121, "retry.120"),
        (3600, "retry.120"),
    ],
)
def test_retry_key_is_the_shortest_delay_that_is_long_enough(requested: int, key: str) -> None:
    assert TOPOLOGY.retry_routing_key(requested) == key


@pytest.mark.parametrize("delays", [(), (0,), (5, -1)])
def test_delays_must_be_positive(delays: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="delays"):
        Topology(prefix="analysis", delays=delays)
