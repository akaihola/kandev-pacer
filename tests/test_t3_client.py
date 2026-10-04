"""Exercise T3's RPC envelope, authentication, and stream handling offline."""

import io
import json
import socket
import ssl
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from kandev_pacer import t3_client


@pytest.mark.parametrize(
    "error, reason, setting",
    [
        (
            URLError(ConnectionRefusedError(111, "private-token")),
            "connection refused",
            "PACER_T3_URL",
        ),
        (
            URLError(socket.gaierror(-2, "private-token")),
            "hostname lookup failed",
            "PACER_T3_URL",
        ),
        (
            URLError(TimeoutError("private-token")),
            "connection timed out",
            "PACER_T3_URL",
        ),
        (TimeoutError("private-token"), "connection timed out", "PACER_T3_URL"),
        (URLError(ssl.SSLError("private-token")), "TLS verification", "PACER_T3_URL"),
        (
            URLError("http://secret@host/?token=private-token"),
            "connection failed",
            "PACER_T3_URL",
        ),
        (
            HTTPError("private-token", 401, "private-token", Message(), None),
            "HTTP 401",
            "T3_THREAD_AUTH_TOKEN",
        ),
        (
            HTTPError("private-token", 403, "private-token", Message(), None),
            "HTTP 403",
            "PACER_T3_BASE_DIR",
        ),
        (
            HTTPError("private-token", 502, "private-token", Message(), None),
            "HTTP 502",
            "reverse proxy",
        ),
    ],
)
def test_ticket_errors_explain_configuration_without_secrets(
    monkeypatch, error, reason, setting
):
    monkeypatch.setenv("T3_THREAD_AUTH_TOKEN", "private-token")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(t3_client, "urlopen", fail)
    monkeypatch.setattr(
        t3_client.websocket,
        "create_connection",
        lambda *a, **kw: pytest.fail("connected"),
    )
    server = t3_client.Server("https://remote.example/private-path", None)
    with pytest.raises(t3_client.T3ConnectionError) as caught:
        server.snapshot()
    message = str(caught.value)
    assert "https://remote.example" in message
    assert reason in message
    assert setting in message
    assert "--config" in message
    assert "docs/t3-pacing.md#connection-errors" in message
    assert "private-token" not in message
    assert "private-path" not in message


def test_remote_requires_token(monkeypatch):
    monkeypatch.delenv("T3_THREAD_AUTH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Remote servers require"):
        t3_client.Server("https://remote.example", None)


@pytest.mark.parametrize(
    "url", ["file:///tmp/server", "http://user:secret@host", "http://host?token=x"]
)
def test_rejects_credential_urls(url):
    with pytest.raises(ValueError):
        t3_client.Server(url, None)


def test_stream_rpc_and_ticket_stay_in_memory(monkeypatch):
    monkeypatch.setenv("T3_THREAD_AUTH_TOKEN", "private-token")
    requests, sent = [], []

    def ticket(request, **kwargs):
        assert request.get_header("Authorization") == "Bearer private-token"
        requests.append(request.full_url)
        return io.BytesIO(b'{"ticket":"private-ticket"}')

    class Socket:
        closed = False
        replies = []

        def send(self, text):
            message = json.loads(text)
            sent.append(message)
            if message["_tag"] == "Request":
                self.replies = [
                    {"_tag": "Ping"},
                    {"_tag": "Chunk", "requestId": "unrelated", "values": []},
                    {
                        "_tag": "Chunk",
                        "requestId": message["id"],
                        "values": [
                            {
                                "kind": "snapshot",
                                "snapshot": {"threads": [], "projects": []},
                            }
                        ],
                    },
                ]

        def recv(self):
            return json.dumps(self.replies.pop(0))

        def settimeout(self, timeout):
            assert timeout > 0

        def close(self):
            self.closed = True

    socket = Socket()

    def connect(url, **kwargs):
        assert url == "wss://remote.example/ws?wsTicket=private-ticket"
        return socket

    monkeypatch.setattr(t3_client, "urlopen", ticket)
    monkeypatch.setattr(t3_client.websocket, "create_connection", connect)
    server = t3_client.Server("https://remote.example", None)
    assert server.snapshot() == {"threads": [], "projects": []}
    assert sent[0]["tag"] == "orchestration.subscribeShell"
    assert sent[-1] == {"_tag": "Pong"}
    assert socket.closed
    assert "private-token" not in json.dumps(sent)


def test_local_token_renews_before_expiry(monkeypatch):
    monkeypatch.delenv("T3_THREAD_AUTH_TOKEN", raising=False)
    issued = []

    def issue(command, **kwargs):
        issued.append(command)
        assert kwargs["stderr"] == t3_client.subprocess.PIPE
        return "private-token"

    monkeypatch.setattr(t3_client.subprocess, "check_output", issue)
    server = t3_client.Server("http://localhost:3773", "/private/t3")
    server.issued_at -= 241
    monkeypatch.setattr(
        t3_client, "urlopen", lambda *a, **kw: (_ for _ in ()).throw(TimeoutError())
    )
    with pytest.raises(t3_client.T3ConnectionError, match="connection timed out"):
        server.snapshot()
    assert len(issued) == 2
    assert issued[-1][-2:] == ["--base-dir", "/private/t3"]
