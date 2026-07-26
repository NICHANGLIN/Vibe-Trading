"""Align broker/Shadow Account trades with Chanlun signal points."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.tools.chan_client import fetch_chan_chart, normalize_period, to_ashare_code


def _parse_unix(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        # ms → s
        if v > 10_000_000_000:
            v //= 1000
        return v
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_unix(int(text))
    try:
        import pandas as pd

        ts = pd.Timestamp(text)
        if ts.tzinfo is None:
            ts = ts.tz_localize("Asia/Shanghai")
        else:
            ts = ts.tz_convert("Asia/Shanghai")
        return int(ts.timestamp())
    except Exception:
        return None


def align_trades_to_chan(
    *,
    symbol: str,
    trades: list[dict[str, Any]],
    period: str = "day",
    window_seconds: int | None = None,
    limit: int = 800,
) -> dict[str, Any]:
    """Match user trades to nearest Chanlun signal within a time window."""
    code = to_ashare_code(symbol)
    period_norm = normalize_period(period)
    if window_seconds is None:
        window_seconds = 3 * 86400 if period_norm == "day" else 6 * 3600

    payload = fetch_chan_chart(symbol=code, period=period_norm, limit=limit)
    signals = []
    for tr in payload.get("trades") or []:
        kind = str(tr.get("kind") or "").upper()
        if (tr.get("level") or "") == "bi" or kind.startswith("BI_"):
            continue
        if kind not in {"B1", "B2", "B3", "S1", "S2", "S3"}:
            continue
        signals.append(
            {
                "time": int(tr.get("time") or 0),
                "kind": kind,
                "side": tr.get("side"),
                "price": tr.get("price"),
                "label": tr.get("label") or tr.get("title"),
            }
        )

    rows: list[dict[str, Any]] = []
    hit = 0
    for raw in trades:
        t = _parse_unix(raw.get("time") or raw.get("datetime") or raw.get("date"))
        side = str(raw.get("side") or raw.get("direction") or "").lower()
        if side in {"buy", "b", "long", "买入"}:
            side = "buy"
        elif side in {"sell", "s", "short", "卖出"}:
            side = "sell"
        if t is None:
            rows.append({"input": raw, "aligned": False, "error": "unparseable time"})
            continue

        best = None
        best_dt = None
        for sig in signals:
            if side and sig["side"] and side != sig["side"]:
                continue
            dt = abs(sig["time"] - t)
            if dt > window_seconds:
                continue
            if best_dt is None or dt < best_dt:
                best = sig
                best_dt = dt
        aligned = best is not None
        if aligned:
            hit += 1
        rows.append(
            {
                "input": {
                    "time": t,
                    "side": side or raw.get("side"),
                    "price": raw.get("price"),
                    "qty": raw.get("qty") or raw.get("quantity"),
                },
                "aligned": aligned,
                "delta_seconds": best_dt,
                "signal": best,
            }
        )

    return {
        "symbol": code,
        "name": payload.get("name"),
        "period": period_norm,
        "window_seconds": window_seconds,
        "user_trades": len(trades),
        "chan_signals": len(signals),
        "aligned_count": hit,
        "align_rate": (hit / len(trades)) if trades else 0.0,
        "rows": rows,
        "note": (
            "Use after Shadow Account journal parse: check whether real fills "
            "land near B1–B3/S1–S3 from chan-kit (NICHANGLIN/czsc)."
        ),
    }


class ChanShadowAlignTool(BaseTool):
    """Align real/shadow trades to Chanlun signals for review."""

    name = "chan_shadow_align"
    description = (
        "Align a list of user or Shadow Account trades to nearby Chanlun "
        "B1–B3/S1–S3 points from chan-kit. Input trades need time (+ optional side)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "trades": {
                "type": "array",
                "items": {"type": "object"},
                "description": "List of {time, side?, price?} trades",
            },
            "trades_json": {
                "type": "string",
                "description": "Alternative JSON string for trades list",
            },
            "period": {"type": "string", "default": "day"},
            "window_seconds": {"type": "integer"},
            "limit": {"type": "integer", "default": 800},
        },
        "required": ["symbol"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        try:
            trades = kwargs.get("trades")
            if trades is None and kwargs.get("trades_json"):
                trades = json.loads(str(kwargs["trades_json"]))
            if not isinstance(trades, list) or not trades:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "trades or trades_json (non-empty list) is required",
                    },
                    ensure_ascii=False,
                )
            result = align_trades_to_chan(
                symbol=str(kwargs["symbol"]),
                trades=trades,
                period=str(kwargs.get("period", "day")),
                window_seconds=kwargs.get("window_seconds"),
                limit=int(kwargs.get("limit", 800) or 800),
            )
            return json.dumps({"status": "ok", **result}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
