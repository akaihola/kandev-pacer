"""Exercise T3's RPC envelope, authentication, and stream handling offline."""

import io
import json

import pytest

from kandev_pacer import t3_client


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
    with pytest.raises(TimeoutError):
        server.snapshot()
    assert len(issued) == 2
    assert issued[-1][-2:] == ["--base-dir", "/private/t3"]
