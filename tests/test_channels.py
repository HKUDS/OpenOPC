from __future__ import annotations

import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from opc.channels.manager import ChannelManager
from opc.channels.session import ChannelSessionMapping
from opc.core.config import OPCConfig
from opc.core.models import SystemMessage, UserMessage
from opc.engine import OPCEngine
from opc.layer0_interaction.message_bus import MessageBus


def _stub_bus():
    return SimpleNamespace(publish_inbound=None, publish_outbound=None)


class ChannelRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_manager_initializes_enabled_channels_and_dispatches(self) -> None:
        config = OPCConfig()
        config.channels.slack.enabled = True
        config.channels.slack.allow_from = ["*"]
        bus = MessageBus()
        sent: list[SystemMessage] = []

        class _Channel:
            def __init__(self, cfg, bus):
                self.cfg = cfg
                self.bus = bus
                self._running = False

            async def start(self):
                self._running = True

            async def stop(self):
                self._running = False

            async def send(self, message: SystemMessage):
                sent.append(message)

        real_import = __import__
        with patch("builtins.__import__") as mocked_import:
            def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
                if name == "opc.channels.slack":
                    return SimpleNamespace(SlackChannel=_Channel)
                return real_import(name, globals, locals, fromlist, level)
            mocked_import.side_effect = fake_import
            manager = ChannelManager(config, bus)

        self.assertEqual(manager.enabled_channels, ["slack"])
        await manager.start_all()
        await manager.dispatch_outbound(SystemMessage(channel="slack", user_id="u", session_id="s", content="hello", metadata={"chat_id": "c1"}))
        await manager.stop_all()
        self.assertEqual(sent[0].content, "hello")
        self.assertEqual(sent[0].metadata["chat_id"], "c1")

    async def test_blocking_send_does_not_stall_dispatch_loop(self) -> None:
        """Regression: a single blocking send must not freeze the shared outbound consumer.

        The _dispatch_outbound loop is the only outbound consumer for every channel.
        Before the timeout guard, an awaited send that never returns would stall the
        whole loop, so messages for every other chat would never be delivered. The guard
        bounds a single send and lets the loop move on, so later messages still arrive.
        """
        import asyncio

        config = OPCConfig()
        config.channels.slack.enabled = True
        config.channels.slack.allow_from = ["*"]
        # Use a small timeout so the test runs quickly.
        config.channels.dispatch_timeout_seconds = 0.05
        config.channels.telegram.enabled = True
        config.channels.telegram.allow_from = ["*"]
        bus = MessageBus()
        delivered: list[SystemMessage] = []

        class _BlockingChannel:
            def __init__(self, cfg, bus):
                self.cfg = cfg
                self.bus = bus
                self._running = False

            async def start(self):
                self._running = True

            async def stop(self):
                self._running = False

            async def send(self, message: SystemMessage):
                delivered.append(message)

        # A channel whose send() blocks forever until cancelled by the timeout guard.
        class _HangingChannel(_BlockingChannel):
            async def send(self, message: SystemMessage):
                delivered.append(message)
                await asyncio.Event().wait()  # never completes on its own

        real_import = __import__
        with patch("builtins.__import__") as mocked_import:
            def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
                if name == "opc.channels.slack":
                    return SimpleNamespace(SlackChannel=_HangingChannel)
                if name == "opc.channels.telegram":
                    return SimpleNamespace(TelegramChannel=_BlockingChannel)
                return real_import(name, globals, locals, fromlist, level)
            mocked_import.side_effect = fake_import
            manager = ChannelManager(config, bus)

        self.assertEqual(sorted(manager.enabled_channels), ["slack", "telegram"])
        await manager.start_all()  # also starts the internal _dispatch_outbound loop

        try:
            # The slate's message blocks; the guard times it out, then the loop must
            # keep running so the subsequent (healthy) telegram message still arrives.
            await bus.publish_outbound(SystemMessage(channel="slack", user_id="u", session_id="s", content="blocked", metadata={"chat_id": "c1"}))
            await bus.publish_outbound(SystemMessage(channel="telegram", user_id="u", session_id="s", content="healthy", metadata={"chat_id": "c2"}))
            await asyncio.sleep(0.3)  # allow the blocking send to time out and the healthy one to flow
        finally:
            await manager.stop_all()  # cancels the internal dispatch loop

        contents = [m.content for m in delivered]
        self.assertIn("blocked", contents, "the blocking send should still be attempted")
        self.assertIn("healthy", contents, "healthy send after a blocking send must still be delivered")

    def test_config_load_save_roundtrip_includes_channels(self) -> None:
        config = OPCConfig()
        config.channels.feishu.enabled = True
        config.channels.feishu.app_id = "app"
        config.channels.feishu.allow_from = ["*"]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            config.save(path)
            reloaded = OPCConfig.load(path)
        self.assertTrue(reloaded.channels.feishu.enabled)
        self.assertEqual(reloaded.channels.feishu.app_id, "app")

    async def test_user_message_metadata_is_preserved_in_response(self) -> None:
        bus = MessageBus()
        payload = UserMessage(channel="feishu", user_id="user1", content="hi", session_id="sess", metadata={"chat_id": "chat-1", "thread_id": "th-1"})

        async def handler(message: UserMessage):
            return SystemMessage(channel=message.channel, user_id=message.user_id, session_id=message.session_id, content="ok", metadata={"chat_id": message.metadata.get("chat_id", "")})

        bus.set_handler(handler)
        response = await bus.process_single(payload)
        assert response is not None
        self.assertEqual(response.metadata["chat_id"], "chat-1")

    async def test_base_channel_publish_inbound_normalizes_metadata_and_thread_session(self) -> None:
        from opc.channels.slack import SlackChannel

        config = OPCConfig()
        config.channels.slack.allow_from = ["*"]
        bus = MessageBus()
        channel = SlackChannel(config.channels.slack, bus)

        await channel.publish_inbound(
            sender_id="u-1",
            chat_id="c-1",
            content="hello",
            attachments=["a.png"],
            metadata={"thread_id": "th-9", "reply_to": "ts-1", "raw": {"x": 1}},
        )
        message = await bus._inbound_queue.get()
        self.assertEqual(message.session_id, "slack:c-1:th-9")
        self.assertEqual(message.metadata["sender_id"], "u-1")
        self.assertEqual(message.metadata["reply_to"], "ts-1")
        self.assertEqual(message.metadata["attachments"], ["a.png"])

    def test_channel_session_mapping_prefers_thread_scope(self) -> None:
        route = ChannelSessionMapping.derive(channel="feishu", chat_id="chat-1", thread_id="thread-1")
        self.assertEqual(route.session_id, "feishu:chat-1:thread-1")
        self.assertEqual(route.thread_id, "thread-1")


