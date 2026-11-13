# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os
import signal
import sys
import threading

from fraud_detection import flags, log
from fraud_detection.consumer import FraudDetectionConsumer


def main() -> None:
    log.configure_console()
    flags.init_provider()

    bootstrap_servers = os.environ.get("KAFKA_ADDR")
    if bootstrap_servers is None:
        print("KAFKA_ADDR is not supplied", flush=True)
        sys.exit(1)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    signal.signal(signal.SIGINT, lambda signum, frame: stop.set())

    FraudDetectionConsumer(bootstrap_servers).run(stop)


if __name__ == "__main__":
    main()
