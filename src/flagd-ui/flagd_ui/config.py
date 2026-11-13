#!/usr/bin/python
# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import os

ROOT_PATH = "/feature"
DEFAULT_STORAGE_FILE_PATH = "/app/data/demo.flagd.json"


def port() -> int:
    return int(os.environ.get("FLAGD_UI_PORT") or "4000")


def storage_file_path() -> str:
    return os.environ.get("FLAGD_UI_STORAGE_FILE_PATH") or DEFAULT_STORAGE_FILE_PATH
