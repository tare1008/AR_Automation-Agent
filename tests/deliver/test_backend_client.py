import httpx

from ar_pipeline.deliver.backend_client import BackendClient


def _client(handler, *, auth_header="") -> BackendClient:
    transport = httpx.MockTransport(handler)
    return BackendClient(
        base_url="https://backend.example/api",
        auth_header=auth_header,
        http=httpx.Client(transport=transport, base_url="https://backend.example/api"),
    )


_PAYLOAD = {"envelope": {"extraction_id": "ext-1"}, "header": {}, "line_items": []}


def test_2xx_is_ok_and_sends_idempotency_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["idem"] = request.headers.get("idempotency-key")
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(201, json={"id": "ext-1", "status": "received"})

    r = _client(handler).send(_PAYLOAD, "ext-1")
    assert r.outcome == "ok"
    assert r.status_code == 201
    assert seen["url"] == "https://backend.example/api/remittances"
    assert seen["idem"] == "ext-1"
    assert seen["auth"] is None  # no auth_header configured


def test_auth_header_sent_when_configured():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sekret"
        return httpx.Response(200, json={})

    _client(handler, auth_header="Bearer sekret").send(_PAYLOAD, "ext-1")


def test_400_is_permanent_fail():
    r = _client(lambda req: httpx.Response(400, text="bad payload")).send(_PAYLOAD, "ext-1")
    assert r.outcome == "permanent_fail"
    assert r.status_code == 400
    assert "bad payload" in r.detail


def test_429_is_transient_fail():
    r = _client(lambda req: httpx.Response(429, text="slow down")).send(_PAYLOAD, "ext-1")
    assert r.outcome == "transient_fail"
    assert r.status_code == 429


def test_500_is_transient_fail():
    r = _client(lambda req: httpx.Response(503, text="down")).send(_PAYLOAD, "ext-1")
    assert r.outcome == "transient_fail"
    assert r.status_code == 503


def test_timeout_is_transient_fail():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    r = _client(handler).send(_PAYLOAD, "ext-1")
    assert r.outcome == "transient_fail"
    assert r.status_code is None
    assert "Timeout" in r.detail or "timed out" in r.detail


def test_transport_error_is_transient_fail():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    r = _client(handler).send(_PAYLOAD, "ext-1")
    assert r.outcome == "transient_fail"
    assert r.status_code is None


def test_detail_is_bounded():
    r = _client(lambda req: httpx.Response(400, text="x" * 5000)).send(_PAYLOAD, "ext-1")
    assert len(r.detail) <= 500
