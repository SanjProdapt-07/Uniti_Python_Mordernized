# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import logging
import random
import urllib.request

import grpc
import prometheus_client
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from openfeature import api
from openfeature.flag_evaluation import FlagResolutionDetails
from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider
from opentelemetry import baggage, context, trace
from opentelemetry.trace import StatusCode

import demo_pb2
import demo_pb2_grpc
from ad import problempattern
from ad.__main__ import build_server, configure_logging
from ad.service import PROMETHEUS_REGISTRY, AdService


class FakeCPULoad:
    def __init__(self):
        self.calls = []

    def execute(self, enabled):
        self.calls.append(enabled)


class FakeGC:
    runs = 0

    def do_execute(self):
        FakeGC.runs += 1


class AbortContext:
    def abort(self, code, details):
        self.code, self.details = code, details
        raise grpc.RpcError(details)


SEEN_CONTEXTS = []


def _flag(value):
    def evaluator(flag, ctx):
        SEEN_CONTEXTS.append(ctx)
        return FlagResolutionDetails(value=value)

    return InMemoryFlag("v", {"v": value}, context_evaluator=evaluator)


@pytest.fixture
def flags():
    def make(ad_failure=False, ad_manual_gc=False, ad_high_cpu=False):
        SEEN_CONTEXTS.clear()
        api.set_provider(
            InMemoryProvider(
                {
                    "adFailure": _flag(ad_failure),
                    "adManualGc": _flag(ad_manual_gc),
                    "adHighCpu": _flag(ad_high_cpu),
                }
            )
        )
        return api.get_client()

    yield make
    api.clear_providers()


def served(category):
    return PROMETHEUS_REGISTRY.get_sample_value("demo_ad_served_total", {"category": category}) or 0


def call(service, keys=(), bag=None, ctx=None):
    current = context.get_current()
    for k, v in (bag or {}).items():
        current = baggage.set_baggage(k, v, current)
    token = context.attach(current)
    try:
        with trace.get_tracer("test").start_as_current_span("server"):
            return service.GetAds(demo_pb2.AdRequest(context_keys=list(keys)), ctx or AbortContext())
    finally:
        context.detach(token)


def spans_by_name(spans, name):
    return [s for s in spans.get_finished_spans() if s.name == name]


def ad_request_points(reader):
    points = {}
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "demo.ad.requests":
                    assert sm.scope.name == "ad"
                    assert m.description == "Counts ad requests by request and response type"
                    for p in m.data.data_points:
                        key = (p.attributes["demo.ad.request_type"], p.attributes["demo.ad.response_type"])
                        points[key] = p.value
    return points


def test_targeted_request(telemetry, flags, caplog):
    spans, reader, _ = telemetry
    cpu = FakeCPULoad()
    service = AdService(flags(), cpu_load=cpu)
    before = served("accessories")
    caplog.set_level(logging.INFO)
    response = call(service, ["accessories", "binoculars"], {"session.id": "s-1", "enduser.id": "u-1"})
    assert [a.redirect_url for a in response.ads] == [
        "/product/0PUK6V6EV0",
        "/product/6E92ZMYYFZ",
        "/product/L9ECAV7KIM",
        "/product/2ZYFJ3GM2N",
    ]
    (server,) = spans_by_name(spans, "server")
    assert server.attributes["demo.ad.context_keys"] == "[accessories, binoculars]"
    assert server.attributes["demo.ad.context_keys.count"] == 2
    assert server.attributes["demo.ad.count"] == 4
    assert server.attributes["demo.ad.request_type"] == "TARGETED"
    assert server.attributes["demo.ad.response_type"] == "TARGETED"
    assert server.attributes["session.id"] == "s-1"
    assert server.attributes["enduser.id"] == "u-1"
    by_category = spans_by_name(spans, "getAdsByCategory")
    assert [(s.attributes["demo.ad.category"], s.attributes["demo.ad.count"]) for s in by_category] == [
        ("accessories", 3),
        ("binoculars", 1),
    ]
    assert all(s.parent.span_id == server.context.span_id for s in by_category)
    assert served("accessories") - before == 3
    assert cpu.calls == [False]
    assert all(c.targeting_key == "s-1" and c.attributes == {"session": "s-1"} for c in SEEN_CONTEXTS)
    assert "Targeted ad request received for [accessories, binoculars]" in caplog.messages
    assert ad_request_points(reader).get(("TARGETED", "TARGETED"), 0) >= 1


