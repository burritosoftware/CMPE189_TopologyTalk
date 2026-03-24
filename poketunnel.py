from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import struct
import time
import webbrowser
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, Optional
from urllib.parse import quote, urlparse

import httpx
import websockets
from websockets.client import WebSocketClientProtocol


DEFAULT_BASE_URL = "https://poke.com/api/v1"
DEFAULT_FRONTEND_URL = "https://poke.com"
DEFAULT_LOGIN_TIMEOUT_MS = 5 * 60 * 1000
LOGIN_POLL_INTERVAL_MS = 2000


class PokeAuthError(Exception):
    pass


class PikoAuthError(Exception):
    pass


class PikoConnectionError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class LoginOptions:
    on_code: Optional[Callable[[dict[str, str]], None]] = None
    open_browser: bool = True
    timeout_ms: int = DEFAULT_LOGIN_TIMEOUT_MS
    base_url: Optional[str] = None
    frontend_url: Optional[str] = None


@dataclass
class LoginResult:
    token: str


@dataclass
class TunnelOptions:
    url: str
    name: str
    token: Optional[str] = None
    base_url: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    sync_interval_ms: int = 5 * 60 * 1000
    cleanup_on_stop: bool = True


@dataclass
class TunnelInfo:
    connection_id: str
    tunnel_url: str
    local_url: str
    name: str


class _CredentialsStore:
    @staticmethod
    def config_dir() -> Path:
        xdg = os.getenv("XDG_CONFIG_HOME")
        if xdg:
            return Path(xdg) / "poke"
        return Path.home() / ".config" / "poke"

    @classmethod
    def credentials_path(cls) -> Path:
        return cls.config_dir() / "credentials.json"

    @classmethod
    def write_token(cls, token: str) -> None:
        cls.config_dir().mkdir(parents=True, exist_ok=True)
        path = cls.credentials_path()
        path.write_text(json.dumps({"token": token}, indent=2), encoding="utf-8")
        with contextlib.suppress(Exception):
            path.chmod(0o600)

    @classmethod
    def read(cls) -> Optional[dict[str, Any]]:
        try:
            return json.loads(cls.credentials_path().read_text(encoding="utf-8"))
        except Exception:
            return None

    @classmethod
    def clear(cls) -> None:
        with contextlib.suppress(FileNotFoundError):
            cls.credentials_path().unlink()


def get_token() -> Optional[str]:
    data = _CredentialsStore.read()
    token = data.get("token") if data else None
    return token if isinstance(token, str) else None


def is_logged_in() -> bool:
    return get_token() is not None


async def logout() -> None:
    _CredentialsStore.clear()


async def fetch_with_auth(
    *,
    path: str,
    options: Optional[dict[str, Any]] = None,
    token: Optional[str] = None,
    base_url: Optional[str] = None,
) -> httpx.Response:
    auth_token = token or get_token()
    if not auth_token:
        raise PokeAuthError("Not logged in. Run 'poke login'.")

    opts = dict(options or {})
    method = opts.pop("method", "GET")
    headers = dict(opts.pop("headers", {}))
    headers["Authorization"] = f"Bearer {auth_token}"
    url = f"{base_url or os.getenv('POKE_API') or DEFAULT_BASE_URL}{path}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(method, url, headers=headers, **opts)

    if response.status_code == 401:
        _CredentialsStore.clear()
        raise PokeAuthError("Session expired. Run 'poke login' again.")

    return response


