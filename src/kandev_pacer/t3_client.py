"""T3 V2 RPC transport, adapted from the t3-start-thread skill."""

import errno
import json
import os
import re
import shutil
import ssl
import subprocess
import time
import uuid
from socket import gaierror
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

import websocket


class T3ConnectionError(RuntimeError):
    """A transport failure explained without tokens or response contents."""


class T3ConfigurationError(ValueError):
    """A configuration failure with an explanation safe to log."""


class T3Rejected(RuntimeError):
    """T3 answered the RPC with a failure instead of an acknowledgement."""


class Server:
    """Keep a short-lived bearer session in memory for one invocation."""

    def __init__(self, url: str, base_dir: str | None) -> None:
        """Validate the endpoint and capture a bearer session privately."""
        self.base_dir = base_dir
        self.issued_at = time.monotonic()
        self.url = url.rstrip("/")
        try:
            parsed = urlsplit(self.url)
            hostname = parsed.hostname
            _ = parsed.port
        except ValueError:
            raise T3ConfigurationError(
                "Invalid t3_url (PACER_T3_URL). Set a valid HTTP or HTTPS server URL "
                "in [kandev-pacer] in the TOML selected by --config; "
                "check environment overrides. See docs/t3-pacing.md#connection-errors."
            ) from None
        if parsed.scheme not in {"http", "https"} or not hostname:
            msg = (
                "t3_url (PACER_T3_URL) must use HTTP or HTTPS and include a hostname. "
                "Check [kandev-pacer] in the TOML selected by --config and environment "
                "overrides. See docs/t3-pacing.md#connection-errors."
            )
            raise T3ConfigurationError(msg)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            msg = (
                "Remove credentials, query parameters, and fragments from t3_url "
                "(PACER_T3_URL). Supply the bearer token through T3_THREAD_AUTH_TOKEN "
                "in the process environment. Check the TOML selected by --config "
                "and environment overrides. See docs/t3-pacing.md#connection-errors."
            )
            raise T3ConfigurationError(msg)
        token = os.environ.get("T3_THREAD_AUTH_TOKEN")
        if not token:
            if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                msg = (
                    "Non-loopback t3_url (PACER_T3_URL) requires T3_THREAD_AUTH_TOKEN "
                    "in the process environment, even on the same machine. On the T3 "
                    "server's machine, issue a token with `t3 auth session issue "
                    "--ttl 1h --label kandev-pacer --token-only` and export it before "
                    "starting the pacer. Add --base-dir if the server uses a custom "
                    "state directory. See docs/t3-pacing.md#connection-errors."
                )
                raise T3ConfigurationError(msg)
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
            msg = (
                "t3 auth session issue returned no bearer token. Check the T3 CLI "
                "installation and t3_base_dir (PACER_T3_BASE_DIR); use the running "
                "server's state directory. See docs/t3-pacing.md#connection-errors."
            )
            raise T3ConfigurationError(msg)
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
        try:
            with urlopen(request, timeout=15) as response:
                ticket = json.load(response)["ticket"]
        except (URLError, TimeoutError) as error:
            endpoint = urlsplit(self.url)
            # Only show the origin; a proxy path or exception may contain secrets.
            origin = f"{endpoint.scheme}://{endpoint.netloc}"
            if isinstance(error, HTTPError):
                reason = f"HTTP {error.code}"
                hint = (
                    "Check T3_THREAD_AUTH_TOKEN and ensure t3_base_dir "
                    "(PACER_T3_BASE_DIR) matches the running server's state directory."
                    if error.code in {401, 403}
                    else "Check the T3 server and any reverse proxy."
                )
            else:
                cause = error.reason if isinstance(error, URLError) else error
                if isinstance(cause, gaierror):
                    reason = "hostname lookup failed"
                elif isinstance(cause, ssl.SSLError):
                    reason = "TLS verification or handshake failed"
                elif isinstance(cause, TimeoutError):
                    reason = "connection timed out"
                elif isinstance(cause, OSError) and cause.errno == errno.ECONNREFUSED:
                    reason = "connection refused"
                else:
                    reason = "connection failed"
                hint = (
                    "Ensure T3 is running and t3_url (PACER_T3_URL) matches its "
                    "listening address and port; check network/proxy/TLS settings."
                )
            raise T3ConnectionError(
                f"T3 HTTP ticket request to {origin} failed: {reason}. {hint} "
                "Configure [kandev-pacer] in the TOML selected by --config; "
                "environment settings override TOML. See docs/t3-pacing.md#connection-errors."
            ) from None
        ws_url = (
            re.sub(r"^http", "ws", self.url)
            + "/ws?orchestrationProtocol=2&wsTicket="
            + quote(ticket)
        )
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
                        raise T3Rejected("T3 rejected the RPC: " + json.dumps(result))
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
