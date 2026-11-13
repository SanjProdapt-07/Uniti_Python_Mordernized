# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import errno
import itertools
import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import formatdate, parsedate_to_datetime
from urllib.parse import quote, unquote_to_bytes

from .pages import content_type_for, error_page

READ_CHUNK = 256 * 1024
FILE_METHODS = {"GET", "HEAD", "POST"}
GZIP_METHODS = {"GET", "HEAD"}

_boundaries = itertools.count(1)


@dataclass
class Reply:
    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: Callable[[], Iterator[bytes]] = lambda: iter(())
    length: int = 0


class Unsatisfiable(Exception):
    pass


def error_reply(status: int, headers: list[tuple[str, str]] | None = None) -> Reply:
    page = error_page(status)
    extra = list(headers or [])
    if status == 400:
        extra.append(("connection", "close"))
    return Reply(
        status,
        [("content-type", "text/html"), ("content-length", str(len(page))), *extra],
        lambda: iter((page,)),
        len(page),
    )


def normalize_uri(raw_path: bytes) -> bytes | None:
    decoded = unquote_to_bytes(raw_path)
    if not decoded.startswith(b"/") or b"\x00" in decoded:
        return None
    segments = decoded.split(b"/")[1:]
    out: list[bytes] = []
    for segment in segments:
        if segment == b"..":
            if not out:
                return None
            out.pop()
        elif segment not in (b"", b"."):
            out.append(segment)
    if not out:
        return b"/"
    trailing = segments[-1] in (b"", b".", b"..")
    return b"/" + b"/".join(out) + (b"/" if trailing else b"")


def parse_http_date(value: str) -> int | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def etag_matches(header: str, etag: str, weak: bool) -> bool:
    if header.strip() == "*":
        return True
    for candidate in header.split(","):
        candidate = candidate.strip()
        if weak and candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


def _digit(value: str, index: int) -> bool:
    return index < len(value) and "0" <= value[index] <= "9"


def _char(value: str, index: int) -> str:
    return value[index] if index < len(value) else ""


def parse_ranges(value: str, size: int) -> list[tuple[int, int]] | None:
    spec = value[6:]
    i = 0
    ranges: list[tuple[int, int]] = []
    total = 0
    while True:
        start = end = 0
        suffix = False
        open_ended = False
        while _char(spec, i) == " ":
            i += 1
        if _char(spec, i) != "-":
            if not _digit(spec, i):
                raise Unsatisfiable
            while _digit(spec, i):
                start = start * 10 + int(spec[i])
                i += 1
            while _char(spec, i) == " ":
                i += 1
            if _char(spec, i) != "-":
                raise Unsatisfiable
            i += 1
            while _char(spec, i) == " ":
                i += 1
            if _char(spec, i) in (",", ""):
                end = size
                open_ended = True
        else:
            suffix = True
            i += 1
        if not open_ended:
            if not _digit(spec, i):
                raise Unsatisfiable
            while _digit(spec, i):
                end = end * 10 + int(spec[i])
                i += 1
            while _char(spec, i) == " ":
                i += 1
            if _char(spec, i) not in (",", ""):
                raise Unsatisfiable
            if suffix:
                start = size - end if end < size else 0
                end = size - 1
            end = size if end >= size else end + 1
        if start < end:
            ranges.append((start, end))
            total += end - start
        elif start == 0:
            return None
        if _char(spec, i) != ",":
            break
        i += 1
    if not ranges:
        raise Unsatisfiable
    if total > size:
        return None
    return ranges