async def login(options: Optional[LoginOptions] = None) -> LoginResult:
    options = options or LoginOptions()
    existing = get_token()
    if existing:
        return LoginResult(token=existing)

    base_url = options.base_url or os.getenv("POKE_API") or DEFAULT_BASE_URL
    frontend_url = options.frontend_url or os.getenv("POKE_FRONTEND") or DEFAULT_FRONTEND_URL

    async with httpx.AsyncClient(timeout=30.0) as client:
        code_res = await client.post(f"{base_url}/cli-auth/code")
        if not code_res.is_success:
            raise PokeAuthError("Failed to create login code")

        payload = code_res.json()
        device_code = payload["deviceCode"]
        user_code = payload["userCode"]
        login_url = f"{frontend_url}/device?code={quote(user_code)}"

        if options.on_code:
            options.on_code({"userCode": user_code, "loginUrl": login_url})

        if options.open_browser:
            with contextlib.suppress(Exception):
                webbrowser.open(login_url)

        deadline = time.monotonic() + (options.timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            await asyncio.sleep(LOGIN_POLL_INTERVAL_MS / 1000.0)
            poll_res = await client.get(f"{base_url}/cli-auth/poll/{device_code}")
            poll = poll_res.json()

            if poll.get("status") == "authenticated":
                token = poll["token"]
                _CredentialsStore.write_token(token)
                return LoginResult(token=token)
            if poll.get("status") == "expired":
                raise PokeAuthError("Login code expired.")
            if poll.get("status") == "invalid":
                raise PokeAuthError("Invalid login code.")

    raise PokeAuthError("Login timed out.")


EventHandler = Callable[..., Any]


class _EventEmitter:
    def __init__(self) -> None:
        self._listeners: dict[str, set[EventHandler]] = defaultdict(set)

    def on(self, event: str, handler: EventHandler):
        self._listeners[event].add(handler)
        return self

    def off(self, event: str, handler: EventHandler):
        handlers = self._listeners.get(event)
        if handlers:
            handlers.discard(handler)
        return self

    def emit(self, event: str, *args: Any) -> None:
        for handler in list(self._listeners.get(event, ())):
            try:
                result = handler(*args)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:
                pass


class _ByteTransportBuffer:
    def __init__(self, max_total_bytes: int = 16 * 1024 * 1024) -> None:
        self._chunks: Deque[bytes] = deque()
        self._total = 0
        self._closed = False
        self._error: Optional[BaseException] = None
        self._cond = asyncio.Condition()
        self._max_total_bytes = max_total_bytes

    async def push(self, data: bytes) -> None:
        async with self._cond:
            if self._closed:
                return
            self._chunks.append(bytes(data))
            self._total += len(data)
            if self._total > self._max_total_bytes:
                self._closed = True
                self._error = RuntimeError("transport buffer overflow")
            self._cond.notify_all()

    async def error(self, exc: BaseException) -> None:
        async with self._cond:
            self._closed = True
            self._error = exc
            self._cond.notify_all()

    async def end(self) -> None:
        async with self._cond:
            self._closed = True
            self._cond.notify_all()

    async def read_exactly(self, n: int) -> bytes:
        async with self._cond:
            while self._total < n:
                if self._closed:
                    raise self._error or RuntimeError("transport closed")
                await self._cond.wait()
            return self._consume_locked(n)

    def _consume_locked(self, n: int) -> bytes:
        if n == 0:
            return b""
        out = bytearray(n)
        pos = 0
        while pos < n:
            chunk = self._chunks[0]
            take = min(len(chunk), n - pos)
            out[pos:pos + take] = chunk[:take]
            pos += take
            self._total -= take
            if take == len(chunk):
                self._chunks.popleft()
            else:
                self._chunks[0] = chunk[take:]
        return bytes(out)


class WebSocketTransport:
    def __init__(self, ws: WebSocketClientProtocol) -> None:
        self._ws = ws
        self._buf = _ByteTransportBuffer()
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def _recv_loop(self) -> None:
        try:
            async for message in self._ws:
                if isinstance(message, str):
                    data = message.encode("utf-8")
                else:
                    data = bytes(message)
                await self._buf.push(data)
        except Exception as exc:
            await self._buf.error(exc)
        else:
            await self._buf.end()

    async def read(self, n: int) -> bytes:
        return await self._buf.read_exactly(n)

    async def write(self, data: bytes) -> None:
        await self._ws.send(data)

    async def close(self) -> None:
        self._recv_task.cancel()
        with contextlib.suppress(Exception):
            await self._ws.close()


# Yamux implementation (minimal client-side subset used by PokeTunnel)
FRAME_DATA = 0
FRAME_WINDOW_UPDATE = 1
FRAME_PING = 2
FRAME_GO_AWAY = 3

FLAG_SYN = 1
FLAG_ACK = 2
FLAG_FIN = 4
FLAG_RST = 8

GO_AWAY_NORMAL = 0
GO_AWAY_PROTOCOL_ERROR = 1
GO_AWAY_INTERNAL_ERROR = 2

MAX_STREAM_WINDOW = 256 * 1024
DEFAULT_CONFIG = {
    "accept_backlog": 256,
    "enable_keep_alive": True,
    "keep_alive_interval": 30.0,
    "connection_write_timeout": 10.0,
    "max_stream_window_size": MAX_STREAM_WINDOW,
    "max_incoming_streams": 1000,
}

STATE_INIT = 0
STATE_SYN_SENT = 1
STATE_SYN_RECEIVED = 2
STATE_ESTABLISHED = 3
STATE_LOCAL_CLOSE = 4
STATE_REMOTE_CLOSE = 5
STATE_CLOSED = 6
STATE_RESET = 7


class SessionShutdownError(Exception):
    pass


class StreamClosedError(Exception):
    pass


class StreamResetError(Exception):
    pass


class GoAwayError(Exception):
    pass


class _AsyncQueue:
    def __init__(self, maxsize: int) -> None:
        self._q: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    async def push(self, item: Any) -> None:
        if self._closed:
            raise RuntimeError("queue is closed")
        await self._q.put(item)

    def try_push(self, item: Any) -> bool:
        if self._closed or self._q.full():
            return False
        self._q.put_nowait(item)
        return True

    async def pop(self) -> Any:
        if self._closed and self._q.empty():
            raise RuntimeError("queue is closed")
        item = await self._q.get()
        return item

    def close(self) -> None:
        self._closed = True
        if self._q.empty():
            with contextlib.suppress(Exception):
                self._q.put_nowait(_QUEUE_CLOSED)


_QUEUE_CLOSED = object()


class YamuxStream:
    def __init__(self, stream_id: int, session: "YamuxSession", max_window: int, state: int = STATE_INIT) -> None:
        self.id = stream_id
        self.session = session
        self.state = state
        self.max_window = max_window
        self.recv_buf = bytearray()
        self.recv_window = max_window
        self.send_window = max_window
        self._read_cond = asyncio.Condition()
        self._send_cond = asyncio.Condition()

    async def read(self, n: int = 65536) -> bytes:
        while True:
            async with self._read_cond:
                if self.state == STATE_RESET:
                    raise StreamResetError()
                if self.recv_buf:
                    take = min(n, len(self.recv_buf))
                    data = bytes(self.recv_buf[:take])
                    del self.recv_buf[:take]
                else:
                    if self.state in (STATE_REMOTE_CLOSE, STATE_CLOSED):
                        return b""
                    if self.state not in (STATE_ESTABLISHED, STATE_LOCAL_CLOSE):
                        raise StreamClosedError()
                    await self._read_cond.wait()
                    continue
            await self._maybe_send_window_update()
            return data

    async def write(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            self._check_writable()
            async with self._send_cond:
                while self.send_window == 0:
                    self._check_writable()
                    try:
                        await asyncio.wait_for(
                            self._send_cond.wait(),
                            timeout=self.session.connection_write_timeout,
                        )
                    except asyncio.TimeoutError as exc:
                        raise StreamClosedError("write timeout: peer not sending window updates") from exc
                chunk_len = min(len(data) - offset, self.send_window)
                chunk = data[offset:offset + chunk_len]
                self.send_window -= chunk_len
            await self.session.send_frame(FRAME_DATA, 0, self.id, chunk_len, chunk)
            offset += chunk_len

    async def close(self) -> None:
        if self.state == STATE_ESTABLISHED:
            self.state = STATE_LOCAL_CLOSE
        elif self.state == STATE_REMOTE_CLOSE:
            self.state = STATE_CLOSED
        else:
            return
        await self.session.send_frame(FRAME_WINDOW_UPDATE, FLAG_FIN, self.id, 0)
        if self.state == STATE_CLOSED:
            self.session.remove_stream(self.id)
        async with self._read_cond:
            self._read_cond.notify_all()

    def reset(self) -> None:
        if self.state in (STATE_CLOSED, STATE_RESET):
            return
        self.state = STATE_RESET
        self.session.send_frame_no_wait(FRAME_WINDOW_UPDATE, FLAG_RST, self.id, 0)
        self.session.remove_stream(self.id)
        self._wake_all()

    def force_close(self) -> None:
        if self.state in (STATE_CLOSED, STATE_RESET):
            return
        self.state = STATE_REMOTE_CLOSE if self.recv_buf else STATE_RESET
        self._wake_all()

    def deliver_data(self, data: bytes) -> bool:
        if self.state in (STATE_REMOTE_CLOSE, STATE_CLOSED, STATE_RESET):
            return False
        if len(data) > self.recv_window:
            return False
        self.recv_buf.extend(data)
        self.recv_window -= len(data)
        asyncio.create_task(self._notify_readers())
        return True

    def process_flags(self, flags: int) -> None:
        if flags & FLAG_RST:
            self.state = STATE_RESET
            self.session.remove_stream(self.id)
            self._wake_all()
            return
        if flags & FLAG_FIN:
            if self.state in (STATE_ESTABLISHED, STATE_SYN_SENT, STATE_SYN_RECEIVED):
                self.state = STATE_REMOTE_CLOSE
            elif self.state == STATE_LOCAL_CLOSE:
                self.state = STATE_CLOSED
                self.session.remove_stream(self.id)
            asyncio.create_task(self._notify_readers())
        if flags & FLAG_ACK and self.state == STATE_SYN_SENT:
            self.state = STATE_ESTABLISHED

    def update_send_window(self, length: int) -> None:
        self.send_window = min(self.send_window + length, 0xFFFFFFFF)
        asyncio.create_task(self._notify_writers())

    def _check_writable(self) -> None:
        if self.state == STATE_RESET:
            raise StreamResetError()
        if self.state not in (STATE_ESTABLISHED, STATE_REMOTE_CLOSE):
            raise StreamClosedError()

    async def _maybe_send_window_update(self) -> None:
        delta = self.max_window - self.recv_window - len(self.recv_buf)
        if delta < self.max_window / 2:
            return
        self.recv_window += delta
        self.session.send_frame_no_wait(FRAME_WINDOW_UPDATE, 0, self.id, delta)

    async def _notify_readers(self) -> None:
        async with self._read_cond:
            self._read_cond.notify_all()

    async def _notify_writers(self) -> None:
        async with self._send_cond:
            self._send_cond.notify_all()

    def _wake_all(self) -> None:
        asyncio.create_task(self._notify_readers())
        asyncio.create_task(self._notify_writers())


class YamuxSession:
    def __init__(self, transport: WebSocketTransport, is_client: bool = True, **config: Any) -> None:
        self.transport = transport
        self.is_client = is_client
        merged = dict(DEFAULT_CONFIG)
        merged.update(config)
        self.config = merged
        self.connection_write_timeout = float(self.config["connection_write_timeout"])
        self.next_stream_id = 1 if is_client else 2
        self.streams: dict[int, YamuxStream] = {}
        self.accept_queue = _AsyncQueue(int(self.config["accept_backlog"]))
        self.closed = False
        self.remote_go_away = False
        self.shutdown_error: Optional[BaseException] = None
        self.highest_inbound_stream_id = 0
        self._write_lock = asyncio.Lock()
        self._keepalive_task: Optional[asyncio.Task[None]] = None
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._ping_id = 0
        self._pings: dict[int, asyncio.Future[int]] = {}
        if self.config["enable_keep_alive"]:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def open(self) -> YamuxStream:
        if self.closed:
            raise SessionShutdownError()
        if self.remote_go_away:
            raise GoAwayError("received goaway")
        stream_id = self.next_stream_id
        self.next_stream_id += 2
        stream = YamuxStream(stream_id, self, int(self.config["max_stream_window_size"]))
        self.streams[stream_id] = stream
        try:
            await self.send_frame(FRAME_WINDOW_UPDATE, FLAG_SYN, stream_id, 0)
        except Exception:
            stream.force_close()
            self.streams.pop(stream_id, None)
            raise
        stream.state = STATE_ESTABLISHED
        return stream

    async def accept(self) -> YamuxStream:
        if self.closed:
            raise SessionShutdownError()
        item = await self.accept_queue.pop()
        if item is _QUEUE_CLOSED:
            raise SessionShutdownError()
        stream: YamuxStream = item
        try:
            await self.send_frame(FRAME_WINDOW_UPDATE, FLAG_ACK, stream.id, 0)
        except Exception:
            stream.force_close()
            self.streams.pop(stream.id, None)
            raise
        stream.state = STATE_ESTABLISHED
        return stream

    async def ping(self) -> float:
        if self.closed:
            raise SessionShutdownError()
        ping_id = self._ping_id
        self._ping_id += 1
        fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._pings[ping_id] = fut
        started = time.monotonic()
        await self.send_frame(FRAME_PING, FLAG_SYN, 0, ping_id)
        await asyncio.wait_for(fut, timeout=self.connection_write_timeout)
        return time.monotonic() - started

    async def close(self) -> None:
        if self.closed:
            return
        if self._keepalive_task:
            self._keepalive_task.cancel()
        with contextlib.suppress(Exception):
            await self.send_frame(FRAME_GO_AWAY, 0, 0, GO_AWAY_NORMAL)
        self.closed = True
        self.shutdown_error = SessionShutdownError()
        for stream in list(self.streams.values()):
            stream.force_close()
        self.streams.clear()
        self.accept_queue.close()
        for fut in self._pings.values():
            if not fut.done():
                fut.set_exception(SessionShutdownError())
        self._pings.clear()
        with contextlib.suppress(Exception):
            await self.transport.close()
        with contextlib.suppress(Exception):
            await self._recv_task

    def is_closed(self) -> bool:
        return self.closed

    async def send_frame(
        self,
        frame_type: int,
        flags: int,
        stream_id: int,
        length: int,
        payload: bytes = b"",
    ) -> None:
        if self.closed and self.shutdown_error:
            raise self.shutdown_error
        header = struct.pack("!BBHII", 0, frame_type, flags, stream_id, length)
        frame = header + payload
        async with self._write_lock:
            await self.transport.write(frame)

    def send_frame_no_wait(
        self,
        frame_type: int,
        flags: int,
        stream_id: int,
        length: int,
        payload: bytes = b"",
    ) -> None:
        asyncio.create_task(self.send_frame(frame_type, flags, stream_id, length, payload))

    def remove_stream(self, stream_id: int) -> None:
        self.streams.pop(stream_id, None)

    async def _keepalive_loop(self) -> None:
        try:
            while not self.closed:
                await asyncio.sleep(float(self.config["keep_alive_interval"]))
                await self.ping()
        except Exception as exc:
            self._close_with_error(RuntimeError("keepalive timeout") if isinstance(exc, asyncio.TimeoutError) else exc)

    async def _recv_loop(self) -> None:
        try:
            while not self.closed:
                header = await self.transport.read(12)
                version, frame_type, flags, stream_id, length = struct.unpack("!BBHII", header)
                if version != 0:
                    await self._go_away(GO_AWAY_PROTOCOL_ERROR)
                    return
                if frame_type == FRAME_PING:
                    self._handle_ping(flags, length)
                elif frame_type == FRAME_GO_AWAY:
                    self._handle_go_away(length)
                elif frame_type in (FRAME_DATA, FRAME_WINDOW_UPDATE):
                    await self._handle_stream_message(frame_type, flags, stream_id, length)
                else:
                    await self._go_away(GO_AWAY_PROTOCOL_ERROR)
                    return
        except Exception as exc:
            if not self.closed:
                self._close_with_error(exc)

    def _handle_ping(self, flags: int, length: int) -> None:
        if flags & FLAG_SYN:
            self.send_frame_no_wait(FRAME_PING, FLAG_ACK, 0, length)
            return
        if flags & FLAG_ACK:
            fut = self._pings.pop(length, None)
            if fut and not fut.done():
                fut.set_result(length)

    def _handle_go_away(self, code: int) -> None:
        self.remote_go_away = True
        self.accept_queue.close()
        if code != GO_AWAY_NORMAL:
            self._close_with_error(GoAwayError(f"received goaway: code {code}"))

    async def _handle_stream_message(self, frame_type: int, flags: int, stream_id: int, length: int) -> None:
        if frame_type == FRAME_DATA and length > int(self.config["max_stream_window_size"]):
            await self._go_away(GO_AWAY_PROTOCOL_ERROR)
            return
        if flags & FLAG_SYN:
            self._incoming_stream(stream_id)
        stream = self.streams.get(stream_id)
        if not stream:
            if frame_type == FRAME_DATA and length > 0:
                await self.transport.read(length)
            if not (flags & FLAG_RST):
                self.send_frame_no_wait(FRAME_WINDOW_UPDATE, FLAG_RST, stream_id, 0)
            return
        remaining_flags = flags & ~FLAG_SYN
        if frame_type == FRAME_DATA:
            if length > 0:
                payload = await self.transport.read(length)
                if not stream.deliver_data(payload):
                    stream.reset()
                    return
            if remaining_flags:
                stream.process_flags(remaining_flags)
        elif frame_type == FRAME_WINDOW_UPDATE:
            if length > 0:
                stream.update_send_window(length)
            if remaining_flags:
                stream.process_flags(remaining_flags)

    def _incoming_stream(self, stream_id: int) -> None:
        if stream_id == 0:
            self.send_frame_no_wait(FRAME_WINDOW_UPDATE, FLAG_RST, stream_id, 0)
            return
        if stream_id in self.streams:
            return
        should_be_remote = self.is_client
        if ((stream_id % 2 == 0) != should_be_remote):
            self.send_frame_no_wait(FRAME_WINDOW_UPDATE, FLAG_RST, stream_id, 0)
            return
        if stream_id <= self.highest_inbound_stream_id:
            self.send_frame_no_wait(FRAME_WINDOW_UPDATE, FLAG_RST, stream_id, 0)
            return
        self.highest_inbound_stream_id = stream_id
        if len(self.streams) >= int(self.config["max_incoming_streams"]):
            self.send_frame_no_wait(FRAME_WINDOW_UPDATE, FLAG_RST, stream_id, 0)
            return
        stream = YamuxStream(stream_id, self, int(self.config["max_stream_window_size"]), STATE_SYN_RECEIVED)
        self.streams[stream_id] = stream
        if not self.accept_queue.try_push(stream):
            self.streams.pop(stream_id, None)
            self.send_frame_no_wait(FRAME_WINDOW_UPDATE, FLAG_RST, stream_id, 0)

    async def _go_away(self, code: int) -> None:
        with contextlib.suppress(Exception):
            await self.send_frame(FRAME_GO_AWAY, 0, 0, code)
        self._close_with_error(RuntimeError(f"protocol error: goaway {code}"))

    def _close_with_error(self, exc: BaseException) -> None:
        if self.closed:
            return
        self.closed = True
        self.shutdown_error = exc
        if self._keepalive_task:
            self._keepalive_task.cancel()
        for stream in list(self.streams.values()):
            stream.force_close()
        self.streams.clear()
        self.accept_queue.close()
        for fut in self._pings.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pings.clear()
        asyncio.create_task(self.transport.close())


_HEADER_LIMIT = 64 * 1024
_BODY_LIMIT = 64 * 1024 * 1024


def _find_header_end(buf: bytes) -> int:
    return buf.find(b"\r\n\r\n")


def _sanitize_header_value(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ")


async def _read_headers(stream: YamuxStream) -> tuple[bytes, bytes]:
    data = bytearray()
    while len(data) < _HEADER_LIMIT:
        chunk = await stream.read(4096)
        if not chunk:
            raise RuntimeError("stream closed before headers complete")
        data.extend(chunk)
        idx = _find_header_end(data)
        if idx >= 0:
            end = idx + 4
            return bytes(data[:end]), bytes(data[end:])
    raise RuntimeError("headers too large")


def _parse_request(header_bytes: bytes) -> tuple[str, str, str, dict[str, str]]:
    text = header_bytes.decode("latin-1")
    lines = text.split("\r\n")
    if not lines or not lines[0]:
        raise RuntimeError("empty request")
    request_line = lines[0]
    parts = request_line.split(" ")
    if len(parts) < 3:
        raise RuntimeError("invalid request line")
    method, path, http_version = parts[0], parts[1], parts[2]
    if not path.startswith("/"):
        raise RuntimeError("invalid request path")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return method, path, http_version, headers


async def _read_request_body(stream: YamuxStream, headers: dict[str, str], remainder: bytes) -> Optional[bytes]:
    content_length = headers.get("content-length")
    if content_length:
        try:
            length = int(content_length)
        except ValueError:
            return None
        if length <= 0:
            return None
        if length > _BODY_LIMIT:
            raise RuntimeError(f"body too large: {length} bytes")
        body = bytearray(remainder[:length])
        while len(body) < length:
            body.extend(await stream.read(min(32768, length - len(body))))
        return bytes(body)

    transfer_encoding = headers.get("transfer-encoding", "")
    if "chunked" in transfer_encoding.lower():
        data = bytearray(remainder)
        chunks: list[bytes] = []
        total = 0
        while True:
            while b"\r\n" not in data:
                if len(data) > _HEADER_LIMIT:
                    raise RuntimeError("chunked size line too large")
                data.extend(await stream.read(4096))
            line_end = data.find(b"\r\n")
            chunk_size = int(data[:line_end].decode("ascii").strip(), 16)
            del data[:line_end + 2]
            if chunk_size <= 0:
                break
            total += chunk_size
            if total > _BODY_LIMIT:
                raise RuntimeError(f"chunked body too large: {total} bytes")
            while len(data) < chunk_size + 2:
                data.extend(await stream.read(max(4096, chunk_size + 2 - len(data))))
            chunks.append(bytes(data[:chunk_size]))
            del data[:chunk_size + 2]
        return b"".join(chunks)

    return remainder or None


async def _proxy_to_local(stream: YamuxStream, local_addr: str) -> None:
    reader = writer = None
    try:
        header_bytes, remainder = await _read_headers(stream)
        method, path, http_version, headers = _parse_request(header_bytes)
        body = await _read_request_body(stream, headers, remainder)

        host, port = (local_addr.split(":", 1) + ["80"])[:2]
        port_num = int(port)

        hop_by_hop = {
            "host",
            "transfer-encoding",
            "connection",
            "keep-alive",
            "te",
            "trailer",
            "upgrade",
        }
        forwarded_headers = {
            k: v for k, v in headers.items() if k not in hop_by_hop
        }
        forwarded_headers["host"] = f"{host}:{port_num}"
        forwarded_headers["connection"] = "close"
        if body is not None and body and "content-length" not in forwarded_headers:
            forwarded_headers["content-length"] = str(len(body))

        request_lines = [f"{method} {path} {http_version}\r\n"]
        for key, value in forwarded_headers.items():
            request_lines.append(f"{_sanitize_header_value(key)}: {_sanitize_header_value(value)}\r\n")
        request_lines.append("\r\n")
        outgoing = "".join(request_lines).encode("latin-1") + (body or b"")

        reader, writer = await asyncio.open_connection(host, port_num)
        writer.write(outgoing)
        await writer.drain()

        response = bytearray()
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > _BODY_LIMIT + _HEADER_LIMIT:
                raise RuntimeError("response body exceeds size limit")

        await stream.write(bytes(response))
    except Exception:
        bad_gateway = (
            b"HTTP/1.1 502 Bad Gateway\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: 11\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"Bad Gateway"
        )
        with contextlib.suppress(Exception):
            await stream.write(bad_gateway)
    finally:
        with contextlib.suppress(Exception):
            await stream.close()
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


RETRY_BASE_MS = 100
RETRY_MAX_MS = 15000
RETRY_JITTER = 0.3
NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 405, 410}


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, PikoAuthError):
        return False
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status not in NON_RETRYABLE_STATUS_CODES
    if isinstance(exc, Exception):
        msg = str(exc).lower()
        if "401" in msg or "403" in msg or "unauthorized" in msg or "forbidden" in msg:
            return False
    return True



def _retry_delay_ms(attempt: int) -> int:
    base = min(RETRY_BASE_MS * (2 ** attempt), RETRY_MAX_MS)
    jitter = base * RETRY_JITTER * ((random.random() * 2) - 1)
    return max(RETRY_BASE_MS, round(base + jitter))


class _PikoClient(_EventEmitter):
    def __init__(self, *, upstream_url: str, endpoint_id: str, token: str, local_addr: str) -> None:
        super().__init__()
        self.upstream_url = upstream_url
        self.endpoint_id = endpoint_id
        self.token = token
        self.local_addr = local_addr
        self.session: Optional[YamuxSession] = None
        self.running = False
        self._connected = False
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._stop_event.clear()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self.running = False
        self._stop_event.set()
        if self.session is not None:
            with contextlib.suppress(Exception):
                await self.session.close()
            self.session = None
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(Exception):
                await self._loop_task
        self._set_connected(False)

    async def _run_loop(self) -> None:
        attempt = 0
        while self.running and not self._stop_event.is_set():
            try:
                await self._connect_and_serve()
                attempt = 0
            except Exception as exc:
                self._set_connected(False)
                self.session = None
                if not self.running or self._stop_event.is_set():
                    return
                if not _should_retry(exc):
                    self.emit("error", exc)
                    self.running = False
                    return
                attempt += 1
                self.emit("disconnected")
                await asyncio.sleep(_retry_delay_ms(attempt) / 1000.0)

    async def _connect_and_serve(self) -> None:
        ws_url = (
            self.upstream_url.rstrip("/")
            .replace("https://", "wss://")
            .replace("http://", "ws://")
            + f"/piko/v1/upstream/{self.endpoint_id}"
        )
        try:
            ws = await websockets.connect(
                ws_url,
                additional_headers={"Authorization": f"Bearer {self.token}"},
                ping_interval=None,
                close_timeout=5,
                max_size=None,
            )
        except websockets.InvalidStatus as exc:
            if exc.status_code in (401, 403):
                raise PikoAuthError(f"Authentication failed: HTTP {exc.status_code}") from exc
            raise PikoConnectionError(
                f"Unexpected HTTP {exc.status_code} from upstream",
                status_code=exc.status_code,
            ) from exc
        except Exception as exc:
            raise PikoConnectionError(str(exc)) from exc

        transport = WebSocketTransport(ws)
        session = YamuxSession(transport, is_client=True, enable_keep_alive=True, keep_alive_interval=30.0)
        self.session = session
        self._set_connected(True)
        self.emit("connected")

        try:
            while self.running and not session.is_closed():
                stream = await session.accept()
                asyncio.create_task(_proxy_to_local(stream, self.local_addr))
        finally:
            if not session.is_closed():
                with contextlib.suppress(Exception):
                    await session.close()

    def _set_connected(self, value: bool) -> None:
        self._connected = value


class PokeTunnel(_EventEmitter):
    def __init__(self, options: TunnelOptions | dict[str, Any]) -> None:
        super().__init__()
        self.options = options if isinstance(options, TunnelOptions) else TunnelOptions(**options)
        parsed = urlparse(self.options.url)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError(f"Invalid URL: {self.options.url}")
        self._local_url = parsed
        self._piko_client: Optional[_PikoClient] = None
        self._sync_task: Optional[asyncio.Task[None]] = None
        self._connection_id: Optional[str] = None
        self._tunnel_url: Optional[str] = None
        self._connected = False

    @property
    def info(self) -> Optional[TunnelInfo]:
        if not self._connection_id or not self._tunnel_url:
            return None
        return TunnelInfo(
            connection_id=self._connection_id,
            tunnel_url=self._tunnel_url,
            local_url=self.options.url,
            name=self.options.name,
        )

    @property
    def connected(self) -> bool:
        return self._connected

    def _resolve_token(self) -> str:
        token = self.options.token or get_token()
        if not token:
            raise PokeAuthError("Not logged in. Run 'poke login'.")
        return token

    async def _fetch_auth(self, *, path: str, options: Optional[dict[str, Any]] = None) -> httpx.Response:
        return await fetch_with_auth(
            path=path,
            options=options,
            token=self._resolve_token(),
            base_url=self.options.base_url,
        )

    async def start(self) -> TunnelInfo:
        payload: dict[str, Any] = {
            "name": self.options.name,
            "serverUrl": self.options.url,
            "tunnel": True,
        }
        if self.options.client_id:
            payload["clientId"] = self.options.client_id
        if self.options.client_secret:
            payload["clientSecret"] = self.options.client_secret

        response = await self._fetch_auth(
            path="/mcp/connections/cli",
            options={
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "content": json.dumps(payload),
            },
        )
        if not response.is_success:
            message = f"HTTP {response.status_code}"
            try:
                message = response.json().get("message") or message
            except Exception:
                pass
            raise RuntimeError(f"Failed to create tunnel: {message}")

        data = response.json()
        self._connection_id = data.get("id")
        self._tunnel_url = data.get("serverUrl")
        if not self._connection_id or not self._tunnel_url:
            raise RuntimeError("Server did not return a valid connection ID or tunnel URL.")
        if not data.get("tunnel", {}).get("token") or not data.get("tunnel", {}).get("upstreamUrl"):
            raise RuntimeError("Tunnel configuration not available.")

        local_host = self._local_url.hostname or "127.0.0.1"
        local_port = self._local_url.port or (443 if self._local_url.scheme == "https" else 80)
        self._piko_client = _PikoClient(
            upstream_url=data["tunnel"]["upstreamUrl"],
            endpoint_id=data["id"],
            token=data["tunnel"]["token"],
            local_addr=f"{local_host}:{local_port}",
        )
        self._piko_client.on("error", lambda err: self.emit("error", err if isinstance(err, Exception) else Exception(str(err))))
        self._piko_client.on("disconnected", self._handle_disconnected)

        connected_fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def on_connected() -> None:
            if not connected_fut.done():
                connected_fut.set_result(None)

        def on_error(err: Exception) -> None:
            if not connected_fut.done():
                connected_fut.set_exception(err)

        self._piko_client.on("connected", on_connected)
        self._piko_client.on("error", on_error)

        try:
            await self._piko_client.start()
            await asyncio.wait_for(connected_fut, timeout=30.0)
        finally:
            self._piko_client.off("connected", on_connected)
            self._piko_client.off("error", on_error)

        self._connected = True
        await self._activate_tunnel()

        if self.options.sync_interval_ms > 0:
            self._sync_task = asyncio.create_task(self._sync_loop())

        info = self.info
        if info is None:
            raise RuntimeError("Tunnel connected but failed to retrieve connection info.")

        self.emit("connected", info)
        return info

    async def stop(self) -> None:
        if self._sync_task is not None:
            self._sync_task.cancel()
            with contextlib.suppress(Exception):
                await self._sync_task
            self._sync_task = None

        if self.options.cleanup_on_stop and self._connection_id:
            with contextlib.suppress(Exception):
                await self._fetch_auth(
                    path=f"/mcp/connections/{self._connection_id}",
                    options={"method": "DELETE"},
                )

        if self._piko_client is not None:
            with contextlib.suppress(Exception):
                await self._piko_client.stop()
            self._piko_client = None

        self._connected = False
        self._connection_id = None
        self._tunnel_url = None

    async def create_recipe(self, *, name: Optional[str] = None) -> str:
        if not self._connection_id:
            raise RuntimeError("Tunnel is not started.")
        response = await self._fetch_auth(
            path=f"/mcp/connections/{self._connection_id}/create-recipe",
            options={
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "content": json.dumps({"name": name or self.options.name}),
            },
        )
        if not response.is_success:
            raise RuntimeError(f"Failed to create recipe (HTTP {response.status_code}).")
        return response.json()["link"]

    async def _activate_tunnel(self) -> None:
        if not self._connection_id:
            raise RuntimeError("Tunnel is not started.")
        response = await self._fetch_auth(
            path=f"/mcp/connections/{self._connection_id}/activate-tunnel",
            options={"method": "POST"},
        )
        if response.is_success:
            payload = response.json()
            if payload.get("status") == "oauth_required" and payload.get("authUrl"):
                self.emit("oauthRequired", {"authUrl": payload["authUrl"]})
        else:
            await self.sync_tools()

    async def sync_tools(self) -> None:
        if not self._connection_id:
            return
        try:
            response = await self._fetch_auth(
                path=f"/mcp/connections/{self._connection_id}/sync-tools",
                options={"method": "POST"},
            )
            if response.is_success:
                payload = response.json()
                if payload.get("requiresOAuth") and payload.get("oauthUrl"):
                    self.emit("oauthRequired", {"authUrl": payload["oauthUrl"]})
                else:
                    tools = payload.get("tools")
                    tool_count = len(tools) if isinstance(tools, list) else 0
                    self.emit("toolsSynced", {"toolCount": tool_count})
        except Exception:
            pass

    async def _sync_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.options.sync_interval_ms / 1000.0)
                await self.sync_tools()
        except asyncio.CancelledError:
            pass

    def _handle_disconnected(self) -> None:
        self._connected = False
        self.emit("disconnected")


__all__ = [
    "PokeAuthError",
    "LoginOptions",
    "LoginResult",
    "TunnelOptions",
    "TunnelInfo",
    "fetch_with_auth",
    "login",
    "logout",
    "is_logged_in",
    "get_token",
    "PokeTunnel",
]
