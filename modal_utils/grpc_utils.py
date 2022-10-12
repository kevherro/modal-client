import asyncio
import contextlib
import socket
import time
import urllib.parse
import uuid
from typing import Any, AsyncIterator, Dict, Optional, TypeVar

import grpclib.events
from grpclib import GRPCError, Status
from grpclib.client import Channel, Stream, UnaryStreamMethod
from grpclib.const import Cardinality
from grpclib.exceptions import StreamTerminatedError
from sentry_sdk import add_breadcrumb, capture_exception

from modal_proto import api_pb2

from .async_utils import TaskContext, synchronizer
from .logger import logger


def auth_metadata(client_type: api_pb2.ClientType, credentials) -> Dict[str, str]:
    if credentials and (client_type == api_pb2.CLIENT_TYPE_CLIENT or client_type == api_pb2.CLIENT_TYPE_WEB_SERVER):
        token_id, token_secret = credentials
        return {
            "x-modal-token-id": token_id,
            "x-modal-token-secret": token_secret,
        }
    elif credentials and client_type == api_pb2.CLIENT_TYPE_CONTAINER:
        task_id, task_secret = credentials
        return {
            "x-modal-task-id": task_id,
            "x-modal-task-secret": task_secret,
        }
    else:
        return {}


_SendType = TypeVar("_SendType")
_RecvType = TypeVar("_RecvType")

RETRYABLE_GRPC_STATUS_CODES = [
    Status.DEADLINE_EXCEEDED,
    Status.UNAVAILABLE,
    Status.INTERNAL,
]


class ChannelFactory:
    """Manages gRPC connection with the server. This factory is used by the channel pool."""

    def __init__(
        self, server_url: str, client_type: api_pb2.ClientType = None, credentials=None, inject_tracing_context=None
    ) -> None:
        try:
            o = urllib.parse.urlparse(server_url)
        except Exception:
            logger.exception(f"failed to parse server url: {server_url}")
            raise

        # Determine if the url is a socket.
        self.scheme = o.scheme
        self.is_socket = self.scheme == "unix"
        if self.is_socket:
            self.target = o.path
        else:
            self.target = o.netloc
            self.is_tls = o.scheme.endswith("s")

        self.inject_tracing_context = inject_tracing_context
        self.metadata = auth_metadata(client_type, credentials)
        logger.debug(f"Connecting to {self.target} using scheme {o.scheme}")

    def create(self) -> Channel:
        if self.is_socket:
            channel = Channel(path=self.target)
        else:
            parts = self.target.split(":")
            assert len(parts) <= 2, "Invalid target location: " + self.target
            channel = Channel(
                host=parts[0],
                port=parts[1] if len(parts) == 2 else 443 if self.is_tls else 80,
                ssl=self.is_tls,
            )

        # Inject metadata for the client.
        async def send_request(event: grpclib.events.SendRequest) -> None:
            for k, v in self.metadata.items():
                event.metadata[k] = v

            if self.inject_tracing_context is not None:
                self.inject_tracing_context(event.metadata)

        grpclib.events.listen(channel, grpclib.events.SendRequest, send_request)
        return channel


class ChannelStruct:
    def __init__(self, channel: Channel) -> None:
        self.channel = channel
        self.n_concurrent_requests = 0
        self.created_at = time.time()
        self.last_active = self.created_at


