# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
from openfeature import api
from openfeature.contrib.hook.opentelemetry import TracingHook
from openfeature.flag_evaluation import Reason
from openfeature.hook import Hook
from opentelemetry import metrics

KEY = "feature_flag.key"
VARIANT = "feature_flag.result.variant"
REASON = "feature_flag.result.reason"
PROVIDER = "feature_flag.provider.name"


class MetricsHook(Hook):
    def __init__(self, meter_provider=None):
        meter = metrics.get_meter("OpenFeature", meter_provider=meter_provider)
        self._active = meter.create_up_down_counter(
            "feature_flag.evaluation_active_count", unit="{request}",
            description="active flag evaluations counter")
        self._requests = meter.create_counter(
            "feature_flag.evaluation_requests_total", unit="{request}",
            description="feature flag evaluation request counter")
        self._success = meter.create_counter(
            "feature_flag.evaluation_success_total", unit="{impression}",
            description="feature flag evaluation success counter")
        self._errors = meter.create_counter(
            "feature_flag.evaluation_error_total", unit="{request}",
            description="feature flag evaluation error counter")

    def before(self, hook_context, hints):
        attributes = {KEY: hook_context.flag_key}
        self._active.add(1, attributes)
        self._requests.add(1, attributes)
        return None

    def after(self, hook_context, details, hints):
        attributes = {
            KEY: hook_context.flag_key,
            VARIANT: details.variant or str(details.value),
            REASON: str(details.reason or Reason.UNKNOWN).lower(),
        }
        if hook_context.provider_metadata:
            attributes[PROVIDER] = hook_context.provider_metadata.name
        self._success.add(1, attributes)

    def error(self, hook_context, exception, hints):
        self._errors.add(1, {KEY: hook_context.flag_key, "exception": str(exception)})

    def finally_after(self, hook_context, details, hints):
        self._active.add(-1, {KEY: hook_context.flag_key})


def setup_feature_flags():
    from openfeature.contrib.provider.flagd import FlagdProvider

    api.set_provider(FlagdProvider())
    api.add_hooks([MetricsHook(), TracingHook()])
    return api.get_client()
