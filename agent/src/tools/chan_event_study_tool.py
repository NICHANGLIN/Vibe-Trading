"""Event study: forward returns after Chanlun signal kinds (from chart candles)."""

from __future__ import annotations

import json
from statistics import mean, median
from typing import Any

from src.agent.tools import BaseTool
from src.tools.chan_client import fetch_chan_chart, normalize_period, to_ashare_code


def _forward_return(closes: list[float], idx: int, horizon: int) -> float | None:
    j = idx + horizon
    if j >= len(closes) or closes[idx] <= 0:
        return None
    return (closes[j] / closes[idx]) - 1.0


def run_chan_event_study(
    *,
    symbol: str,
    period: str = "day",
    kinds: list[str] | None = None,
    horizons: list[int] | None = None,
    limit: int = 800,
) -> dict[str, Any]:
    """Compute average forward returns after each matching signal kind."""
    code = to_ashare_code(symbol)
    period_norm = normalize_period(period)
    wanted = {str(k).upper() for k in (kinds or ["B1", "B2", "B3", "S1", "S2", "S3"])}
    horiz = [int(h) for h in (horizons or [1, 5, 10, 20])]
    horiz = [h for h in horiz if h > 0]
    if not horiz:
        raise ValueError("horizons must contain positive integers")

    payload = fetch_chan_chart(symbol=code, period=period_norm, limit=limit)
    candles = payload.get("candles") or []
    if len(candles) < 30:
        raise RuntimeError("not enough candles for event study")

    time_to_idx = {int(c["time"]): i for i, c in enumerate(candles)}
    closes = [float(c["close"]) for c in candles]

    by_kind: dict[str, dict[str, list[float]]] = {}
    events: list[dict[str, Any]] = []

    for tr in payload.get("trades") or []:
        kind = str(tr.get("kind") or "").upper()
        if kind not in wanted:
            continue
        if (tr.get("level") or "") == "bi" or kind.startswith("BI_"):
            continue
        t = int(tr.get("time") or 0)
        idx = time_to_idx.get(t)
        if idx is None:
            # nearest bar at or after signal time
            idx = next((i for i, c in enumerate(candles) if int(c["time"]) >= t), None)
        if idx is None:
            continue

        side = str(tr.get("side") or ("buy" if kind.startswith("B") else "sell"))
        rets: dict[str, float | None] = {}
        for h in horiz:
            raw = _forward_return(closes, idx, h)
            if raw is None:
                rets[f"h{h}"] = None
                continue
            # For sells, report short-friendly return (negative price move is good)
            signed = -raw if side == "sell" else raw
            rets[f"h{h}"] = signed
            by_kind.setdefault(kind, {}).setdefault(f"h{h}", []).append(signed)

        events.append(
            {
                "time": t,
                "kind": kind,
                "side": side,
                "price": tr.get("price"),
                "returns": rets,
            }
        )

    summary: dict[str, Any] = {}
    for kind, horizons_map in by_kind.items():
        summary[kind] = {"count": 0}
        for key, vals in horizons_map.items():
            if not vals:
                continue
            summary[kind]["count"] = max(summary[kind]["count"], len(vals))
            summary[kind][key] = {
                "n": len(vals),
                "mean": mean(vals),
                "median": median(vals),
                "hit_rate": sum(1 for v in vals if v > 0) / len(vals),
            }

    return {
        "symbol": code,
        "name": payload.get("name"),
        "period": period_norm,
        "horizons": horiz,
        "event_count": len(events),
        "summary": summary,
        "events_tail": events[-30:],
        "note": (
            "Forward returns use chan-kit candles. Sell-side returns are "
            "sign-flipped so positive = favorable for the short."
        ),
    }


class ChanEventStudyTool(BaseTool):
    """Alpha-style event study around Chanlun signal timestamps."""

    name = "chan_event_study"
    description = (
        "Event study on Chanlun signals from chan-kit: average/median forward "
        "returns at horizons (default 1/5/10/20 bars) by B1–B3/S1–S3 kind. "
        "Useful before or alongside Alpha Zoo factor work."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "period": {"type": "string", "default": "day"},
            "kinds": {"type": "array", "items": {"type": "string"}},
            "horizons": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Forward bar horizons",
            },
            "limit": {"type": "integer", "default": 800},
        },
        "required": ["symbol"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        try:
            kinds = kwargs.get("kinds")
            if isinstance(kinds, str):
                kinds = [k.strip() for k in kinds.split(",") if k.strip()]
            horizons = kwargs.get("horizons")
            if isinstance(horizons, str):
                horizons = [int(x.strip()) for x in horizons.split(",") if x.strip()]
            result = run_chan_event_study(
                symbol=str(kwargs["symbol"]),
                period=str(kwargs.get("period", "day")),
                kinds=kinds,
                horizons=horizons,
                limit=int(kwargs.get("limit", 800) or 800),
            )
            return json.dumps({"status": "ok", **result}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
