"""Channel manager for OpenOPC."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from opc.channels.base import BaseChannel
from opc.channels.provider_registry import PROVIDER_SPECS, ordered_provider_specs
from opc.core.config import OPCConfig
from opc.core.models import SystemMessage
from opc.layer0_interaction.message_bus import MessageBus


class ChannelManager:
    def __init__(self, config: OPCConfig, bus: MessageBus):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self.channel_errors: dict[str, str] = {}
        self._dispatch_task: asyncio.Task[Any] | None = None
        self._init_channels()

    def _init_channels(self) -> None:
        for spec in ordered_provider_specs():
            cfg = getattr(self.config.channels, spec.name)
            if not cfg.enabled:
                continue
            channel = self._build_channel(spec.name, cfg, spec.module_name, spec.class_name)
            if channel is None:
                continue
            self.channels[spec.name] = channel
            logger.info("Channel configured: {}", spec.name)

    def _build_channel(self, key: str, cfg: Any, module_name: str, class_name: str) -> BaseChannel | None:
        try:
            module = __import__(module_name, fromlist=[class_name])
            cls = getattr(module, class_name)
            return cls(cfg, self.bus)
        except Exception as e:
            logger.warning("Channel {} not available: {}", key, e)
            self.channel_errors[key] = str(e)
            return None

    @property
    def enabled_channels(self) -> list[str]:
        return list(self.channels.keys())

    def get_channel(self, name: str) -> BaseChannel | None:
        return self.channels.get(name)

    def get_status(self, name: str) -> dict[str, Any]:
        spec = PROVIDER_SPECS[name]
        cfg = getattr(self.config.channels, name)
        enabled = bool(getattr(cfg, "enabled", False))
        channel = self.channels.get(name)
        capability = channel.describe_capability() if channel is not None else {
            "name": name,
            "delivery_mode": spec.delivery_mode,
            "available": False if spec.required_package and name in self.channel_errors else True,
            "configured": False,
            "missing_config": list(spec.required_config_fields),
            "running": False,
            "ready": False,
            "last_error": self.channel_errors.get(name, ""),
        }
        return {
            "name": name,
            "enabled": enabled,
            "delivery_mode": capability.get("delivery_mode", spec.delivery_mode),
            "available": capability.get("available", True),
            "configured": capability.get("configured", False),
            "ready": capability.get("ready", False),
            "running": capability.get("running", False),
            "missing_config": capability.get("missing_config", []),
            "last_error": capability.get("last_error", "") or self.channel_errors.get(name, ""),
            "bridge_required": spec.bridge_required,
            "extra_name": spec.extra_name,
            "required_package": spec.required_package,
        }

    def get_all_statuses(self) -> list[dict[str, Any]]:
        return [self.get_status(spec.name) for spec in ordered_provider_specs()]

    async def start_all(self) -> None:
        for name, channel in self.channels.items():
            try:
                await channel.start()
            except Exception as e:
                logger.warning("Failed to start channel {}: {}", name, e)
                channel.set_last_error(e)
        if self._dispatch_task is None:
            self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

    async def stop_all(self) -> None:
        if self._dispatch_task:
            self._dispatch_task.cancel()
            self._dispatch_task = None
        for channel in self.channels.values():
            try:
                await channel.stop()
            except Exception:
                pass

    async def _dispatch_outbound(self) -> None:
        while True:
            message = await self.bus.get_response(timeout=1.0)
            if message is None:
                continue
            # This loop is the only outbound consumer for every channel, so
            # neither a failing send NOR a blocking/hanging send may stall it.
            # A single call that never returns would otherwise freeze all message
            # delivery across all channels until restart.
            timeout = float(getattr(self.config.channels, "dispatch_timeout_seconds", 30.0))
            try:
                await asyncio.wait_for(self.dispatch_outbound(message), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "Outbound dispatch timed out after {:.1f}s (channel={}) — skipped to keep delivering other messages",
                    timeout,
                    message.channel,
                )
            except Exception as exc:  # noqa: BLE001 - resilience of the dispatch loop
                logger.exception("Failed to dispatch outbound message: {}", exc)

    async def dispatch_outbound(self, message: SystemMessage) -> None:
        channel_name = message.channel or self.config.system.default_channel
        channel = self.channels.get(channel_name)
        if channel is None:
            logger.debug("No channel dispatcher for {}", channel_name)
            return
        await channel.send(message)
