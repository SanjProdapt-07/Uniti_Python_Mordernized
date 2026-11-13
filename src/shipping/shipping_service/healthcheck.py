# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os
import socket
import sys

port = int(os.environ.get("SHIPPING_PORT", "50050"))
try:
    socket.create_connection(("127.0.0.1", port), timeout=5).close()
except OSError:
    sys.exit(1)
sys.exit(0)
