# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

REASONS = {
    301: "Moved Permanently",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Not Allowed",
    412: "Precondition Failed",
    416: "Requested Range Not Satisfiable",
    500: "Internal Server Error",
}

DEFAULT_TYPE = "text/plain"

MIME_TYPES = {
    "html": "text/html",
    "htm": "text/html",
    "shtml": "text/html",
    "css": "text/css",
    "xml": "text/xml",
    "gif": "image/gif",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "js": "application/javascript",
    "atom": "application/atom+xml",
    "rss": "application/rss+xml",
    "txt": "text/plain",
    "avif": "image/avif",
    "png": "image/png",
    "svg": "image/svg+xml",
    "svgz": "image/svg+xml",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "wbmp": "image/vnd.wap.wbmp",
    "webp": "image/webp",
    "ico": "image/x-icon",
    "jng": "image/x-jng",
    "bmp": "image/x-ms-bmp",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "json": "application/json",
    "pdf": "application/pdf",
    "wasm": "application/wasm",
    "zip": "application/zip",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "bin": "application/octet-stream",
}


def content_type_for(uri: bytes) -> str:
    name = uri.rsplit(b"/", 1)[-1]
    if b"." not in name:
        return DEFAULT_TYPE
    ext = name.rsplit(b".", 1)[-1].decode("latin-1").lower()
    return MIME_TYPES.get(ext, DEFAULT_TYPE)


def error_page(status: int) -> bytes:
    title = f"{status} {REASONS[status]}"
    return (
        "<html>\r\n"
        f"<head><title>{title}</title></head>\r\n"
        "<body>\r\n"
        f"<center><h1>{title}</h1></center>\r\n"
        "<hr><center>nginx</center>\r\n"
        "</body>\r\n"
        "</html>\r\n"
    ).encode()
