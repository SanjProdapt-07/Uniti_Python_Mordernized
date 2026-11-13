# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os

import uvicorn

from .app import create_app
from .flags import OpenFeatureFlags


def main() -> None:
    port = int(os.environ.get("EMAIL_PORT") or 4567)
    app = create_app(OpenFeatureFlags())
    uvicorn.run(app, host="0.0.0.0", port=port, server_header=False)


if __name__ == "__main__":
    main()