def test_unknown_category_falls_back_to_random(telemetry, flags):
    spans, reader, _ = telemetry
    before = served("books"), served("random")
    response = call(AdService(flags(), cpu_load=FakeCPULoad()), ["books"], {"session.id": "s"})
    assert len(response.ads) == 2
    (server,) = spans_by_name(spans, "server")
    assert server.attributes["demo.ad.request_type"] == "TARGETED"
    assert server.attributes["demo.ad.response_type"] == "RANDOM"
    (random_span,) = spans_by_name(spans, "getRandomAds")
    assert random_span.attributes["demo.ad.count"] == 2
    assert PROMETHEUS_REGISTRY.get_sample_value("demo_ad_served_total", {"category": "books"}) == before[0] == 0
    assert served("random") - before[1] == 2
    assert ad_request_points(reader).get(("TARGETED", "RANDOM"), 0) >= 1


def test_non_targeted_without_baggage(telemetry, flags, caplog):
    spans, reader, _ = telemetry
    caplog.set_level(logging.INFO)
    service = AdService(flags(), cpu_load=FakeCPULoad(), rng=random.Random(1))
    response = call(service)
    assert len(response.ads) == 2
    assert all(ad in service.all_ads for ad in response.ads)
    (server,) = spans_by_name(spans, "server")
    assert server.attributes["demo.ad.context_keys"] == "[]"
    assert server.attributes["demo.ad.request_type"] == "NOT_TARGETED"
    assert server.attributes["demo.ad.response_type"] == "RANDOM"
    assert "session.id" not in server.attributes
    assert "no baggage found in context" in caplog.messages
    assert "Non-targeted ad request received, preparing random response." in caplog.messages
    assert all(c.targeting_key is None for c in SEEN_CONTEXTS)
    assert ad_request_points(reader).get(("NOT_TARGETED", "RANDOM"), 0) >= 1


def test_ad_failure_flag(telemetry, flags, caplog):
    spans, _, _ = telemetry

    class AlwaysZero(random.Random):
        def randrange(self, n):
            return 0

    service = AdService(flags(ad_failure=True), cpu_load=FakeCPULoad(), rng=AlwaysZero())
    ctx = AbortContext()
    with pytest.raises(grpc.RpcError):
        call(service, ["travel"], ctx=ctx)
    assert ctx.code == grpc.StatusCode.UNAVAILABLE
    (server,) = spans_by_name(spans, "server")
    assert server.status.status_code == StatusCode.ERROR
    event = server.events[0]
    assert event.name == "Error"
    assert event.attributes["exception.message"] == "UNAVAILABLE"
    assert "GetAds Failed with status Status{code=UNAVAILABLE, description=null, cause=null}" in caplog.messages


def test_ad_failure_flag_only_fails_one_in_ten(flags):
    class NotZero(random.Random):
        def randrange(self, n):
            return 3

    response = call(AdService(flags(ad_failure=True), cpu_load=FakeCPULoad(), rng=NotZero()), ["travel"])
    assert len(response.ads) == 1


def test_manual_gc_flag(flags, caplog):
    FakeGC.runs = 0
    service = AdService(flags(ad_manual_gc=True), cpu_load=FakeCPULoad(), gc_trigger_factory=FakeGC)
    call(service, ["travel"])
    assert FakeGC.runs == 1
    assert "Feature Flag adManualGc enabled, performing a manual gc now" in caplog.messages


