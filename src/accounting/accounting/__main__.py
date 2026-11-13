# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os
import signal
import threading

from accounting import helpers, log
from accounting.consumer import Consumer


def main() -> None:
    print("Accounting service started", flush=True)

    helpers.output_in_order(helpers.filter_relevant(os.environ))

    log.configure_console()

    stop = threading.Event()

    def request_stop(signum, frame):
        if not stop.is_set():
            log.application_stopping()
            stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    consumer = Consumer()
    log.application_started(os.getcwd())
    consumer.run(stop)


if __name__ == "__main__":
    main()
