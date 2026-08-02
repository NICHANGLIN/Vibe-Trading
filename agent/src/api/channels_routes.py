"""IM channel HTTP routes.

Mounted by ``agent/api_server.py`` via ``register_channels_routes(app, ...)``.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Pydantic models (defined locally -- NO shared modules, per maintainer rule)
# ---------------------------------------------------------------------------

class ChannelPairingCommandRequest(BaseModel):
    """Pairing command payload for IM channel sender pairing."""

    channel: str
    command: str


class ChannelNotifyRequest(BaseModel):
    """Proactive outbound notify to IM channel recipients."""

    content: str
    channel: str = "weixin"
    targets: str = "operators"  # operators | approved


# ---------------------------------------------------------------------------
# Lifecycle helpers (module-level, access host state via sys.modules)
# ---------------------------------------------------------------------------


async def _start_channel_runtime():
    """Start the IM channel runtime."""
    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    runtime = host._get_channel_runtime()
    await runtime.start(start_manager=True)
    return runtime


async def _stop_channel_runtime() -> None:
    """Stop the IM channel runtime if it was initialized."""
    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    if host._channel_runtime is None:
        return
    await host._channel_runtime.stop()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

AuthDep = Callable[..., Awaitable[Any] | Any]


def register_channels_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
) -> None:
    """Mount the channel routes onto ``app``.

    Resolves ``require_auth`` from the host ``api_server`` module via
    ``sys.modules`` when not passed explicitly.
    """
    # Resolve host dependencies via sys.modules fallback
    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")

    if host is None:
        raise RuntimeError(
            "register_channels_routes: api_server module not in sys.modules; "
            "ensure api_server is imported before calling this function"
        )

    if require_auth is None:
        require_auth = host.require_auth

    # Late-access closure for monkeypatch compatibility
    def _get_channel_runtime():
        """Late-access _get_channel_runtime for test monkeypatch compat."""
        h = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        return h._get_channel_runtime()

    # --- Routes ---

    @app.get("/channels/status", dependencies=[Depends(require_auth)])
    async def channels_status():
        """Return IM channel runtime and adapter status."""
        runtime = _get_channel_runtime()
        return runtime.status()

    @app.post("/channels/start", dependencies=[Depends(require_auth)])
    async def channels_start():
        """Start configured IM channel adapters."""
        runtime = await _start_channel_runtime()
        return {"status": "started", **runtime.status()}

    @app.post("/channels/stop", dependencies=[Depends(require_auth)])
    async def channels_stop():
        """Stop configured IM channel adapters."""
        runtime = _get_channel_runtime()
        await runtime.stop()
        return {"status": "stopped", **runtime.status()}

    @app.post("/channels/pairing/command", dependencies=[Depends(require_auth)])
    async def channels_pairing_command(payload: ChannelPairingCommandRequest):
        """Run a pairing command against the shared pairing store."""
        from src.channels.pairing import handle_pairing_command

        return {
            "channel": payload.channel,
            "reply": handle_pairing_command(payload.channel, payload.command),
        }

    @app.post("/channels/notify", dependencies=[Depends(require_auth)])
    async def channels_notify(payload: ChannelNotifyRequest):
        """Push a proactive message to channel operators or approved senders.

        WeChat requires a recent ``context_token`` per recipient; targets without
        one are skipped and listed in ``skipped``.
        """
        from src.channels.bus.events import OutboundMessage
        from src.channels.config import load_channels_config
        from src.channels.pairing import get_approved
        from src.channels.runtime import ChannelRuntime

        content = (payload.content or "").strip()
        if not content:
            return {"ok": False, "error": "content is required", "sent": [], "skipped": []}

        channel_name = (payload.channel or "weixin").strip().lower() or "weixin"
        targets_mode = (payload.targets or "operators").strip().lower()
        if targets_mode not in {"operators", "approved"}:
            targets_mode = "operators"

        runtime = _get_channel_runtime()
        if runtime.manager is None:
            return {
                "ok": False,
                "error": "channel manager not initialized; call /channels/start first",
                "sent": [],
                "skipped": [],
            }

        adapter = runtime.manager.get_channel(channel_name)
        if adapter is None:
            return {
                "ok": False,
                "error": f"channel '{channel_name}' is not loaded",
                "sent": [],
                "skipped": [],
            }
        if not getattr(adapter, "is_running", False):
            # Best-effort start of the whole runtime/manager.
            await runtime.start(start_manager=True)
            if not getattr(adapter, "is_running", False):
                return {
                    "ok": False,
                    "error": f"channel '{channel_name}' is not running",
                    "sent": [],
                    "skipped": [],
                }

        config = load_channels_config()
        global_ops, channel_ops = ChannelRuntime.operators_from_config(config)
        recipients: list[str] = []
        if targets_mode == "approved":
            recipients = list(get_approved(channel_name))
        else:
            recipients = sorted(global_ops | channel_ops.get(channel_name, set()))
            if not recipients:
                recipients = list(get_approved(channel_name))

        # Normalize chat ids (operators may be stored as bare or @im.wechat).
        uniq: list[str] = []
        seen: set[str] = set()
        for raw in recipients:
            chat_id = str(raw or "").strip()
            if not chat_id or chat_id in seen:
                continue
            seen.add(chat_id)
            uniq.append(chat_id)

        sent: list[str] = []
        skipped: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []

        ctx_map = getattr(adapter, "_context_tokens", None)
        for chat_id in uniq:
            if isinstance(ctx_map, dict) and not ctx_map.get(chat_id):
                skipped.append({"chat_id": chat_id, "reason": "missing_context_token"})
                continue
            try:
                await adapter.send(
                    OutboundMessage(
                        channel=channel_name,
                        chat_id=chat_id,
                        content=content,
                        metadata={"proactive": True, "source": "channels_notify"},
                    )
                )
                sent.append(chat_id)
            except Exception as exc:  # noqa: BLE001
                errors.append({"chat_id": chat_id, "error": str(exc)})

        return {
            "ok": bool(sent) and not errors,
            "channel": channel_name,
            "targets": targets_mode,
            "sent": sent,
            "skipped": skipped,
            "errors": errors,
            "count_sent": len(sent),
            "count_skipped": len(skipped),
            "count_errors": len(errors),
        }
