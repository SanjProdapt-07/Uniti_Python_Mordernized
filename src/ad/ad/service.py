# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import logging
import random
from enum import Enum

import grpc
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from opentelemetry import baggage, metrics, trace
from opentelemetry.trace import StatusCode
from prometheus_client import CollectorRegistry, Counter

import demo_pb2
import demo_pb2_grpc
from ad.catalog import create_ads_map
from ad.problempattern import CPULoad, GarbageCollectionTrigger

MAX_ADS_TO_SERVE = 2
AD_FAILURE = "adFailure"
AD_MANUAL_GC_FEATURE_FLAG = "adManualGc"
AD_HIGH_CPU_FEATURE_FLAG = "adHighCpu"

logger = logging.getLogger("oteldemo.AdService")
tracer = trace.get_tracer("ad")
meter = metrics.get_meter("ad")
ad_requests_counter = meter.create_counter(
    "demo.ad.requests", description="Counts ad requests by request and response type"
)

PROMETHEUS_REGISTRY = CollectorRegistry(auto_describe=True)
ads_served_counter = Counter(
    "demo_ad_served",
    "Total number of ads served, labeled by category",
    ["category"],
    registry=PROMETHEUS_REGISTRY,
)


class AdRequestType(Enum):
    TARGETED = "TARGETED"
    NOT_TARGETED = "NOT_TARGETED"


class AdResponseType(Enum):
    TARGETED = "TARGETED"
    RANDOM = "RANDOM"


class AdUnavailable(Exception):
    def __str__(self):
        return "UNAVAILABLE"


def java_list_string(items) -> str:
    return "[" + ", ".join(items) + "]"


class AdService(demo_pb2_grpc.AdServiceServicer):
    def __init__(self, flag_client=None, cpu_load=None, gc_trigger_factory=GarbageCollectionTrigger, rng=None):
        self._flag_client = flag_client
        self.cpu_load = cpu_load
        self.gc_trigger_factory = gc_trigger_factory
        self.random = rng or random.Random()
        self.ads_map = create_ads_map()
        self.all_ads = [ad for ads in self.ads_map.values() for ad in ads]

    @property
    def flag_client(self):
        return self._flag_client or api.get_client()

    def GetAds(self, request, context):
        span = trace.get_current_span()
        flags = self.flag_client
        try:
            all_ads = []
            entries = baggage.get_all()
            targeting_key = None
            attributes = {}
            if entries:
                session_id = entries.get("session.id")
                if session_id is not None:
                    span.set_attribute("session.id", session_id)
                    targeting_key = session_id
                    attributes["session"] = session_id
                enduser_id = entries.get("enduser.id")
                if enduser_id is not None:
                    span.set_attribute("enduser.id", enduser_id)
            else:
                logger.info("no baggage found in context")
            evaluation_context = EvaluationContext(targeting_key=targeting_key, attributes=attributes)

            cpu_load = self.cpu_load or CPULoad.get_instance()
            cpu_load.execute(flags.get_boolean_value(AD_HIGH_CPU_FEATURE_FLAG, False, evaluation_context))

            context_keys = list(request.context_keys)
            span.set_attribute("demo.ad.context_keys", java_list_string(context_keys))
            span.set_attribute("demo.ad.context_keys.count", len(context_keys))
            if context_keys:
                logger.info("Targeted ad request received for " + java_list_string(context_keys))
                for key in context_keys:
                    all_ads.extend(self.get_ads_by_category(key))
                request_type = AdRequestType.TARGETED
                response_type = AdResponseType.TARGETED
            else:
                logger.info("Non-targeted ad request received, preparing random response.")
                all_ads = self.get_random_ads()
                request_type = AdRequestType.NOT_TARGETED
                response_type = AdResponseType.RANDOM
            if not all_ads:
                all_ads = self.get_random_ads()
                response_type = AdResponseType.RANDOM
            span.set_attribute("demo.ad.count", len(all_ads))
            span.set_attribute("demo.ad.request_type", request_type.value)
            span.set_attribute("demo.ad.response_type", response_type.value)

            ad_requests_counter.add(
                1,
                {"demo.ad.request_type": request_type.value, "demo.ad.response_type": response_type.value},
            )

            if flags.get_boolean_value(AD_FAILURE, False, evaluation_context) and self.random.randrange(10) == 0:
                raise AdUnavailable()

            if flags.get_boolean_value(AD_MANUAL_GC_FEATURE_FLAG, False, evaluation_context):
                logger.warning(
                    "Feature Flag " + AD_MANUAL_GC_FEATURE_FLAG + " enabled, performing a manual gc now"
                )
                self.gc_trigger_factory().do_execute()

            return demo_pb2.AdResponse(ads=all_ads)
        except AdUnavailable as err:
            span.add_event("Error", {"exception.message": str(err)})
            span.set_status(StatusCode.ERROR)
            logger.warning("GetAds Failed with status Status{code=UNAVAILABLE, description=null, cause=null}")
        context.abort(grpc.StatusCode.UNAVAILABLE, "")

    def get_ads_by_category(self, category: str):
        with tracer.start_as_current_span("getAdsByCategory", attributes={"demo.ad.category": category}) as span:
            ads = self.ads_map.get(category, ())
            span.set_attribute("demo.ad.count", len(ads))
            ads_served_counter.labels(category).inc(len(ads))
            return list(ads)

    def get_random_ads(self):
        with tracer.start_as_current_span("getRandomAds") as span:
            ads = [self.all_ads[self.random.randrange(len(self.all_ads))] for _ in range(MAX_ADS_TO_SERVE)]
            span.set_attribute("demo.ad.count", len(ads))
            ads_served_counter.labels("random").inc(len(ads))
            return ads