class ChannelPool:
    """Use multiple channels under the hood. A drop-in replacement for the GRPC channel.

    The ALB in AWS limits the number of streams per connection to 128.
    This is super annoying and means we can't put every request on the same channel.
    As a dumb workaround, we use a pool of channels.

    This object is not thread-safe.
    """

    # How long to keep alive unused channels in the pool, before closing them.
    CHANNEL_KEEP_ALIVE = 40

    # Maximum number of concurrent requests per channel.
    MAX_REQUESTS_PER_CHANNEL = 64

    # Don't accept more connections on this channel after this many seconds
    MAX_CHANNEL_LIFETIME = 30

    def __init__(self, task_context: TaskContext, channel_factory: ChannelFactory) -> None:
        # Only used by start()
        self._task_context = task_context

        # Threadsafe because it is read-only
        self._channel_factory = channel_factory

        # Protects the channels list below
        self._lock = asyncio.Lock()
        self._channels: list[ChannelStruct] = []

    async def _purge_channels(self):
        to_close: list[ChannelStruct] = []
        async with self._lock:
            for ch in self._channels:
                now = time.time()
                inactive_time = now - ch.last_active
                if ch.n_concurrent_requests > 0:
                    ch.last_active = now
                elif inactive_time >= self.CHANNEL_KEEP_ALIVE:
                    logger.debug(f"Closing channel of age {now - ch.created_at}s, inactive for {inactive_time}s")
                    to_close.append(ch)
            for ch in to_close:
                self._channels.remove(ch)
        for ch in to_close:
            ch.channel.close()

    async def start(self) -> None:
        self._task_context.infinite_loop(self._purge_channels, sleep=10.0)

    async def _get_channel(self) -> ChannelStruct:
        async with self._lock:
            eligible_channels = [
                ch
                for ch in self._channels
                if ch.n_concurrent_requests < self.MAX_REQUESTS_PER_CHANNEL
                and time.time() - ch.created_at < self.MAX_CHANNEL_LIFETIME
            ]
            if eligible_channels:
                ch = eligible_channels[0]
            else:
                channel = self._channel_factory.create()
                ch = ChannelStruct(channel)
                self._channels.append(ch)
                n_conc_reqs = [ch.n_concurrent_requests for ch in self._channels]
                n_conc_reqs_str = ", ".join(str(z) for z in n_conc_reqs)
                logger.debug(f"Pool: Added new channel (concurrent requests: {n_conc_reqs_str}")

        return ch

    def close(self) -> None:
        logger.debug("Pool: Shutting down")
        for ch in self._channels:
            ch.channel.close()
        self._channels = []

    def size(self) -> int:
        return len(self._channels)

    @synchronizer.asynccontextmanager
    async def request(
        self, name: str, cardinality: Cardinality, request_type, reply_type, timeout, metadata
    ) -> AsyncIterator[Stream]:
        ch = await self._get_channel()
        ch.n_concurrent_requests += 1
        try:
            async with ch.channel.request(
                name, cardinality, request_type, reply_type, timeout=timeout, metadata=metadata
            ) as stream:
                yield stream
        except GRPCError:
            channel_age = time.time() - ch.created_at
            add_breadcrumb(
                message=f"Error calling {name} on channel of age {channel_age:.4f}s",
                level="warning",
            )
            raise
        finally:
            ch.n_concurrent_requests -= 1


async def unary_stream(
    method: UnaryStreamMethod[_SendType, _RecvType],
    request: _SendType,
    metadata: Optional[Any] = None,
) -> AsyncIterator[_RecvType]:
    """Helper for making a unary-streaming gRPC request."""
    async with method.open(metadata=metadata) as stream:
        await stream.send_message(request, end=True)
        async for item in stream:
            yield item


async def retry_transient_errors(
    fn,
    *args,
    base_delay: float = 0.1,
    max_delay: float = 1,
    delay_factor: float = 2,
    max_retries: int = 3,
    additional_status_codes: list = [],
    ignore_errors: list = [],
    attempt_timeout: Optional[float] = None,  # timeout for each attempt
    total_timeout: Optional[float] = None,  # timeout for the entire function call
    attempt_timeout_floor=2.0,  # always have at least this much timeout (only for total_timeout)
):
    """Retry on transient gRPC failures with back-off until max_retries is reached.
    If max_retries is None, retry forever."""

    delay = base_delay
    n_retries = 0

    status_codes = [*RETRYABLE_GRPC_STATUS_CODES, *additional_status_codes]

    idempotency_key = str(uuid.uuid4())

    if total_timeout is not None:
        total_deadline = time.time() + total_timeout
    else:
        total_deadline = None

    while True:
        metadata = [("x-idempotency-key", idempotency_key), ("x-retry-attempt", str(n_retries))]
        timeouts = []
        if attempt_timeout is not None:
            timeouts.append(attempt_timeout)
        if total_timeout is not None:
            timeouts.append(max(total_deadline - time.time(), attempt_timeout_floor))
        if timeouts:
            timeout = min(timeouts)  # In case the function provided both types of timeouts
        else:
            timeout = None
        try:
            return await fn(*args, metadata=metadata, timeout=timeout)
        except (StreamTerminatedError, GRPCError, socket.gaierror) as exc:
            if isinstance(exc, GRPCError) and exc.status not in status_codes:
                raise exc

            if max_retries is not None and n_retries >= max_retries:
                raise exc

            if total_deadline and time.time() + delay + attempt_timeout_floor >= total_deadline:
                # no point sleeping if that's going to push us past the deadline
                raise exc

            n_retries += 1
            if not (isinstance(exc, GRPCError) and exc.status in ignore_errors):
                capture_exception(exc)

            await asyncio.sleep(delay)
            delay = min(delay * delay_factor, max_delay)


def find_free_port() -> int:
    """Find a free TCP port, useful for testing."""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def patch_mock_servicer(cls):
    """Patches all unimplemented abstract methods in a mock servicer."""

    async def fallback(self, stream) -> None:
        raise GRPCError(Status.UNIMPLEMENTED, "Not implemented in mock servicer " + repr(cls))

    # Fill in the remaining methods on the class
    for name in dir(cls):
        method = getattr(cls, name)
        if getattr(method, "__isabstractmethod__", False):
            setattr(cls, name, fallback)

    cls.__abstractmethods__ = frozenset()
    return cls