class ProviderSmokeTests(unittest.TestCase):
    def test_provider_registry_shape(self) -> None:
        config = OPCConfig()
        expected = ["telegram", "whatsapp", "discord", "feishu", "mochat", "dingtalk", "email", "slack", "qq", "matrix"]
        for name in expected:
            self.assertTrue(hasattr(config.channels, name))

    def test_telegram_normalize_update(self) -> None:
        from opc.channels.telegram import TelegramChannel
        channel = TelegramChannel(OPCConfig().channels.telegram, _stub_bus())
        normalized = channel.normalize_update({"message": {"text": "hi", "chat": {"id": 7}, "from": {"id": 9}, "message_thread_id": 88}})
        self.assertEqual(normalized["chat_id"], "7")
        self.assertEqual(normalized["thread_id"], "88")

    def test_whatsapp_normalize_event(self) -> None:
        from opc.channels.whatsapp import WhatsAppChannel
        channel = WhatsAppChannel(OPCConfig().channels.whatsapp, _stub_bus())
        normalized = channel.normalize_event({"message": {"from": "u1", "chat_id": "c1", "text": "hi"}})
        self.assertEqual(normalized["sender_id"], "u1")
        self.assertEqual(normalized["chat_id"], "c1")

    def test_discord_normalize_event(self) -> None:
        from opc.channels.discord import DiscordChannel
        channel = DiscordChannel(OPCConfig().channels.discord, _stub_bus())
        normalized = channel.normalize_event({"author": {"id": "u1"}, "channel_id": "c1", "content": "hi", "is_thread": True, "thread_id": "t1"})
        self.assertEqual(normalized["thread_id"], "t1")

    def test_feishu_normalize_event(self) -> None:
        from opc.channels.feishu import FeishuChannel
        channel = FeishuChannel(OPCConfig().channels.feishu, _stub_bus())
        normalized = channel.normalize_event({"event": {"sender": {"sender_id": {"open_id": "u1"}}, "message": {"chat_id": "c1", "content": "hi", "thread_id": "t1", "parent_id": "p1"}}})
        self.assertEqual(normalized["sender_id"], "u1")
        self.assertEqual(normalized["reply_to"], "p1")

    def test_mochat_normalize_event(self) -> None:
        from opc.channels.mochat import MochatChannel
        channel = MochatChannel(OPCConfig().channels.mochat, _stub_bus())
        normalized = channel.normalize_event({"message": {"from": "u1", "conversation_id": "c1", "text": "hi"}})
        self.assertEqual(normalized["chat_id"], "c1")

    def test_dingtalk_normalize_event(self) -> None:
        from opc.channels.dingtalk import DingTalkChannel
        channel = DingTalkChannel(OPCConfig().channels.dingtalk, _stub_bus())
        normalized = channel.normalize_event({"senderStaffId": "u1", "conversationId": "c1", "text": {"content": "hi"}, "msgId": "m1"})
        self.assertEqual(normalized["reply_to"], "m1")

    def test_email_normalize_email(self) -> None:
        from opc.channels.email import EmailChannel
        channel = EmailChannel(OPCConfig().channels.email, _stub_bus())
        normalized = channel.normalize_email({"from": "a@example.com", "thread_id": "th1", "body": "hi", "message_id": "m1"})
        self.assertEqual(normalized["chat_id"], "th1")

    def test_email_parse_long_body_returns_marked_preview_metadata(self) -> None:
        from opc.channels.email import EmailChannel
        config = OPCConfig().channels.email
        config.max_body_chars = 20
        channel = EmailChannel(config, _stub_bus())
        message = EmailMessage()
        message["From"] = "a@example.com"
        message["Subject"] = "Long body"
        message["Message-ID"] = "<m1>"
        message.set_content("hello " * 20)

        with patch("opc.channels.email.persist_tool_result", return_value={"full_output_path": "/tmp/email-body.txt"}):
            parsed = channel._parse_message_bytes(message.as_bytes())

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertTrue(parsed["body_truncated"])
        self.assertGreater(parsed["body_omitted_chars"], 0)
        self.assertIn("email body preview truncated", parsed["body"])
        self.assertTrue(parsed["full_body_path"])

    def test_slack_normalize_event(self) -> None:
        from opc.channels.slack import SlackChannel
        channel = SlackChannel(OPCConfig().channels.slack, _stub_bus())
        normalized = channel.normalize_event({"event": {"user": "u1", "channel": "c1", "text": "hi", "thread_ts": "t1", "ts": "m1"}})
        self.assertEqual(normalized["thread_id"], "t1")

    def test_qq_normalize_event(self) -> None:
        from opc.channels.qq import QQChannel
        channel = QQChannel(OPCConfig().channels.qq, _stub_bus())
        normalized = channel.normalize_event({"author": {"id": "u1"}, "group_openid": "g1", "content": "hi", "id": "m1"})
        self.assertEqual(normalized["chat_id"], "g1")

    def test_matrix_normalize_event(self) -> None:
        from opc.channels.matrix import MatrixChannel
        channel = MatrixChannel(OPCConfig().channels.matrix, _stub_bus())
        normalized = channel.normalize_event({"sender": "@u:server", "room_id": "!r:server", "event_id": "$e", "content": {"body": "hi", "m.relates_to": {"event_id": "$thread"}}})
        self.assertEqual(normalized["chat_id"], "!r:server")
        self.assertEqual(normalized["thread_id"], "$thread")

    def test_socket_and_polling_channels_capture_outbound(self) -> None:
        from opc.channels.email import EmailChannel
        from opc.channels.slack import SlackChannel

        slack = SlackChannel(OPCConfig().channels.slack, _stub_bus())
        email = EmailChannel(OPCConfig().channels.email, _stub_bus())

        import asyncio
        asyncio.run(slack.send(SystemMessage(channel="slack", user_id="u", session_id="s", content="hello", metadata={"chat_id": "c1", "thread_id": "t1"})))
        asyncio.run(email.send(SystemMessage(channel="email", user_id="u", session_id="s", content="hello", metadata={"chat_id": "mailbox"})))

        self.assertEqual(slack.last_outbound["thread_id"], "t1")
        self.assertEqual(email.last_outbound["chat_id"], "mailbox")


if __name__ == "__main__":
    unittest.main()
