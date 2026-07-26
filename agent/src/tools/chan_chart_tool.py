"""Fetch Chanlun structure/signals from chan-kit (NICHANGLIN/czsc), not upstream waditu examples."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.tools.chan_client import (
    fetch_chan_chart,
    normalize_period,
    summarize_chart,
    to_ashare_code,
)


class ChanChartTool(BaseTool):
    """Read Chanlun chart features from the local chan-kit API."""

    name = "chan_chart"
    description = (
        "Fetch Chanlun (缠论) analysis from the chan-kit / NICHANGLIN/czsc service: "
        "fenxing, bi, zhongshu, and B1/S1/B2/S2/B3/S3 trade points. "
        "Use this instead of the bundled waditu-czsc chanlun skill when "
        "analyzing A-shares for the Chanlun Web product. "
        "Requires CHAN_API_BASE_URL (default http://127.0.0.1:8000) and "
        "usually CHAN_SERVICE_TOKEN."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "A-share code, e.g. 000001, 000001.SZ, 600519.SH",
            },
            "market": {
                "type": "string",
                "description": "Optional sz/sh when symbol is 6 digits",
            },
            "period": {
                "type": "string",
                "description": "day / 60m / 30m (aliases 1D / 1H accepted)",
                "default": "day",
            },
            "limit": {
                "type": "integer",
                "description": "Bars to load (50-2000)",
                "default": 300,
            },
            "include_raw": {
                "type": "boolean",
                "description": "Include truncated raw fx/bi/zs/trades arrays",
                "default": False,
            },
        },
        "required": ["symbol"],
    }
    repeatable = True
    is_readonly = True

    @classmethod
    def check_available(cls) -> bool:
        return True

    def execute(self, **kwargs: Any) -> str:
        try:
            symbol = to_ashare_code(str(kwargs.get("symbol", "")), kwargs.get("market"))
            period = normalize_period(str(kwargs.get("period", "day")))
            limit = int(kwargs.get("limit", 300) or 300)
            limit = max(50, min(2000, limit))
            payload = fetch_chan_chart(
                symbol=symbol,
                period=period,
                limit=limit,
            )
            summary = summarize_chart(payload)
            out: dict[str, Any] = {"status": "ok", "summary": summary}
            if bool(kwargs.get("include_raw")):
                out["raw"] = {
                    "fx": (payload.get("fx") or [])[-40:],
                    "bi": (payload.get("bi") or [])[-40:],
                    "zs": (payload.get("zs") or [])[-12:],
                    "trades": (payload.get("trades") or [])[-40:],
                }
            return json.dumps(out, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