def test_high_cpu_flag_passes_flag_value(flags):
    cpu = FakeCPULoad()
    call(AdService(flags(ad_high_cpu=True), cpu_load=cpu), ["travel"])
    assert cpu.calls == [True]


def test_cpu_load_starts_and_stops_workers(caplog):
    caplog.set_level(logging.INFO)
    spawned = []

    class Worker:
        terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            return 0

    load = problempattern.CPULoad(spawn=lambda: spawned.append(Worker()) or spawned[-1])
    load.execute(True)
    load.execute(True)
    assert len(spawned) == 4
    assert caplog.messages.count("High CPU-Load problempattern enabled") == 2
    load.execute(False)
    assert all(w.terminated for w in spawned)
    assert load.running is False


def test_cpu_load_real_worker_process_burns_cpu():
    worker = problempattern._spawn_worker()
    try:
        assert worker.poll() is None
    finally:
        worker.terminate()
        worker.wait(timeout=5)


def test_gc_trigger_runs_timed_collections(caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(problempattern.GarbageCollectionTrigger, "MAX_OBJECTS", 20000)
    trigger = problempattern.GarbageCollectionTrigger()
    trigger.do_execute()
    assert caplog.messages[0] == "Triggering a manual garbage collection, next one in 10 seconds."
    assert caplog.messages[1].startswith("The artificially triggered GCs took: ")
    assert caplog.messages[1].endswith(" ms")
    assert trigger.last_gc > 0
    trigger.do_execute()
    assert len(caplog.messages) == 2


def test_gc_trigger_respects_heap_budget():
    trigger = problempattern.GarbageCollectionTrigger(memory_usage=lambda: 10**12)
    trigger.do_execute()


def test_log_format_matches_log4j_pattern():
    configure_logging()
    from ad.__main__ import Log4jStyleFormatter

    record = logging.LogRecord("oteldemo.AdService", logging.INFO, __file__, 1, "Ad service starting.", None, None)
    line = Log4jStyleFormatter().format(record)
    assert line.endswith(" - oteldemo.AdService - Ad service starting. trace_id= span_id= trace_flags= ")


def test_over_the_wire(telemetry, flags):
    from opentelemetry.instrumentation.grpc import GrpcInstrumentorServer

    spans, _, _ = telemetry
    instrumentor = GrpcInstrumentorServer()
    instrumentor.instrument()
    try:
        server, health_servicer, port = build_server(AdService(flags(), cpu_load=FakeCPULoad()), 0)
    finally:
        instrumentor.uninstrument()
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    server.start()
    prometheus_client.disable_created_metrics()
    prom, _ = prometheus_client.start_http_server(0, addr="127.0.0.1", registry=PROMETHEUS_REGISTRY)
    try:
        with grpc.insecure_channel(f"localhost:{port}") as channel:
            stub = demo_pb2_grpc.AdServiceStub(channel)
            response = stub.GetAds(
                demo_pb2.AdRequest(context_keys=["telescopes"]), metadata=(("baggage", "session.id=abc"),)
            )
            assert [a.text for a in response.ads] == ["Starsense Explorer Refractor Telescope for sale. 20% off."]
            health = health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest())
            assert health.status == health_pb2.HealthCheckResponse.SERVING
        body = urllib.request.urlopen(f"http://127.0.0.1:{prom.server_port}/metrics").read().decode()
        assert 'demo_ad_served_total{category="telescopes"}' in body
        assert "_created" not in body
        assert "python_gc" not in body
    finally:
        server.stop(None)
        prom.shutdown()
    server_span = next(s for s in spans.get_finished_spans() if s.kind == trace.SpanKind.SERVER)
    assert server_span.attributes["session.id"] == "abc"
    assert server_span.attributes["demo.ad.response_type"] == "TARGETED"
