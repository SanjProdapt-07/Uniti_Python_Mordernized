# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os

import uvicorn

from .app import create_app


def main() -> None:
    port = int(os.environ.get("PORT") or os.environ.get("FRONTEND_PORT") or "8080")
    uvicorn.run(create_app(), host="0.0.0.0", port=port, access_log=False)


if __name__ == "__main__":
    main()
