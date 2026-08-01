"""WeChat invite QR helpers (pairing invite, not bot rebind)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.channels.pairing.store import (
    INVITE_PAYLOAD_PREFIX,
    create_invite,
    format_invite_payload,
)
from src.channels.utils import get_runtime_subdir

DEFAULT_INVITE_TTL_S = 1800
DEFAULT_INVITE_MAX_USES = 1


def invite_qr_dir() -> Path:
    """Return the directory used for generated WeChat invite QR images."""
    path = get_runtime_subdir("weixin") / "invites"
    path.mkdir(parents=True, exist_ok=True)
    return path


def render_invite_qr_png(payload: str, code: str) -> Path:
    """Render *payload* as a PNG QR code and return the file path."""
    import qrcode

    out_dir = invite_qr_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{code.replace('/', '_')}.png"
    img = qrcode.make(payload)
    img.save(out)
    return out


def build_invite_for_weixin(
    *,
    created_by: str,
    ttl_s: int = DEFAULT_INVITE_TTL_S,
    max_uses: int = DEFAULT_INVITE_MAX_USES,
) -> dict[str, Any]:
    """Create a weixin invite, render its QR PNG, and return delivery fields.

    Returns:
        Dict with ``code``, ``payload``, ``qr_path``, ``caption``, plus store
        metadata from :func:`create_invite`.
    """
    invite = create_invite(
        "weixin",
        created_by,
        ttl=ttl_s,
        max_uses=max_uses,
    )
    code = str(invite["code"])
    payload = str(invite.get("payload") or format_invite_payload(code))
    qr_path = render_invite_qr_png(payload, code)
    caption = (
        "ClawBot 邀请二维码\n\n"
        f"邀请码: `{code}`\n"
        f"扫码内容: `{INVITE_PAYLOAD_PREFIX}{code}`\n\n"
        "请让对方：\n"
        "1. 用微信扫描此二维码，记下邀请码\n"
        "2. 打开同一个 ClawBot 对话\n"
        f"3. 发送 `/join {code}`\n\n"
        "注意：这不是 bot 换绑登录码；扫码不会替换服务器上的微信绑定。"
    )
    return {
        **invite,
        "payload": payload,
        "qr_path": str(qr_path),
        "caption": caption,
    }
