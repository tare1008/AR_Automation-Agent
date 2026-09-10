RECEIVED: dict[str, dict] = {}
_IDEMPOTENCY: dict[str, str] = {}  # idempotency key -> extraction_id


def received_list() -> list[dict]:
    return list(RECEIVED.values())
