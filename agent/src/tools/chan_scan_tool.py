"""Scan a symbol universe for recent Chanlun signal kinds via chan-kit."""

from __future__ import annotations

import json
from typing import Any

from src.agent.progress import emit_progress
from src.agent.tools import BaseTool
from src.tools.chan_client import fetch_chan_chart, normalize_period, to_ashare_code

_DEFAULT_UNIVERSE = [
    "000001.SZ",
    "600519.SH",
    "300750.SZ",
    "000858.SZ",
    "601318.SH",
    "510300.SH",
    "000333.SZ",
    "002594.SZ",
]


def scan_chan_universe(
    *,
    symbols: list[str],
    period: str = "day",
    kinds: list[str] | None = None,
    lookback_bars: int = 5,
    limit: int = 300,
) -> dict[str, Any]:
    """Return symbols whose recent bars contain matching signal kinds."""
    period_norm = normalize_period(period)
    wanted = {str(k).upper() for k in (kinds or ["B1", "B2", "B3", "S1", "S2", "S3"])}
    lookback = max(1, min(60, int(lookback_bars)))
    matches: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for raw in symbols:
        try:
            code = to_ashare_code(raw)
        except ValueError as exc:
            errors.append({"symbol": str(raw), "error": str(exc)})
            continue
        emit_progress("scan", message=f"scanning {code}")
        try:
            payload = fetch_chan_chart(symbol=code, period=period_norm, limit=limit)
        except Exception as exc:
            errors.append({"symbol": code, "error": str(exc)})
            continue

        candles = payload.get("candles") or []
        if not candles:
            continue
        cutoff = int(candles[-lookback]["time"])
        hits = []
        for tr in payload.get("trades") or []:
            kind = str(tr.get("kind") or "").upper()
            if kind not in wanted:
                continue
            if (tr.get("level") or "") == "bi" or kind.startswith("BI_"):
                continue
            t = int(tr.get("time") or 0)
            if t < cutoff:
                continue
            hits.append(
                {
                    "time": t,
                    "kind": kind,
                    "side": tr.get("side"),
                    "price": tr.get("price"),
                    "label": tr.get("label") or tr.get("title"),
                }
            )
        if hits:
            matches.append(
                {
                    "symbol": code,
                    "name": payload.get("name"),
                    "period": period_norm,
                    "quote": payload.get("quote"),
                    "hits": hits,
                    "latest_hit": hits[-1],
                }
            )

    return {
        "period": period_norm,
        "kinds": sorted(wanted),
        "lookback_bars": lookback,
        "scanned": len(symbols),
        "match_count": len(matches),
        "matches": matches,
        "errors": errors,
    }


class ChanScanTool(BaseTool):
    """Universe scan for recent Chanlun buy/sell points."""

    name = "chan_scan"
    description = (
        "Scan A-share symbols for recent Chanlun signals (B1/B2/B3/S1/S2/S3) "
        "using chan-kit / NICHANGLIN/czsc. Use for daily watchlists or scheduled research."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Symbols to scan; defaults to a small hot list",
            },
            "period": {"type": "string", "default": "day"},
            "kinds": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Signal kinds to match",
            },
            "lookback_bars": {
                "type": "integer",
                "default": 5,
                "description": "Only count signals in the last N bars",
            },
            "limit": {"type": "integer", "default": 300},
        },
        "required": [],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        try:
            symbols = kwargs.get("symbols") or list(_DEFAULT_UNIVERSE)
            if isinstance(symbols, str):
                symbols = [s.strip() for s in symbols.replace(";", ",").split(",") if s.strip()]
            kinds = kwargs.get("kinds")
            if isinstance(kinds, str):
                kinds = [k.strip() for k in kinds.split(",") if k.strip()]
            result = scan_chan_universe(
                symbols=list(symbols),
                period=str(kwargs.get("period", "day")),
                kinds=kinds,
                lookback_bars=int(kwargs.get("lookback_bars", 5) or 5),
                limit=int(kwargs.get("limit", 300) or 300),
            )
            return json.dumps({"status": "ok", **result}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
