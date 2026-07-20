"""Unified message bus — routes messages between channels and the system."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine, Optional

from loguru import logger

from opc.core.models import SystemMessage, UserMessage


InboundHandler = Callable[[UserMessage], Coroutine[Any, Any, Optional[SystemMessage]]]


class MessageBus:
    """Async message bus for routing between interaction channels and the OPC engine.

    Channels publish inbound messages; the engine processes them and publishes outbound responses.
    """

    def __init__(self) -> None:
        self._inbound_queue: asyncio.Queue[UserMessage] | None = None
        self._outbound_queue: asyncio.Queue[SystemMessage] | None = None
        self._inbound_handler: InboundHandler | None = None
        self._subscribers: dict[str, list[asyncio.Future]] = {}
        self._running = False

    def publish_event(self, topic: str, payload: Any = None) -> None:
        """Publish a reactive event to all registered topic futures."""
        futures = self._subscribers.pop(topic, [])
        for fut in futures:
            if not fut.done():
                fut.set_result(payload)

    async def subscribe_once(self, topic: str, timeout: float | None = None) -> Any:
        """Reactive await for a single event on a given topic (no polling)."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._subscribers.setdefault(topic, []).append(fut)
        try:
            if timeout is not None:
                return await asyncio.wait_for(fut, timeout=timeout)
            return await fut
        except Exception:
            if topic in self._subscribers and fut in self._subscribers[topic]:
                self._subscribers[topic].remove(fut)
            raise

    def _inbound(self) -> asyncio.Queue[UserMessage]:
        if self._inbound_queue is None:
            self._inbound_queue = asyncio.Queue()
        return self._inbound_queue

    def _outbound(self) -> asyncio.Queue[SystemMessage]:
        if self._outbound_queue is None:
            self._outbound_queue = asyncio.Queue()
        return self._outbound_queue

    def set_handler(self, handler: InboundHandler) -> None:
        self._inbound_handler = handler

    async def publish_inbound(self, message: UserMessage) -> None:
        await self._inbound().put(message)

    async def publish_outbound(self, message: SystemMessage) -> None:
        await self._outbound().put(message)

    async def get_response(self, timeout: float = 600.0) -> SystemMessage | None:
        try:
            return await asyncio.wait_for(self._outbound().get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def start(self) -> None:
        """Start processing inbound messages."""
        self._running = True
        while self._running:
            try:
                msg = await asyncio.wait_for(self._inbound().get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if self._inbound_handler:
                try:
                    response = await self._inbound_handler(msg)
                    if response:
                        await self._outbound().put(response)
                except Exception as e:
                    logger.error(f"Message handler error: {e}")
                    await self._outbound().put(SystemMessage(
                        channel=msg.channel,
                        user_id=msg.user_id,
                        session_id=msg.session_id,
                        content=f"Error processing message: {e}",
                        message_type="reply",
                    ))

    def stop(self) -> None:
        self._running = False

    async def process_single(self, message: UserMessage) -> SystemMessage | None:
        """Process a single message synchronously (for CLI use)."""
        if self._inbound_handler:
            return await self._inbound_handler(message)
        return None
