# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

from typing import Any

from google.protobuf import json_format
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message

MAX_SAFE_INTEGER = 2**53 - 1

_INT64_TYPES = {
    FieldDescriptor.TYPE_INT64,
    FieldDescriptor.TYPE_UINT64,
    FieldDescriptor.TYPE_SINT64,
    FieldDescriptor.TYPE_FIXED64,
    FieldDescriptor.TYPE_SFIXED64,
}


def _scalar(field: FieldDescriptor, value: Any) -> Any:
    if field.type in _INT64_TYPES and abs(value) > MAX_SAFE_INTEGER:
        raise ValueError("Value is larger than Number.MAX_SAFE_INTEGER")
    return value


def to_js(message: Message) -> dict[str, Any]:
    """Shape a decoded message exactly like ts-proto's decode() output."""
    out: dict[str, Any] = {}
    for field in message.DESCRIPTOR.fields:
        value = getattr(message, field.name)
        if field.is_repeated:
            if field.message_type is not None:
                out[field.json_name] = [to_js(v) for v in value]
            else:
                out[field.json_name] = [_scalar(field, v) for v in value]
        elif field.message_type is not None:
            if message.HasField(field.name):
                out[field.json_name] = to_js(value)
        else:
            out[field.json_name] = _scalar(field, value)
    return out


def from_js[M: Message](cls: type[M], data: Any) -> M:
    message = cls()
    if data is None:
        return message
    json_format.ParseDict(data, message, ignore_unknown_fields=True)
    return message
