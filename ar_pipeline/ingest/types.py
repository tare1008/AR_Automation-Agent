from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class GraphMessage:
    id: str
    internet_message_id: str
    sender_address: str
    subject: str
    received_at: datetime
    body_html: str
    body_text: str
    has_attachments: bool
    removed: bool = False


@dataclass(frozen=True, slots=True)
class GraphAttachment:
    name: str
    content_type: str
    size: int
    content: bytes


@dataclass(frozen=True, slots=True)
class DeltaResult:
    messages: list[GraphMessage]
    delta_link: str
