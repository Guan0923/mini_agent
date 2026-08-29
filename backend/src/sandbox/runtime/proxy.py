"""Backend-owned authenticated loopback proxy for restricted command jobs."""

from __future__ import annotations

import base64
import ipaddress
import logging
import secrets
import select
import socket
import threading
import time
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import urlsplit

import h11

from ..errors import SandboxInitializationError
from ..policy import NetworkRule

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProxyCredential:
    username: str
    password: str

    def url(self, port: int) -> str:
        encoded_user = self.username.replace("%", "%25").replace(":", "%3A").replace("@", "%40")
        encoded_password = self.password.replace("%", "%25").replace(":", "%3A").replace("@", "%40")
        return f"http://{encoded_user}:{encoded_password}@127.0.0.1:{port}"


@dataclass(slots=True)
class _Grant:
    job_id: str
    password: str
    rules: tuple[NetworkRule, ...]
    expires_at: float


class RunCommandProxy:
    """One process-wide HTTP/1.1 forward proxy bound only to loopback."""

    _instances: ClassVar[dict[int, RunCommandProxy]] = {}
    _instances_lock: ClassVar[threading.RLock] = threading.RLock()

    def __init__(self, port: int, *, clock=None, resolver=None) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("proxy port must be between 1 and 65535")
        self.port = port
        self._clock = clock or time.time
        self._resolver = resolver or socket.getaddrinfo
        self._grants: dict[str, _Grant] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None

    @classmethod
    def shared(cls, port: int) -> RunCommandProxy:
        with cls._instances_lock:
            proxy = cls._instances.get(port)
            if proxy is None:
                proxy = cls(port)
                proxy.start()
                cls._instances[port] = proxy
            return proxy

    def start(self) -> None:
        if self._thread is not None:
            return
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", self.port))
            listener.listen(64)
            listener.settimeout(0.5)
        except OSError as exc:
            listener.close()
            raise SandboxInitializationError("run_command proxy port is unavailable") from exc
        self._listener = listener
        self._thread = threading.Thread(target=self._serve, name="run-command-proxy", daemon=True)
        self._thread.start()

    def issue(self, job_id: str, rules: tuple[NetworkRule, ...], *, ttl_seconds: int) -> ProxyCredential:
        if not job_id or not rules or not 1 <= ttl_seconds <= 3600:
            raise ValueError("proxy grant is invalid")
        credential = ProxyCredential(f"job-{secrets.token_urlsafe(12)}", secrets.token_urlsafe(32))
        with self._lock:
            self._purge_expired()
            self._grants[credential.username] = _Grant(
                job_id,
                credential.password,
                tuple(rules),
                self._clock() + ttl_seconds,
            )
        return credential

    def revoke_job(self, job_id: str) -> None:
        with self._lock:
            self._grants = {name: grant for name, grant in self._grants.items() if grant.job_id != job_id}

    def close(self) -> None:
        self._stop.set()
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)

    def _serve(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stop.is_set():
            try:
                client, address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if address[0] != "127.0.0.1":
                client.close()
                continue
            threading.Thread(target=self._handle, args=(client,), name="run-command-proxy-client", daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            client.settimeout(15.0)
            connection = h11.Connection(h11.SERVER)
            request: h11.Request | None = None
            body = bytearray()
            complete = False
            while not complete:
                while True:
                    event = connection.next_event()
                    if event is h11.NEED_DATA:
                        break
                    if isinstance(event, h11.Request):
                        request = event
                    elif isinstance(event, h11.Data):
                        body.extend(event.data)
                    elif isinstance(event, h11.EndOfMessage):
                        complete = True
                        break
                if complete:
                    break
                data = client.recv(65536)
                if not data:
                    return
                connection.receive_data(data)
            if request is None:
                return
            grant = self._authorize(request)
            if grant is None:
                self._send_error(client, 407, b"Proxy authentication required", proxy_auth=True)
                return
            method = request.method.decode("ascii", errors="strict").upper()
            host, port, target = self._target(request, method)
            if not self._allowed(grant.rules, host, port):
                self._send_error(client, 403, b"Proxy target denied")
                return
            upstream = self._connect_once(host, port)
            if method == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._tunnel(client, upstream)
                return
            headers = [
                (name, value)
                for name, value in request.headers
                if name.lower() not in {b"proxy-authorization", b"proxy-connection", b"connection"}
            ]
            headers.append((b"Connection", b"close"))
            outbound = h11.Connection(h11.CLIENT)
            upstream.sendall(outbound.send(h11.Request(method=request.method, target=target, headers=headers)))
            if body:
                upstream.sendall(outbound.send(h11.Data(data=bytes(body))))
            upstream.sendall(outbound.send(h11.EndOfMessage()))
            while True:
                data = upstream.recv(65536)
                if not data:
                    return
                client.sendall(data)
        except Exception as exc:
            logger.debug("run_command proxy request failed: %s", type(exc).__name__)
            try:
                self._send_error(client, 502, b"Proxy request failed")
            except OSError:
                pass
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass
            try:
                client.close()
            except OSError:
                pass

    def _authorize(self, request: h11.Request) -> _Grant | None:
        raw = next((value for name, value in request.headers if name.lower() == b"proxy-authorization"), None)
        if raw is None or not raw.startswith(b"Basic "):
            return None
        try:
            username, password = base64.b64decode(raw[6:], validate=True).decode("utf-8").split(":", 1)
        except (ValueError, UnicodeError):
            return None
        with self._lock:
            self._purge_expired()
            grant = self._grants.get(username)
            if grant is None or not secrets.compare_digest(grant.password, password):
                return None
            return grant

    @staticmethod
    def _target(request: h11.Request, method: str) -> tuple[str, int, bytes]:
        raw_target = request.target.decode("ascii", errors="strict")
        if method == "CONNECT":
            parsed = urlsplit(f"//{raw_target}")
            if not parsed.hostname or parsed.port is None:
                raise ValueError("CONNECT target is invalid")
            return parsed.hostname, parsed.port, request.target
        parsed = urlsplit(raw_target)
        if parsed.scheme.lower() != "http" or not parsed.hostname:
            raise ValueError("HTTP proxy target must use http")
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        return parsed.hostname, port, path.encode("ascii")

    @staticmethod
    def _allowed(rules: tuple[NetworkRule, ...], host: str, port: int) -> bool:
        candidate = _canonical_host(host)
        host_rules = [rule for rule in rules if _canonical_host(rule.host) == candidate]
        return bool(host_rules) and (
            any(rule.port is None for rule in host_rules) or any(rule.port == port for rule in host_rules)
        )

    def _connect_once(self, host: str, port: int) -> socket.socket:
        answers = self._resolver(host, port, type=socket.SOCK_STREAM)
        if not answers:
            raise OSError("proxy target did not resolve")
        family, socktype, protocol, _, sockaddr = answers[0]
        upstream = socket.socket(family, socktype, protocol)
        upstream.settimeout(15.0)
        try:
            upstream.connect(sockaddr)
        except Exception:
            upstream.close()
            raise
        return upstream

    @staticmethod
    def _tunnel(client: socket.socket, upstream: socket.socket) -> None:
        sockets = (client, upstream)
        for current in sockets:
            current.settimeout(None)
        while True:
            readable, _, _ = select.select(sockets, (), (), 30.0)
            if not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                destination = upstream if source is client else client
                destination.sendall(data)

    @staticmethod
    def _send_error(client: socket.socket, status: int, body: bytes, *, proxy_auth: bool = False) -> None:
        headers = [(b"Content-Length", str(len(body)).encode("ascii")), (b"Connection", b"close")]
        if proxy_auth:
            headers.append((b"Proxy-Authenticate", b'Basic realm="run_command"'))
        connection = h11.Connection(h11.SERVER)
        client.sendall(connection.send(h11.Response(status_code=status, headers=headers)))
        client.sendall(connection.send(h11.Data(data=body)))
        client.sendall(connection.send(h11.EndOfMessage()))

    def _purge_expired(self) -> None:
        now = self._clock()
        self._grants = {name: grant for name, grant in self._grants.items() if grant.expires_at > now}


def _canonical_host(value: str) -> str:
    host = value.rstrip(".").casefold()
    try:
        return str(ipaddress.ip_address(host.split("%", 1)[0]))
    except ValueError:
        return host.encode("idna").decode("ascii")


__all__ = ["ProxyCredential", "RunCommandProxy"]
