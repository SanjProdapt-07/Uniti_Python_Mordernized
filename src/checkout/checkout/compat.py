# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
"""Helpers that keep strings and payloads identical to the Go implementation."""

import json

import grpc
from google.protobuf.descriptor import FieldDescriptor

_GO_CODE_NAMES = {
    grpc.StatusCode.OK: "OK",
    grpc.StatusCode.CANCELLED: "Canceled",
    grpc.StatusCode.UNKNOWN: "Unknown",
    grpc.StatusCode.INVALID_ARGUMENT: "InvalidArgument",
    grpc.StatusCode.DEADLINE_EXCEEDED: "DeadlineExceeded",
    grpc.StatusCode.NOT_FOUND: "NotFound",
    grpc.StatusCode.ALREADY_EXISTS: "AlreadyExists",
    grpc.StatusCode.PERMISSION_DENIED: "PermissionDenied",
    grpc.StatusCode.RESOURCE_EXHAUSTED: "ResourceExhausted",
    grpc.StatusCode.FAILED_PRECONDITION: "FailedPrecondition",
    grpc.StatusCode.ABORTED: "Aborted",
    grpc.StatusCode.OUT_OF_RANGE: "OutOfRange",
    grpc.StatusCode.UNIMPLEMENTED: "Unimplemented",
    grpc.StatusCode.INTERNAL: "Internal",
    grpc.StatusCode.UNAVAILABLE: "Unavailable",
    grpc.StatusCode.DATA_LOSS: "DataLoss",
    grpc.StatusCode.UNAUTHENTICATED: "Unauthenticated",
}

DEADLINE_EXCEEDED = "context deadline exceeded"
CANCELED = "context canceled"


def rpc_error(err):
    code = err.code() if callable(getattr(err, "code", None)) else grpc.StatusCode.UNKNOWN
    details = err.details() if callable(getattr(err, "details", None)) else str(err)
    return f"rpc error: code = {_GO_CODE_NAMES.get(code, 'Unknown')} desc = {details or ''}"


def go_quote(s):
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch in "\a\b\f\n\r\t\v":
            out.append("\\" + "abfnrtv"["\a\b\f\n\r\t\v".index(ch)])
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\x{o:02x}")
        elif not ch.isprintable() and ch != " ":
            out.append(f"\\u{o:04x}" if o <= 0xFFFF else f"\\U{o:08x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _trunc_div(a, b):
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def money_float(m):
    """strconv.ParseFloat(fmt.Sprintf("%d.%02d", units, nanos/10000000)), 0 on parse error."""
    text = "%d.%02d" % (m.units, _trunc_div(m.nanos, 10000000))
    try:
        return float(text)
    except ValueError:
        return 0.0


_UNITS = ((3_600_000_000_000, "h"), (60_000_000_000, "m"))


def _fmt_frac(v, prec):
    """Go's fmtFrac: v / 10**prec printed without trailing zeros."""
    integer, frac = divmod(v, 10**prec)
    digits = f"{frac:0{prec}d}".rstrip("0")
    return integer, ("." + digits if digits else "")


def go_duration(seconds):
    """time.Duration.String() for a duration given in seconds."""
    ns = int(round(seconds * 1e9))
    if ns == 0:
        return "0s"
    sign = "-" if ns < 0 else ""
    u = abs(ns)
    if u < 1_000_000_000:
        if u < 1_000:
            return f"{sign}{u}ns"
        if u < 1_000_000:
            whole, frac = _fmt_frac(u, 3)
            return f"{sign}{whole}{frac}µs"
        whole, frac = _fmt_frac(u, 6)
        return f"{sign}{whole}{frac}ms"
    secs, frac = _fmt_frac(u, 9)
    out = f"{secs % 60}{frac}s"
    mins = secs // 60
    if mins > 0:
        out = f"{mins % 60}m{out}"
        hours = mins // 60
        if hours > 0:
            out = f"{hours}h{out}"
    return sign + out


def proto_to_go_json(msg):
    """encoding/json view of a protoc-gen-go struct: proto field names, omitempty, int64 as numbers."""
    if msg is None:
        return None
    out = {}
    for field, value in msg.ListFields():
        if field.type == FieldDescriptor.TYPE_MESSAGE:
            if field.is_repeated:
                out[field.name] = [proto_to_go_json(v) for v in value]
            else:
                out[field.name] = proto_to_go_json(value)
        elif field.is_repeated:
            out[field.name] = list(value)
        else:
            out[field.name] = value
    return out


def go_json_dumps(obj):
    """json.Marshal of a map[string]interface{}: top-level keys sorted, compact, HTML-safe escaping."""
    text = json.dumps({k: obj[k] for k in sorted(obj)}, separators=(",", ":"), ensure_ascii=False)
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
        .encode("utf-8")
    )


class JSONDecodeError(ValueError):
    pass


def _reject_constant(name):
    raise JSONDecodeError(f"invalid character '{name[0]}' looking for beginning of value")


def go_json_loads(data):
    try:
        return json.loads(data, parse_constant=_reject_constant)
    except JSONDecodeError:
        raise
    except ValueError as exc:
        raise JSONDecodeError(str(exc)) from exc


def go_field(obj, name):
    """encoding/json matches object keys to struct tags case-insensitively, preferring an exact match."""
    if name in obj:
        return obj[name]
    lowered = name.lower()
    for key, value in obj.items():
        if key.lower() == lowered:
            return value
    return None


_INT_RANGES = {"int64": (-(2**63), 2**63 - 1), "int32": (-(2**31), 2**31 - 1)}


def go_int(value, kind, path):
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JSONDecodeError(f"json: cannot unmarshal {_go_json_type(value)} into Go struct field {path} of type {kind}")
    lo, hi = _INT_RANGES[kind]
    if isinstance(value, float) or not lo <= value <= hi:
        raise JSONDecodeError(f"json: cannot unmarshal number {value} into Go struct field {path} of type {kind}")
    return value


def go_str(value, path):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise JSONDecodeError(f"json: cannot unmarshal {_go_json_type(value)} into Go struct field {path} of type string")
    return value


def go_object(value, path, type_name):
    if value is None or isinstance(value, dict):
        return value
    raise JSONDecodeError(f"json: cannot unmarshal {_go_json_type(value)} into Go {path} of type {type_name}")


def _go_json_type(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"
