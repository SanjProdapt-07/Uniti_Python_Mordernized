# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os
import signal
import sys
import threading

from openfeature import api
from openfeature.contrib.provider.flagd import FlagdProvider

from payment.logger import get_logger, serialize_error
from payment.server import PaymentService, build_server


def main() -> int:
    logger = get_logger()
    api.set_provider(FlagdProvider())

    ip = "0.0.0.0"
    if os.environ.get("IPV6_ENABLED") == "true":
        ip = "[::]"
        logger.info(f"Overwriting Localhost IP: {ip}")

    address = ip + f":{os.environ.get('PAYMENT_PORT', 'undefined')}"

    try:
        server, _ = build_server(PaymentService(), address)
        server.start()
    except Exception as err:
        logger.error("", extra={"err": serialize_error(err)})
        return 1
    logger.info(f"payment gRPC server started on {address}")

    stop = threading.Event()
    received = {}

    def close_gracefully(signum, _frame):
        received["signum"] = signum
        stop.set()

    signal.signal(signal.SIGINT, close_gracefully)
    signal.signal(signal.SIGTERM, close_gracefully)
    stop.wait()
    server.stop(None)
    api.shutdown()
    return 128 + received["signum"]


if __name__ == "__main__":
    sys.exit(main())
