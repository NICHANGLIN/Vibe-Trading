"""Tests for WeChat pairing-invite store, QR helper, and runtime slash commands."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.channels.bus.events import InboundMessage, OutboundMessage
from src.channels.bus.queue import MessageBus


class FakeSessionService:
    """Minimal stand-in; invite/join commands never call the agent loop."""

    def create_session(self, title: str = "", config: dict | None = None):
        return {"session_id": "session-1"}

    async def send_message(self, session_id: str, content: str, **kwargs):
        raise AssertionError("agent loop should not run for invite/join commands")

    def get_messages(self, session_id: str, limit: int = 200):
        return []


def test_create_and_redeem_invite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.channels.pairing import store as pairing_store

    monkeypatch.setattr(pairing_store, "_store_path", lambda: tmp_path / "pairing.json")

    invite = pairing_store.create_invite("weixin", "owner", ttl=600, max_uses=1)
    code = invite["code"]
    assert invite["payload"] == f"VIBE-WEIXIN-INVITE:{code}"
    assert pairing_store.is_approved("weixin", "guest") is False

    ok, reason = pairing_store.redeem_invite("weixin", code, "guest")
    assert ok is True
    assert reason == "approved"
    assert pairing_store.is_approved("weixin", "guest") is True

    ok2, reason2 = pairing_store.redeem_invite("weixin", code, "guest2")
    assert ok2 is False
    assert reason2 == "exhausted"


def test_redeem_invite_rejects_wrong_channel_and_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.channels.pairing import store as pairing_store

    monkeypatch.setattr(pairing_store, "_store_path", lambda: tmp_path / "pairing.json")
    invite = pairing_store.create_invite("weixin", "owner", ttl=600, max_uses=2)
    ok, reason = pairing_store.redeem_invite("telegram", invite["code"], "guest")
    assert ok is False
    assert reason == "wrong_channel"

    expired = pairing_store.create_invite("weixin", "owner", ttl=1, max_uses=1)
    # Force expiry
    path = tmp_path / "pairing.json"
    import json
    import time

    data = json.loads(path.read_text())
    data["invites"][expired["code"]]["expires_at"] = time.time() - 10
    path.write_text(json.dumps(data))
    ok2, reason2 = pairing_store.redeem_invite("weixin", expired["code"], "guest")
    assert ok2 is False
    assert reason2 == "expired"


def test_normalize_invite_payload_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.channels.pairing import store as pairing_store

    monkeypatch.setattr(pairing_store, "_store_path", lambda: tmp_path / "pairing.json")
    invite = pairing_store.create_invite("weixin", "owner")
    code = invite["code"]
    ok, reason = pairing_store.redeem_invite(
        "weixin", f"VIBE-WEIXIN-INVITE:{code.lower()}", "guest"
    )
    assert ok is True
    assert reason == "approved"


def test_build_invite_for_weixin_writes_png(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.channels import weixin_invite
    from src.channels.pairing import store as pairing_store

    monkeypatch.setattr(pairing_store, "_store_path", lambda: tmp_path / "pairing.json")
    monkeypatch.setattr(weixin_invite, "invite_qr_dir", lambda: tmp_path / "invites")

    result = weixin_invite.build_invite_for_weixin(created_by="owner", ttl_s=120, max_uses=1)
    qr_path = Path(result["qr_path"])
    assert qr_path.exists()
    assert qr_path.suffix == ".png"
    assert result["code"] in result["caption"]
    assert "/join" in result["caption"]


def test_runtime_invite_rejects_non_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from src.channels.pairing import store as pairing_store
        from src.channels.runtime import ChannelRuntime

        monkeypatch.setattr(pairing_store, "_store_path", lambda: tmp_path / "pairing.json")
        bus = MessageBus()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=FakeSessionService(),
            manager=None,
            session_map_path=tmp_path / "sessions.json",
            operators=["owner"],
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="weixin",
                    sender_id="stranger",
                    chat_id="stranger",
                    content="/invite",
                )
            )
            outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await runtime.stop()

        assert outbound.metadata.get("unauthorized") is True
        assert "Not authorized" in outbound.content

    asyncio.run(scenario())


def test_runtime_invite_and_join_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        from src.channels import weixin_invite
        from src.channels.pairing import store as pairing_store
        from src.channels.runtime import ChannelRuntime

        monkeypatch.setattr(pairing_store, "_store_path", lambda: tmp_path / "pairing.json")
        monkeypatch.setattr(weixin_invite, "invite_qr_dir", lambda: tmp_path / "invites")

        bus = MessageBus()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=FakeSessionService(),
            manager=None,
            session_map_path=tmp_path / "sessions.json",
            operators=["owner@im.wechat"],
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="weixin",
                    sender_id="owner@im.wechat",
                    chat_id="owner@im.wechat",
                    content="/invite",
                )
            )
            invite_out = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            assert invite_out.metadata.get("_invite_command") is True
            assert invite_out.media
            assert Path(invite_out.media[0]).exists()
            code = invite_out.metadata["invite_code"]

            await bus.publish_inbound(
                InboundMessage(
                    channel="weixin",
                    sender_id="guest@im.wechat",
                    chat_id="guest@im.wechat",
                    content=f"/join {code}",
                )
            )
            join_out = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await runtime.stop()

        assert join_out.metadata.get("_join_command") is True
        assert "Access granted" in join_out.content
        assert pairing_store.is_approved("weixin", "guest@im.wechat") is True

    asyncio.run(scenario())


def test_base_handle_message_allows_join_for_unapproved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from src.channels.base import BaseChannel
        from src.channels.pairing import store as pairing_store

        monkeypatch.setattr(pairing_store, "_store_path", lambda: tmp_path / "pairing.json")
        invite = pairing_store.create_invite("weixin", "owner")

        bus = MessageBus()

        class Dummy(BaseChannel):
            name = "weixin"

            async def start(self) -> None:
                return None

            async def stop(self) -> None:
                return None

            async def send(self, msg: OutboundMessage) -> None:
                return None

        ch = Dummy({"enabled": True, "allow_from": []}, bus)
        await ch._handle_message(
            sender_id="guest",
            chat_id="guest",
            content=f"/join {invite['code']}",
            is_dm=True,
        )
        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1)
        assert inbound.content.startswith("/join ")

    asyncio.run(scenario())
