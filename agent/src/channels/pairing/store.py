"""Pairing store for DM sender approval.

Persistent storage at ``~/.vibe-trading/pairing.json`` keeps approved senders
and pending pairing codes per channel.  The store is designed for
private-assistant scale: small JSON file, simple locking, no external DB.
"""

from __future__ import annotations

import json
import logging
import secrets
import string
import threading
import time
from pathlib import Path
from typing import Any

from src.config.paths import get_data_dir

logger = logging.getLogger(__name__)

# threading.Lock is used so store functions remain callable from both sync CLI
# and async channel handlers.  At private-assistant scale (small JSON file,
# sub-millisecond operations) the brief block is acceptable.
_LOCK = threading.Lock()
_ALPHABET = string.ascii_uppercase + string.digits
_CODE_LENGTH = 8  # e.g. ABCD-EFGH
_TTL_DEFAULT_S = 600  # 10 minutes
_INVITE_TTL_DEFAULT_S = 1800  # 30 minutes
_INVITE_MAX_USES_DEFAULT = 1
INVITE_PAYLOAD_PREFIX = "VIBE-WEIXIN-INVITE:"


def _store_path() -> Path:
    return get_data_dir() / "pairing.json"


def _write_text_atomic(path: Path, text: str) -> None:
    """Write *text* to *path* atomically via a temp file + rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _empty_store() -> dict[str, Any]:
    return {"approved": {}, "pending": {}, "invites": {}}


def _load() -> dict[str, Any]:
    path = _store_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _empty_store()
    except (json.JSONDecodeError, OSError):
        logger.warning("Corrupted pairing store, resetting")
        return _empty_store()

    # Convert approved lists to str sets for O(1) lookup.
    for channel, users in data.get("approved", {}).items():
        data["approved"][channel] = {str(u) for u in users}
    data.setdefault("invites", {})
    return data


def _save(data: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Convert sets back to lists for JSON serialization
    payload = {
        "approved": {ch: sorted(list(users)) for ch, users in data.get("approved", {}).items()},
        "pending": dict(data.get("pending", {})),
        "invites": dict(data.get("invites", {})),
    }
    _write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False))


def _gc_pending(data: dict[str, Any]) -> None:
    """Remove expired pending entries in-place."""
    now = time.time()
    pending: dict[str, Any] = data.get("pending", {})
    expired = [code for code, info in pending.items() if info.get("expires_at", 0) < now]
    for code in expired:
        del pending[code]


def _gc_invites(data: dict[str, Any]) -> None:
    """Remove expired invite entries in-place.

    Exhausted-but-unexpired invites are kept so redeem can report ``exhausted``
    distinctly from ``invalid``.
    """
    now = time.time()
    invites: dict[str, Any] = data.get("invites", {})
    expired = [code for code, info in invites.items() if info.get("expires_at", 0) < now]
    for code in expired:
        del invites[code]


def _new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_invite_code(raw: str) -> str:
    """Normalize user-provided invite codes (strip payload prefix / whitespace)."""
    text = (raw or "").strip()
    if text.upper().startswith(INVITE_PAYLOAD_PREFIX):
        text = text[len(INVITE_PAYLOAD_PREFIX) :]
    text = text.strip().strip("`").strip()
    return text.upper()


def is_join_command(content: str) -> bool:
    """Return True when *content* is a ``/join`` slash command."""
    stripped = (content or "").strip().lower()
    return stripped == "/join" or stripped.startswith("/join ")


def parse_join_code(content: str) -> str | None:
    """Extract the invite code argument from a ``/join`` command, if present."""
    parts = (content or "").strip().split(None, 1)
    if not parts or parts[0].lower() != "/join":
        return None
    if len(parts) < 2:
        return None
    code = normalize_invite_code(parts[1])
    return code or None


def create_invite(
    channel: str,
    created_by: str,
    *,
    ttl: int = _INVITE_TTL_DEFAULT_S,
    max_uses: int = _INVITE_MAX_USES_DEFAULT,
) -> dict[str, Any]:
    """Create a channel invite token that can be redeemed via ``/join``.

    Returns:
        Dict with ``code``, ``channel``, ``created_by``, ``created_at``,
        ``expires_at``, ``max_uses``, ``uses``, and ``payload``.
    """
    with _LOCK:
        data = _load()
        _gc_pending(data)
        _gc_invites(data)
        code = _new_code()
        now = time.time()
        info = {
            "channel": channel,
            "created_by": str(created_by),
            "created_at": now,
            "expires_at": now + max(int(ttl), 1),
            "max_uses": max(int(max_uses), 1),
            "uses": 0,
        }
        data.setdefault("invites", {})[code] = info
        _save(data)
        logger.info(
            "Created invite %s for %s by %s (ttl=%ss, max_uses=%s)",
            code,
            channel,
            created_by,
            ttl,
            max_uses,
        )
        return {
            **info,
            "code": code,
            "payload": f"{INVITE_PAYLOAD_PREFIX}{code}",
        }


def redeem_invite(channel: str, code: str, sender_id: str) -> tuple[bool, str]:
    """Redeem an invite for *sender_id* on *channel*.

    Returns:
        ``(True, detail)`` on success, ``(False, reason)`` on failure.
        On success the sender is added to the channel's approved list.
    """
    normalized = normalize_invite_code(code)
    if not normalized:
        return False, "missing_code"

    with _LOCK:
        data = _load()
        _gc_pending(data)
        # Do not GC exhausted-but-unexpired invites before lookup.
        invites: dict[str, Any] = data.setdefault("invites", {})
        info = invites.get(normalized)
        if info is None:
            # Also try case-sensitive key for older stores, then fail.
            info = invites.get(code.strip())
            if info is None:
                return False, "invalid"
            normalized = code.strip()

        if info.get("channel") != channel:
            return False, "wrong_channel"

        now = time.time()
        if float(info.get("expires_at", 0)) < now:
            invites.pop(normalized, None)
            _save(data)
            return False, "expired"

        max_uses = int(info.get("max_uses", _INVITE_MAX_USES_DEFAULT))
        uses = int(info.get("uses", 0))
        if uses >= max_uses:
            return False, "exhausted"

        sid = str(sender_id)
        approved = data.setdefault("approved", {}).setdefault(channel, set())
        already = sid in approved
        approved.add(sid)
        info["uses"] = uses + 1
        if info["uses"] >= max_uses:
            # Keep until expiry so a second redeem reports exhausted, not invalid.
            pass
        _save(data)
        logger.info(
            "Redeemed invite %s for %s@%s (uses=%s/%s, already=%s)",
            normalized,
            sid,
            channel,
            info["uses"],
            max_uses,
            already,
        )
        return True, "already_approved" if already else "approved"


def format_invite_payload(code: str) -> str:
    """Return the QR payload string for an invite code."""
    return f"{INVITE_PAYLOAD_PREFIX}{normalize_invite_code(code)}"


def generate_code(
    channel: str,
    sender_id: str,
    ttl: int = _TTL_DEFAULT_S,
) -> str:
    """Create a new pairing code for *sender_id* on *channel*.

    Returns the code (e.g. ``"ABCD-EFGH"``).
    """
    with _LOCK:
        data = _load()
        _gc_pending(data)
        code = _new_code()

        data.setdefault("pending", {})[code] = {
            "channel": channel,
            "sender_id": str(sender_id),
            "created_at": time.time(),
            "expires_at": time.time() + ttl,
        }
        _save(data)
        logger.info("Generated pairing code %s for %s@%s", code, sender_id, channel)
        return code


def approve_code(code: str, *, restrict_channel: str | None = None) -> tuple[str, str] | None:
    """Approve a pending pairing code.

    Args:
        code: The pairing code to approve.
        restrict_channel: When set, only approve the code if its pending
            request belongs to this channel. A code on a different channel is
            treated as not-found (returns ``None``) so callers scoped to one
            channel cannot approve — or even probe the existence of — pairing
            requests on other channels.

    Returns ``(channel, sender_id)`` on success, or ``None`` if the code
    does not exist, has expired, or is out of the requested channel scope.
    """
    with _LOCK:
        data = _load()
        _gc_pending(data)
        pending: dict[str, Any] = data.get("pending", {})
        info = pending.get(code)
        if info is None:
            return None
        if restrict_channel is not None and info.get("channel") != restrict_channel:
            return None
        del pending[code]
        channel = info["channel"]
        sender_id = str(info["sender_id"])
        data.setdefault("approved", {}).setdefault(channel, set()).add(sender_id)
        _save(data)
        logger.info("Approved pairing code %s for %s@%s", code, sender_id, channel)
        return channel, sender_id


def deny_code(code: str, *, restrict_channel: str | None = None) -> bool:
    """Reject and discard a pending pairing code.

    Args:
        code: The pairing code to deny.
        restrict_channel: When set, only deny the code if its pending request
            belongs to this channel; a code on another channel is treated as
            not-found (returns ``False``).

    Returns ``True`` if the code existed (within scope) and was removed.
    """
    with _LOCK:
        data = _load()
        _gc_pending(data)
        pending: dict[str, Any] = data.get("pending", {})
        info = pending.get(code)
        if info is None:
            return False
        if restrict_channel is not None and info.get("channel") != restrict_channel:
            return False
        del pending[code]
        _save(data)
        logger.info("Denied pairing code %s", code)
        return True


def is_approved(channel: str, sender_id: str) -> bool:
    """Check whether *sender_id* has been approved on *channel*."""
    with _LOCK:
        data = _load()
        approved: dict[str, set[str]] = data.get("approved", {})
        return str(sender_id) in approved.get(channel, set())


def list_pending(restrict_channel: str | None = None) -> list[dict[str, Any]]:
    """Return non-expired pending pairing requests.

    Args:
        restrict_channel: When set, return only requests for this channel.
            Used to scope a per-channel operator's view so they cannot see
            pending codes or sender IDs from other channels.
    """
    with _LOCK:
        data = _load()
        _gc_pending(data)
        return [
            {"code": code, **info}
            for code, info in data.get("pending", {}).items()
            if restrict_channel is None or info.get("channel") == restrict_channel
        ]


def revoke(channel: str, sender_id: str) -> bool:
    """Remove an approved sender from *channel*.

    Returns ``True`` if the sender was present and removed.
    """
    with _LOCK:
        data = _load()
        approved: dict[str, set[str]] = data.get("approved", {})
        users = approved.get(channel, set())
        sid = str(sender_id)
        if sid in users:
            users.discard(sid)
            if not users:
                del approved[channel]
            _save(data)
            logger.info("Revoked %s from %s", sid, channel)
            return True
        return False


def get_approved(channel: str) -> list[str]:
    """Return all approved sender IDs for *channel*."""
    with _LOCK:
        data = _load()
        return sorted(data.get("approved", {}).get(channel, set()))


def format_pairing_reply(code: str) -> str:
    """Return the pairing-code message sent to unrecognised DM senders."""
    return (
        "Hi there! This assistant only responds to approved users.\n\n"
        f"Your pairing code is: `{code}`\n\n"
        "To get access, ask the owner to approve this code:\n"
        f"- In this chat: send `/pairing approve {code}`"
    )


def format_expiry(expires_at: float) -> str:
    """Return a human-readable expiry string (e.g. ``"120s"`` or ``"expired"``)."""
    remaining = int(expires_at - time.time())
    return f"{remaining}s" if remaining > 0 else "expired"


def handle_pairing_command(
    channel: str,
    subcommand_text: str,
    *,
    requesting_channel: str | None = None,
    is_global_operator: bool = True,
) -> str:
    """Execute a pairing subcommand and return the reply text.

    This is a pure function (no side effects other than store mutations)
    so it can be used from the CLI, the REST admin endpoint, and the IM
    channel runtime.

    Args:
        channel: The default channel used for single-argument ``revoke``.
        subcommand_text: The pairing subcommand and its arguments.
        requesting_channel: The channel the command arrived on. Used together
            with ``is_global_operator`` to scope a non-global operator to a
            single channel. ``None`` (the default) applies no channel scope.
        is_global_operator: ``True`` (the default) grants cross-channel
            authority and full request details — this is the behavior for the
            authenticated CLI/REST admin plane and for configured global
            operators. ``False`` restricts the caller to ``requesting_channel``:
            ``list`` shows only that channel, ``approve``/``deny`` act only on
            codes for that channel, and cross-channel ``revoke`` is refused.

    Returns:
        The reply text to send back to the caller.
    """
    parts = subcommand_text.split()
    sub = parts[0].lower() if parts else "list"
    arg = parts[1] if len(parts) > 1 else None

    # Non-global operators are pinned to the channel the command arrived on so
    # they cannot read, approve, or revoke pairing state on other channels.
    scope = None if is_global_operator else requesting_channel

    if sub in ("list",):
        pending = list_pending(restrict_channel=scope)
        if not pending:
            return "No pending pairing requests."
        lines = ["Pending pairing requests:"]
        for item in pending:
            expiry = format_expiry(item.get("expires_at", 0))
            lines.append(
                f"- `{item['code']}` | {item['channel']} | {item['sender_id']} | {expiry}"
            )
        return "\n".join(lines)

    elif sub == "approve":
        if arg is None:
            return "Usage: `/pairing approve <code>`"
        result = approve_code(arg, restrict_channel=scope)
        if result is None:
            return f"Invalid or expired pairing code: `{arg}`"
        ch, sid = result
        return f"Approved pairing code `{arg}` — {sid} can now access {ch}"

    elif sub == "deny":
        if arg is None:
            return "Usage: `/pairing deny <code>`"
        if deny_code(arg, restrict_channel=scope):
            return f"Denied pairing code `{arg}`"
        return f"Pairing code `{arg}` not found or already expired"

    elif sub == "revoke":
        if len(parts) == 2:
            return (
                f"Revoked {arg} from {channel}"
                if revoke(channel, arg)
                else f"{arg} was not in the approved list for {channel}"
            )
        if len(parts) == 3:
            if not is_global_operator and parts[1] != requesting_channel:
                return "Not authorized: cross-channel revoke requires a global operator."
            return (
                f"Revoked {parts[2]} from {arg}"
                if revoke(arg, parts[2])
                else f"{parts[2]} was not in the approved list for {arg}"
            )
        return "Usage: `/pairing revoke <user_id>` or `/pairing revoke <channel> <user_id>`"

    return (
        "Unknown pairing command.\n"
        "Usage: `/pairing [list|approve <code>|deny <code>|revoke <user_id>|revoke <channel> <user_id>]`"
    )
