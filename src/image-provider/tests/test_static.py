# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

import gzip
import os
from email.utils import formatdate

import pytest

from conftest import STATIC, call
from image_provider.pages import error_page

NOT_FOUND = (
    b"<html>\r\n<head><title>404 Not Found</title></head>\r\n<body>\r\n"
    b"<center><h1>404 Not Found</h1></center>\r\n<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n"
)


def _etag(path):
    info = os.stat(path)
    return f'"{int(info.st_mtime):x}-{info.st_size:x}"'


def _all_files():
    return sorted(p.relative_to(STATIC).as_posix() for p in STATIC.rglob("*") if p.is_file())


@pytest.mark.parametrize("name", _all_files())
def test_every_static_file_is_served(app, name):
    path = STATIC / name
    response = call(app, "GET", f"/{name}".encode())
    assert response.status == 200
    assert response.body == path.read_bytes()
    expected_type = "image/png" if name.endswith(".png") else "image/jpeg"
    assert response.headers["content-type"] == expected_type
    assert response.headers["content-length"] == str(path.stat().st_size)
    assert response.headers["etag"] == _etag(path)
    assert response.headers["last-modified"] == formatdate(int(path.stat().st_mtime), usegmt=True)
    assert response.headers["accept-ranges"] == "bytes"
    assert "cache-control" not in response.headers
    assert "expires" not in response.headers


def test_head_returns_headers_without_body(app):
    response = call(app, "HEAD", b"/Banner.png")
    assert response.status == 200
    assert response.body == b""
    assert response.headers["content-length"] == str((STATIC / "Banner.png").stat().st_size)


def test_query_string_from_image_loader_is_ignored(app):
    response = call(app, "GET", b"/products/RoofBinoculars.jpg", query=b"w=640&q=75")
    assert response.status == 200
    assert response.body == (STATIC / "products/RoofBinoculars.jpg").read_bytes()


def test_missing_file_returns_nginx_404(app):
    response = call(app, "GET", b"/products/undefined")
    assert response.status == 404
    assert response.body == NOT_FOUND
    assert response.headers["content-type"] == "text/html"
    assert response.headers["content-length"] == str(len(NOT_FOUND))


def test_head_404_has_no_body(app):
    response = call(app, "HEAD", b"/nope.png")
    assert response.status == 404
    assert response.body == b""


def test_directory_without_slash_redirects(app):
    response = call(app, "GET", b"/products")
    assert response.status == 301
    assert response.headers["location"] == "http://image-provider:8081/products/"
    assert response.body == error_page(301)


def test_directory_redirect_keeps_query(app):
    response = call(app, "GET", b"/products", query=b"a=1")
    assert response.headers["location"] == "http://image-provider:8081/products/?a=1"


@pytest.mark.parametrize("path", [b"/", b"/products/"])
def test_directory_listing_is_forbidden(app, path):
    response = call(app, "GET", path)
    assert response.status == 403
    assert response.body == error_page(403)


def test_index_html_is_served_for_directory(tmp_app, tmp_path):
    (tmp_path / "index.html").write_bytes(b"<p>hi</p>")
    response = call(tmp_app, "GET", b"/")
    assert response.status == 200
    assert response.headers["content-type"] == "text/html"
    assert response.body == b"<p>hi</p>"


@pytest.mark.parametrize("path", [b"/Banner.png/", b"/missing/"])
def test_trailing_slash_on_non_directory_is_404(app, path):
    assert call(app, "GET", path).status == 404


@pytest.mark.parametrize(
    "method,path,status",
    [
        ("POST", b"/Banner.png", 405),
        ("POST", b"/missing.png", 404),
        ("POST", b"/products/", 403),
        ("PUT", b"/Banner.png", 405),
        ("PUT", b"/missing.png", 405),
        ("DELETE", b"/Banner.png", 405),
        ("OPTIONS", b"/", 405),
    ],
)
def test_method_handling(app, method, path, status):
    response = call(app, method, path)
    assert response.status == status
    assert response.body == error_page(status)


