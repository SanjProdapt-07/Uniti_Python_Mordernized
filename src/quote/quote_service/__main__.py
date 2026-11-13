# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os

import uvicorn

from .app import create_app


def main() -> None:
    ip = "0.0.0.0"
    if os.environ.get("IPV6_ENABLED") == "true":
        ip = "[::]"
        print(f"Overwriting Localhost IP: {ip}", flush=True)
    port = os.environ.get("QUOTE_PORT", "")
    address = f"{ip}:{port}"
    app = create_app()
    print(f"Listening on: {address}", flush=True)
    uvicorn.run(
        app,
        host=ip.strip("[]"),
        port=int(port),
        access_log=False,
        server_header=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
