# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import atexit
import gc
import logging
import os
import subprocess
import sys
import threading
import time

_LOAD_LOOP = "import math, time\nwhile True:\n    math.log(time.time() * 1000)\n"


class CPULoad:
    """Busy-loops on THREAD_COUNT cores while adHighCpu is on.

    The JVM version ran 4 Java threads; Python threads share one GIL, so separate
    interpreter processes are used to load the same number of cores.
    """

    THREAD_COUNT = 4
    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self, spawn=None):
        self.logger = logging.getLogger("oteldemo.problempattern.CPULoad")
        self.running = False
        self._workers: list = []
        self._lock = threading.Lock()
        self._spawn = spawn or _spawn_worker
        atexit.register(self._stop_workers)

    @classmethod
    def get_instance(cls) -> "CPULoad":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def execute(self, enabled: bool) -> None:
        if enabled:
            self.logger.info("High CPU-Load problempattern enabled")
            if not self.running:
                self._spawn_workers(self.THREAD_COUNT)
                self.running = True
        else:
            self.running = False
            self._stop_workers()

    def _spawn_workers(self, count: int) -> None:
        with self._lock:
            for _ in range(count):
                self._workers.append(self._spawn())

    def _stop_workers(self) -> None:
        with self._lock:
            for worker in self._workers:
                worker.terminate()
            for worker in self._workers:
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.kill()
            self._workers.clear()


def _spawn_worker():
    return subprocess.Popen(
        [sys.executable, "-I", "-c", _LOAD_LOOP],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def rss_bytes() -> int:
    try:
        with open("/proc/self/statm") as statm:
            return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except OSError:
        return 0


class _Entry:
    __slots__ = ("ref", "payload")

    def __init__(self):
        self.ref = self
        self.payload = [None] * 8


class GarbageCollectionTrigger:
    """Fills memory with cyclic garbage and runs timed full collections.

    Mirrors the JVM version: an initial collection, then ten rounds of filling
    the heap up to 90% of the former -Xmx200m budget (or MAX_OBJECTS pending
    objects) followed by a timed full collection.
    """

    HEAP_LIMIT_BYTES = 200 * 1024 * 1024
    MAX_HEAP_USAGE = 0.9
    MAX_OBJECTS = 500000
    ROUNDS = 10
    _CHECK_EVERY = 10000

    def __init__(self, memory_usage=rss_bytes):
        self.logger = logging.getLogger("oteldemo.problempattern.GarbageCollectionTrigger")
        self.gc_delay_ms = 10_000
        self.last_gc = 0
        self._memory_usage = memory_usage

    def _heap_usage(self) -> float:
        return self._memory_usage() / self.HEAP_LIMIT_BYTES

    def do_execute(self) -> None:
        if _now_ms() - self.last_gc > self.gc_delay_ms:
            self.logger.info(
                f"Triggering a manual garbage collection, next one in {self.gc_delay_ms // 1000} seconds."
            )
            gc.collect()

            total = 0
            for _ in range(self.ROUNDS):
                entries = []
                while len(entries) < self.MAX_OBJECTS and (
                    len(entries) % self._CHECK_EVERY or self._heap_usage() < self.MAX_HEAP_USAGE
                ):
                    entries.append(_Entry())
                del entries
                start = _now_ms()
                gc.collect()
                total += _now_ms() - start
            self.logger.info(f"The artificially triggered GCs took: {total} ms")
            self.last_gc = _now_ms()


def _now_ms() -> int:
    return time.time_ns() // 1_000_000
