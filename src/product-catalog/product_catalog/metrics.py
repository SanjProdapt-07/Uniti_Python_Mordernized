# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
from opentelemetry import metrics
from opentelemetry.metrics import Observation

POOL_NAME_ATTRIBUTE = "db.client.connection.pool.name"
STATE_ATTRIBUTE = "db.client.connection.state"


def register_pool_metrics(pool, meter_provider=None) -> None:
    meter = metrics.get_meter("github.com/XSAM/otelsql", meter_provider=meter_provider)
    name = getattr(pool, "name", "product-catalog")
    base = {"db.system.name": "postgresql", POOL_NAME_ATTRIBUTE: name}

    def connection_count(_options):
        stats = pool.get_stats()
        size = stats.get("pool_size", 0)
        idle = stats.get("pool_available", 0)
        return [
            Observation(idle, {**base, STATE_ATTRIBUTE: "idle"}),
            Observation(max(size - idle, 0), {**base, STATE_ATTRIBUTE: "used"}),
        ]

    def connection_max(_options):
        return [Observation(pool.get_stats().get("pool_max", pool.max_size), base)]

    def pending_requests(_options):
        return [Observation(pool.get_stats().get("requests_waiting", 0), base)]

    meter.create_observable_up_down_counter(
        "db.client.connection.count", callbacks=[connection_count], unit="{connection}",
        description="The number of connections that are currently in state described by the state attribute.")
    meter.create_observable_up_down_counter(
        "db.client.connection.max", callbacks=[connection_max], unit="{connection}",
        description="The maximum number of open connections allowed.")
    meter.create_observable_up_down_counter(
        "db.client.connection.pending_requests", callbacks=[pending_requests], unit="{request}",
        description="The number of current pending requests for an open connection.")
