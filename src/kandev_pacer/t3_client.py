"""T3 0.0.44 RPC transport, adapted from the t3-start-thread skill."""

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

import websocket


class Server:
    """Keep a short-lived bearer session in memory for one invocation."""

    def __init__(self, url: str, base_dir: str | None) -> None:
        """Validate the endpoint and capture a bearer session privately."""
        self.base_dir = base_dir
        self.issued_at = time.monotonic()
        self.url = url.rstrip("/")
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            msg = "Server URL must use HTTP or HTTPS"
            raise ValueError(msg)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            msg = "Do not put credentials or query parameters in the URL"
            raise ValueError(msg)
        token = os.environ.get("T3_THREAD_AUTH_TOKEN")
        if not token:
            if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                msg = "Remote servers require T3_THREAD_AUTH_TOKEN"
                raise ValueError(msg)
            command = [
                shutil.which("t3") or "t3",
                "auth",
                "session",
                "issue",
                "--ttl",
                "5m",
                "--label",
                "T3 thread handoff",
                "--token-only",
            ]
            if base_dir:
                command.extend(["--base-dir", base_dir])
            token = subprocess.check_output(
                command,
                text=True,
                stderr=subprocess.PIPE,
                timeout=15,
                env={**os.environ, "SHELL": shutil.which("bash") or "bash"},
            ).strip()
        if not token:
            msg = "No bearer session was issued"
            raise ValueError(msg)
        self.token: str = token

    def call(self, method: str, payload: dict, *, stream: bool = False) -> dict:
        """Read one snapshot or a command acknowledgement, without retrying."""
        if (
            not os.environ.get("T3_THREAD_AUTH_TOKEN")
            and time.monotonic() - self.issued_at > 240
        ):
            self.__init__(self.url, self.base_dir)
        request = Request(
            self.url + "/api/auth/websocket-ticket",
            method="POST",
            headers={"Authorization": "Bearer " + self.token},
        )
        with urlopen(request, timeout=15) as response:
            ticket = json.load(response)["ticket"]
        ws_url = re.sub(r"^http", "ws", self.url) + "/ws?wsTicket=" + quote(ticket)
        socket = websocket.create_connection(ws_url, timeout=45)
        request_id = str(uuid.uuid4())
        deadline = time.monotonic() + 45
        try:
            socket.send(
                json.dumps(
                    {
                        "_tag": "Request",
                        "id": request_id,
                        "tag": method,
                        "payload": payload,
                        "headers": [],
                    }
                )
            )
            while time.monotonic() < deadline:
                socket.settimeout(max(0.1, deadline - time.monotonic()))
                message = json.loads(socket.recv())
                if message.get("_tag") == "Ping":
                    socket.send(json.dumps({"_tag": "Pong"}))
                    continue
                if str(message.get("requestId")) != request_id:
                    continue
                if stream and message.get("_tag") == "Chunk":
                    for value in message["values"]:
                        if value.get("kind") == "snapshot":
                            return value["snapshot"]
                if message.get("_tag") == "Exit":
                    result = message["exit"]
                    if result.get("_tag") != "Success":
                        raise RuntimeError("T3 rejected the RPC: " + json.dumps(result))
                    if stream:
                        msg = "T3 ended the stream without a snapshot"
                        raise RuntimeError(msg)
                    return result["value"]
            msg = "T3 did not respond before the deadline"
            raise TimeoutError(msg)
        finally:
            socket.close()

    def snapshot(self) -> dict:
        """Read the shell snapshot without printing other projects or messages."""
        return self.call("orchestration.subscribeShell", {}, stream=True)
