"""HTTP client for the chan-kit (NICHANGLIN/czsc) Chanlun chart API."""

from __future__ import annotations

from typing import Any

import requests

from src.config.accessor import get_env_config

_DEFAULT_TIMEOUT = 60

_PERIOD_TO_INTERVAL = {
    "day": "1D",
    "60m": "1H",
    "30m": "30m",
    "1D": "1D",
    "1H": "1H",
    "1d": "1D",
    "1h": "1H",
}

_INTERVAL_TO_PERIOD = {
    "1D": "day",
    "1d": "day",
    "day": "day",
    "1H": "60m",
    "1h": "60m",
    "60m": "60m",
    "4H": "60m",
    "30m": "30m",
}


def chan_api_base_url() -> str:
    """Return configured chan-api base URL (no trailing slash)."""
    return get_env_config().data.chan_api_base_url.rstrip("/")


def chan_service_token() -> str:
    """Return Bearer token for chan-api service calls."""
    return get_env_config().data.chan_service_token.strip()


def normalize_period(period_or_interval: str) -> str:
    """Map Vibe interval aliases to chan-api period (day / 60m / 30m)."""
    key = str(period_or_interval or "day").strip()
    return _INTERVAL_TO_PERIOD.get(key, key if key in {"day", "60m", "30m"} else "day")


def period_to_interval(period: str) -> str:
    """Map chan-api period to Vibe backtest interval."""
    return _PERIOD_TO_INTERVAL.get(str(period or "day").strip(), "1D")


def to_ashare_code(symbol: str, market: str | None = None) -> str:
    """Normalize a 6-digit or dotted A-share code to ``NNNNNN.SZ|SH``."""
    raw = str(symbol or "").strip().upper()
    if "." in raw:
        code, suf = raw.split(".", 1)
        suf = suf.upper()
        if suf in {"SZ", "SH"} and code.isdigit() and len(code) == 6:
            return f"{code}.{suf}"
    code = raw.split(".")[0]
    if not code.isdigit() or len(code) != 6:
        raise ValueError(f"invalid A-share symbol: {symbol!r}")
    m = (market or "").strip().lower()
    if m in {"sh", "sz"}:
        return f"{code}.{m.upper()}"
    if code.startswith(("5", "6", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


def split_ashare_code(code: str) -> tuple[str, str]:
    """Split ``000001.SZ`` into ``(000001, sz)``."""
    normalized = to_ashare_code(code)
    symbol, suf = normalized.split(".", 1)
    return symbol, suf.lower()


def fetch_chan_chart(
    *,
    symbol: str,
    market: str | None = None,
    period: str = "day",
    limit: int = 300,
    source: str = "auto",
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """GET /api/chart from chan-kit and return the JSON payload.

    Raises:
        ValueError: invalid symbol/period.
        RuntimeError: HTTP or transport failure.
    """
    code, mkt = split_ashare_code(to_ashare_code(symbol, market))
    period_norm = normalize_period(period)
    if period_norm not in {"day", "60m", "30m"}:
        raise ValueError(f"unsupported period: {period!r}")

    url = f"{chan_api_base_url()}/api/chart"
    headers: dict[str, str] = {}
    token = chan_service_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.get(
            url,
            params={
                "symbol": code,
                "market": mkt,
                "period": period_norm,
                "limit": int(limit),
                "source": source,
            },
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"chan-api request failed: {exc}") from exc

    if resp.status_code == 401:
        raise RuntimeError(
            "chan-api unauthorized: set CHAN_SERVICE_TOKEN to a valid "
            "CHAN_SERVICE_TOKEN on the chan-kit server"
        )
    if resp.status_code >= 400:
        detail = resp.text[:500]
        raise RuntimeError(f"chan-api HTTP {resp.status_code}: {detail}")

    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("chan-api returned non-object JSON")
    return data


def summarize_chart(payload: dict[str, Any]) -> dict[str, Any]:
    """Compress a chart payload for LLM/tool consumption."""
    trades = payload.get("trades") or []
    signal_trades = [t for t in trades if (t.get("level") or "") != "bi"]
    bi_trades = [t for t in trades if (t.get("level") or "") == "bi" or str(t.get("kind", "")).startswith("BI_")]
    zs = payload.get("zs") or []
    last_zs = zs[-1] if zs else None
    recent_signals = signal_trades[-8:] if signal_trades else []

    return {
        "symbol": payload.get("symbol"),
        "name": payload.get("name"),
        "market": payload.get("market"),
        "period": payload.get("period"),
        "freq": payload.get("freq"),
        "source": payload.get("source"),
        "quote": payload.get("quote"),
        "stats": payload.get("stats"),
        "warning": payload.get("warning"),
        "fx_count": len(payload.get("fx") or []),
        "bi_count": len(payload.get("bi") or []),
        "zs_count": len(zs),
        "last_zhongshu": last_zs,
        "recent_signal_trades": recent_signals,
        "signal_trade_count": len(signal_trades),
        "bi_trade_count": len(bi_trades),
        "latest_signal": recent_signals[-1] if recent_signals else None,
        "engine": "NICHANGLIN/czsc via chan-kit",
    }