@pytest.mark.parametrize(
    "path,status",
    [
        (b"/../etc/passwd", 400),
        (b"/%2e%2e/etc/passwd", 400),
        (b"/products/../../x", 400),
        (b"/Banner%00.png", 400),
        (b"/products/../Banner.png", 200),
        (b"//Banner.png", 200),
        (b"/./products/./RedFlashlight.jpg", 200),
        (b"/products%2FRedFlashlight.jpg", 200),
        (b"/%42anner.png", 200),
    ],
)
def test_uri_normalization(app, path, status):
    assert call(app, "GET", path).status == status


def test_bad_request_closes_connection(app):
    response = call(app, "GET", b"/../x")
    assert response.headers["connection"] == "close"


def test_unknown_extension_defaults_to_text_plain(tmp_app, tmp_path):
    (tmp_path / "notes.weird").write_bytes(b"x")
    (tmp_path / "README").write_bytes(b"y")
    assert call(tmp_app, "GET", b"/notes.weird").headers["content-type"] == "text/plain"
    assert call(tmp_app, "GET", b"/README").headers["content-type"] == "text/plain"


def test_if_none_match_returns_304(app):
    etag = _etag(STATIC / "Banner.png")
    for value in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        response = call(app, "GET", b"/Banner.png", {"if-none-match": value})
        assert response.status == 304
        assert response.body == b""
        assert response.headers["etag"] == etag
        assert "content-type" not in response.headers
        assert "content-length" not in response.headers
        assert "accept-ranges" not in response.headers


def test_if_none_match_mismatch_returns_200(app):
    assert call(app, "GET", b"/Banner.png", {"if-none-match": '"nope"'}).status == 200


def test_if_modified_since_is_exact_match(app):
    mtime = int((STATIC / "Banner.png").stat().st_mtime)
    exact = call(app, "GET", b"/Banner.png", {"if-modified-since": formatdate(mtime, usegmt=True)})
    assert exact.status == 304
    later = call(app, "GET", b"/Banner.png", {"if-modified-since": formatdate(mtime + 60, usegmt=True)})
    assert later.status == 200
    garbage = call(app, "GET", b"/Banner.png", {"if-modified-since": "yesterday"})
    assert garbage.status == 200


def test_modified_since_overrides_matching_etag(app):
    etag = _etag(STATIC / "Banner.png")
    response = call(
        app,
        "GET",
        b"/Banner.png",
        {"if-none-match": etag, "if-modified-since": formatdate(0, usegmt=True)},
    )
    assert response.status == 200


def test_if_match_and_if_unmodified_since(app):
    mtime = int((STATIC / "Banner.png").stat().st_mtime)
    etag = _etag(STATIC / "Banner.png")
    assert call(app, "GET", b"/Banner.png", {"if-match": '"x"'}).status == 412
    assert call(app, "GET", b"/Banner.png", {"if-match": etag}).status == 200
    assert call(app, "GET", b"/Banner.png", {"if-match": f"W/{etag}"}).status == 412
    assert call(app, "GET", b"/Banner.png", {"if-unmodified-since": formatdate(mtime - 60, usegmt=True)}).status == 412
    assert call(app, "GET", b"/Banner.png", {"if-unmodified-since": formatdate(mtime, usegmt=True)}).status == 200
    assert call(app, "GET", b"/Banner.png", {"if-unmodified-since": "bogus"}).status == 412


def test_single_range(app):
    data = (STATIC / "Banner.png").read_bytes()
    response = call(app, "GET", b"/Banner.png", {"range": "bytes=0-9"})
    assert response.status == 206
    assert response.body == data[:10]
    assert response.headers["content-range"] == f"bytes 0-9/{len(data)}"
    assert response.headers["content-length"] == "10"
    assert "accept-ranges" not in response.headers


@pytest.mark.parametrize(
    "spec,slice_",
    [("bytes=-10", slice(-10, None)), ("bytes=100-", slice(100, None)), ("BYTES= 5 - 7 ", slice(5, 8))],
)
def test_range_forms(app, spec, slice_):
    data = (STATIC / "Banner.png").read_bytes()
    response = call(app, "GET", b"/Banner.png", {"range": spec})
    assert response.status == 206
    assert response.body == data[slice_]


