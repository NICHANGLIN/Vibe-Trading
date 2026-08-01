"""Pairing module for DM sender approval."""

from src.channels.pairing.store import (
    INVITE_PAYLOAD_PREFIX,
    approve_code,
    create_invite,
    deny_code,
    format_expiry,
    format_invite_payload,
    format_pairing_reply,
    generate_code,
    get_approved,
    handle_pairing_command,
    is_approved,
    is_join_command,
    list_pending,
    normalize_invite_code,
    parse_join_code,
    redeem_invite,
    revoke,
)

# Metadata keys used by channels and commands to tag pairing-related messages.
PAIRING_CODE_META_KEY = "_pairing_code"
PAIRING_COMMAND_META_KEY = "_pairing_command"
INVITE_COMMAND_META_KEY = "_invite_command"
JOIN_COMMAND_META_KEY = "_join_command"

__all__ = [
    "INVITE_COMMAND_META_KEY",
    "INVITE_PAYLOAD_PREFIX",
    "JOIN_COMMAND_META_KEY",
    "PAIRING_CODE_META_KEY",
    "PAIRING_COMMAND_META_KEY",
    "approve_code",
    "create_invite",
    "deny_code",
    "format_expiry",
    "format_invite_payload",
    "format_pairing_reply",
    "generate_code",
    "get_approved",
    "handle_pairing_command",
    "is_approved",
    "is_join_command",
    "list_pending",
    "normalize_invite_code",
    "parse_join_code",
    "redeem_invite",
    "revoke",
]
