# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Typed accessors for the flags declared in flags.json."""

from openfeature import api

EMIT_RAW_PII = "emitRawPii"
KAFKA_QUEUE_PROBLEMS = "kafkaQueueProblems"
PAYMENT_UNREACHABLE = "paymentUnreachable"


def emit_raw_pii() -> bool:
    return api.get_client().get_boolean_value(EMIT_RAW_PII, False)


def kafka_queue_problems() -> int:
    return api.get_client().get_integer_value(KAFKA_QUEUE_PROBLEMS, 0)


def payment_unreachable() -> bool:
    return api.get_client().get_boolean_value(PAYMENT_UNREACHABLE, False)