def _read(path: bytes, segments: list[tuple[bytes, int, int]], trailer: bytes = b"") -> Iterator[bytes]:
    with open(path, "rb") as handle:
        for prefix, start, end in segments:
            if prefix:
                yield prefix
            handle.seek(start)
            remaining = end - start
            while remaining > 0:
                chunk = handle.read(min(READ_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
    if trailer:
        yield trailer


def _host_name(host_header: str | None) -> str:
    if not host_header:
        return "_"
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1] if "]" in host else host
    return host.split(":", 1)[0]


class StaticFiles:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = os.fsencode(os.path.abspath(root))

    def handle(self, method: str, uri: bytes, query: bytes, headers: dict[str, str], local_port: int | None) -> Reply:
        if method not in FILE_METHODS:
            return error_reply(405)
        if uri.endswith(b"/"):
            return self._directory(method, uri, headers)
        if method in GZIP_METHODS and self._gzip_ok(headers):
            gzipped = self._regular_file(self.root + uri + b".gz")
            if gzipped is not None:
                return self._serve(method, gzipped[0], gzipped[1], content_type_for(uri), headers, gzip=True)
        path = self.root + uri
        try:
            info = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return error_reply(404)
        except PermissionError:
            return error_reply(403)
        except OSError as exc:
            return error_reply(404 if exc.errno == errno.ENAMETOOLONG else 500)
        if stat.S_ISDIR(info.st_mode):
            return self._redirect(uri, query, headers, local_port)
        if not stat.S_ISREG(info.st_mode):
            return error_reply(404)
        try:
            os.close(os.open(path, os.O_RDONLY))
        except PermissionError:
            return error_reply(403)
        if method == "POST":
            return error_reply(405)
        return self._serve(method, path, info, content_type_for(uri), headers)

    def _directory(self, method: str, uri: bytes, headers: dict[str, str]) -> Reply:
        index = self._regular_file(self.root + uri + b"index.html")
        if index is not None:
            if method == "POST":
                return error_reply(405)
            return self._serve(method, index[0], index[1], "text/html", headers)
        try:
            info = os.stat(self.root + uri)
        except (FileNotFoundError, NotADirectoryError):
            return error_reply(404)
        except PermissionError:
            return error_reply(403)
        if not stat.S_ISDIR(info.st_mode):
            return error_reply(404)
        return error_reply(403)

    @staticmethod
    def _regular_file(path: bytes) -> tuple[bytes, os.stat_result] | None:
        try:
            info = os.stat(path)
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode) or not os.access(path, os.R_OK):
            return None
        return path, info

    @staticmethod
    def _gzip_ok(headers: dict[str, str]) -> bool:
        if "via" in headers:
            return False
        for coding in headers.get("accept-encoding", "").split(","):
            name, _, params = coding.strip().partition(";")
            if name.strip().lower() != "gzip":
                continue
            q = params.strip().lower()
            if q.startswith("q="):
                try:
                    return float(q[2:]) > 0
                except ValueError:
                    return False
            return True
        return False

    @staticmethod
    def _redirect(uri: bytes, query: bytes, headers: dict[str, str], local_port: int | None) -> Reply:
        port = f":{local_port}" if local_port and local_port != 80 else ""
        target = quote(uri + b"/", safe="/:@!$&'()*+,;=-._~")
        if query:
            target += "?" + query.decode("latin-1")
        location = f"http://{_host_name(headers.get('host'))}{port}{target}"
        return error_reply(301, [("location", location)])

    def _serve(
        self,
        method: str,
        path: bytes,
        info: os.stat_result,
        content_type: str,
        headers: dict[str, str],
        gzip: bool = False,
    ) -> Reply:
        mtime = int(info.st_mtime)
        size = info.st_size
        etag = f'"{mtime:x}-{size:x}"'
        validators = [("last-modified", formatdate(mtime, usegmt=True)), ("etag", etag)]
        encoding = [("content-encoding", "gzip")] if gzip else []

        precondition = self._preconditions(headers, mtime, etag)
        if precondition == 412:
            return error_reply(412)
        if precondition == 304:
            return Reply(304, validators)

        ranges = None
        if self._range_applies(headers, mtime, etag):
            try:
                ranges = parse_ranges(headers["range"], size)
            except Unsatisfiable:
                return error_reply(416, [("content-range", f"bytes */{size}")])

        if ranges is None:
            return Reply(
                200,
                [
                    ("content-type", content_type),
                    ("content-length", str(size)),
                    *validators,
                    *encoding,
                    ("accept-ranges", "bytes"),
                ],
                lambda: _read(path, [(b"", 0, size)]),
                size,
            )

        if len(ranges) == 1:
            start, end = ranges[0]
            return Reply(
                206,
                [
                    ("content-type", content_type),
                    ("content-length", str(end - start)),
                    *validators,
                    *encoding,
                    ("content-range", f"bytes {start}-{end - 1}/{size}"),
                ],
                lambda: _read(path, [(b"", start, end)]),
                end - start,
            )

        boundary = f"{next(_boundaries):010d}"
        segments = [
            (
                f"\r\n--{boundary}\r\nContent-Type: {content_type}\r\n"
                f"Content-Range: bytes {start}-{end - 1}/{size}\r\n\r\n".encode(),
                start,
                end,
            )
            for start, end in ranges
        ]
        trailer = f"\r\n--{boundary}--\r\n".encode()
        length = sum(len(prefix) + end - start for prefix, start, end in segments) + len(trailer)
        return Reply(
            206,
            [
                ("content-type", f"multipart/byteranges; boundary={boundary}"),
                ("content-length", str(length)),
                *validators,
                *encoding,
            ],
            lambda: _read(path, segments, trailer),
            length,
        )

    @staticmethod
    def _preconditions(headers: dict[str, str], mtime: int, etag: str) -> int | None:
        if "if-unmodified-since" in headers:
            since = parse_http_date(headers["if-unmodified-since"])
            if since is None or since < mtime:
                return 412
        if "if-match" in headers and not etag_matches(headers["if-match"], etag, weak=False):
            return 412
        ims = headers.get("if-modified-since")
        inm = headers.get("if-none-match")
        if ims is None and inm is None:
            return None
        if ims is not None and parse_http_date(ims) != mtime:
            return None
        if inm is not None and not etag_matches(inm, etag, weak=True):
            return None
        return 304

    @staticmethod
    def _range_applies(headers: dict[str, str], mtime: int, etag: str) -> bool:
        value = headers.get("range")
        if value is None or len(value) < 7 or value[:6].lower() != "bytes=":
            return False
        if_range = headers.get("if-range")
        if if_range is None:
            return True
        if len(if_range) >= 2 and if_range.endswith('"'):
            return if_range == etag
        return parse_http_date(if_range) == mtime
