# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import logging
import os
import sys

import uvicorn

from .app import create_app
from .flags import OpenFeatureFlags
from .logs import emit
from .quote import QuoteClient

TARGET = "shipping"


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)5s %(message)s")
    emit(TARGET, "INFO", "Successfully configured OTel")

    if "SHIPPING_PORT" not in os.environ:
        raise SystemExit("$SHIPPING_PORT is not set")
    try:
        port = int(os.environ["SHIPPING_PORT"])
        if not 0 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit("$SHIPPING_PORT is not a valid port") from None

    ip = "0.0.0.0"
    if os.environ.get("IPV6_ENABLED") == "true":
        ip = "[::]"
        emit(TARGET, "INFO", f"Overwriting Localhost IP:  {ip}")

    addr = f"{ip}:{port}"
    emit(TARGET, "INFO", "Shipping service is running", "shipping.server.started", addr=addr)

    app = create_app(OpenFeatureFlags(), QuoteClient())
    uvicorn.run(app, host=ip.strip("[]"), port=port, access_log=False, server_header=False, log_level="warning")


if __name__ == "__main__":
    main()