def test_end_beyond_size_is_clamped(app):
    data = (STATIC / "Banner.png").read_bytes()
    response = call(app, "GET", b"/Banner.png", {"range": f"bytes=10-{len(data) * 2}"})
    assert response.status == 206
    assert response.headers["content-range"] == f"bytes 10-{len(data) - 1}/{len(data)}"


def test_multipart_ranges(app):
    data = (STATIC / "Banner.png").read_bytes()
    size = len(data)
    response = call(app, "GET", b"/Banner.png", {"range": "bytes=0-1,4-5"})
    assert response.status == 206
    content_type = response.headers["content-type"]
    assert content_type.startswith("multipart/byteranges; boundary=")
    boundary = content_type.split("=", 1)[1]
    expected = (
        f"\r\n--{boundary}\r\nContent-Type: image/png\r\nContent-Range: bytes 0-1/{size}\r\n\r\n".encode()
        + data[0:2]
        + f"\r\n--{boundary}\r\nContent-Type: image/png\r\nContent-Range: bytes 4-5/{size}\r\n\r\n".encode()
        + data[4:6]
        + f"\r\n--{boundary}--\r\n".encode()
    )
    assert response.body == expected
    assert response.headers["content-length"] == str(len(expected))


@pytest.mark.parametrize("spec", ["bytes=99999999-", "bytes=abc", "bytes=5-2x", "bytes=-0", "bytes=5-2"])
def test_unsatisfiable_range(app, spec):
    size = (STATIC / "Banner.png").stat().st_size
    response = call(app, "GET", b"/Banner.png", {"range": spec})
    assert response.status == 416
    assert response.headers["content-range"] == f"bytes */{size}"
    assert response.body == error_page(416)


@pytest.mark.parametrize("spec", ["items=0-1", "bytes", "bytes=0-,0-", "bytes=0-1,0-"])
def test_ignored_range_serves_full_file(app, spec):
    response = call(app, "GET", b"/Banner.png", {"range": spec})
    assert response.status == 200
    assert response.headers["accept-ranges"] == "bytes"


def test_if_range(app):
    path = STATIC / "Banner.png"
    etag = _etag(path)
    mtime = int(path.stat().st_mtime)
    assert call(app, "GET", b"/Banner.png", {"range": "bytes=0-1", "if-range": etag}).status == 206
    assert call(app, "GET", b"/Banner.png", {"range": "bytes=0-1", "if-range": '"old"'}).status == 200
    assert (
        call(app, "GET", b"/Banner.png", {"range": "bytes=0-1", "if-range": formatdate(mtime, usegmt=True)}).status
        == 206
    )
    assert (
        call(app, "GET", b"/Banner.png", {"range": "bytes=0-1", "if-range": formatdate(mtime - 1, usegmt=True)}).status
        == 200
    )


def test_gzip_static(tmp_app, tmp_path):
    (tmp_path / "logo.svg").write_bytes(b"<svg/>")
    compressed = gzip.compress(b"<svg/>")
    (tmp_path / "logo.svg.gz").write_bytes(compressed)
    response = call(tmp_app, "GET", b"/logo.svg", {"accept-encoding": "gzip, deflate"})
    assert response.status == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.body == compressed
    plain = call(tmp_app, "GET", b"/logo.svg")
    assert "content-encoding" not in plain.headers
    assert plain.body == b"<svg/>"
    assert call(tmp_app, "GET", b"/logo.svg", {"accept-encoding": "gzip;q=0"}).body == b"<svg/>"
    assert call(tmp_app, "GET", b"/logo.svg", {"accept-encoding": "gzip", "via": "1.1 proxy"}).body == b"<svg/>"
    assert call(tmp_app, "POST", b"/logo.svg", {"accept-encoding": "gzip"}).status == 405


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_unreadable_file_is_forbidden(tmp_app, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"x")
    secret.chmod(0)
    assert call(tmp_app, "GET", b"/secret.png").status == 403
