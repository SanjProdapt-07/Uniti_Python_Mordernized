# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass


@dataclass
class Message:
    to: str | list[str]
    from_: str
    subject: str
    body: str


class TestMailer:
    """In-memory delivery store, equivalent to Mail::TestMailer used by Pony's :test transport."""

    __test__ = False

    def __init__(self):
        self.deliveries: list[Message] = []

    @staticmethod
    def _validate(kind: str, addresses: list[str]) -> None:
        if not addresses or any(not a.strip() for a in addresses):
            raise ValueError(f"SMTP {kind} address may not be blank: {addresses!r}")
        for address in addresses:
            if len(address.encode()) > 2000:
                raise ValueError(f"SMTP {kind} address may not exceed 2000 bytes: {address!r}")
            if "\r" in address or "\n" in address:
                raise ValueError(f"SMTP {kind} address may not contain CR or LF line breaks: {address!r}")

    def mail(self, *, to, from_: str, subject: str, body: str) -> None:
        recipients = [] if to is None else [str(a) for a in to] if isinstance(to, list) else [str(to)]
        self._validate("From", [from_])
        self._validate("To", recipients)
        self.deliveries.append(Message(to=to, from_=from_, subject=subject, body=body))
