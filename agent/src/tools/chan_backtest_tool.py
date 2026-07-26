"""Map chan-kit trades[] into a sandboxed SignalEngine and run a backtest."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from src.agent.progress import emit_progress
from src.agent.tools import BaseTool
from src.tools.backtest_tool import run_backtest
from src.tools.chan_client import (
    fetch_chan_chart,
    normalize_period,
    period_to_interval,
    to_ashare_code,
)
from src.tools.path_utils import safe_run_dir

_SIGNAL_KINDS_LONG = ("B1", "B2", "B3")
_SIGNAL_KINDS_SHORT = ("S1", "S2", "S3")

_ENGINE_TEMPLATE = '''"""Auto-generated Chanlun signal engine from chan-kit trades.

Symbol: {code}
Period: {period}
Kinds long: {kinds_long}
Kinds short: {kinds_short}
Engine: NICHANGLIN/czsc via chan-kit (not upstream waditu skill examples)
"""

import pandas as pd

_LONG_KEYS = {long_keys!r}
_SHORT_KEYS = {short_keys!r}


def _bar_key(ts) -> str:
    t = pd.Timestamp(ts)
    if getattr(t, "tzinfo", None) is not None:
        t = t.tz_convert("Asia/Shanghai").tz_localize(None)
    # Daily bars match on date; intraday keep minute precision.
    if t.hour == 0 and t.minute == 0 and t.second == 0:
        return t.strftime("%Y-%m-%d")
    return t.strftime("%Y-%m-%d %H:%M")


class SignalEngine:
    """Replay embedded Chanlun buy/sell point timestamps as position signals."""

    def __init__(self):
        pass

    def generate(self, data_map):
        result = {{}}
        for code, df in data_map.items():
            signal = pd.Series(0, index=df.index, dtype=int)
            for dt in df.index:
                key = _bar_key(dt)
                if key in _LONG_KEYS:
                    signal.loc[dt] = 1
                elif key in _SHORT_KEYS:
                    signal.loc[dt] = -1
            result[code] = signal
        return result
'''


def _trade_key(trade: dict[str, Any], period: str) -> str | None:
    t = trade.get("time")
    if t is None:
        return None
    try:
        # chan-kit times are Unix seconds (Asia/Shanghai wall clock)
        ts = pd_timestamp_from_unix(int(t), period)
    except Exception:
        return None
    return ts


def pd_timestamp_from_unix(unix_s: int, period: str) -> str:
    import pandas as pd

    t = pd.Timestamp(unix_s, unit="s", tz="Asia/Shanghai").tz_localize(None)
    if period == "day":
        return t.strftime("%Y-%m-%d")
    return t.strftime("%Y-%m-%d %H:%M")


def _filter_kinds(kinds: list[str] | None) -> tuple[set[str], set[str]]:
    if not kinds:
        return set(_SIGNAL_KINDS_LONG), set(_SIGNAL_KINDS_SHORT)
    wanted = {str(k).upper() for k in kinds}
    long_k = wanted & set(_SIGNAL_KINDS_LONG)
    short_k = wanted & set(_SIGNAL_KINDS_SHORT)
    if not long_k and not short_k:
        raise ValueError(f"no valid signal kinds in {kinds!r}; use B1/B2/B3/S1/S2/S3")
    return long_k, short_k


def build_chan_backtest_run(
    *,
    symbol: str,
    period: str = "day",
    start_date: str,
    end_date: str,
    kinds: list[str] | None = None,
    limit: int = 800,
    source: str = "auto",
    initial_cash: float = 1_000_000,
) -> dict[str, Any]:
    """Fetch chan trades, write run dir, optionally ready for ``backtest``."""
    code = to_ashare_code(symbol)
    period_norm = normalize_period(period)
    long_kinds, short_kinds = _filter_kinds(kinds)

    emit_progress("fetch", message=f"fetching chan-kit chart for {code} {period_norm}")
    payload = fetch_chan_chart(symbol=code, period=period_norm, limit=limit)
    trades = payload.get("trades") or []

    long_keys: set[str] = set()
    short_keys: set[str] = set()
    used: list[dict[str, Any]] = []
    for tr in trades:
        kind = str(tr.get("kind") or "").upper()
        if kind not in long_kinds and kind not in short_kinds:
            continue
        if (tr.get("level") or "") == "bi" or kind.startswith("BI_"):
            continue
        key = _trade_key(tr, period_norm)
        if not key:
            continue
        if kind in long_kinds:
            long_keys.add(key)
        else:
            short_keys.add(key)
        used.append(
            {
                "time": tr.get("time"),
                "kind": kind,
                "side": tr.get("side"),
                "price": tr.get("price"),
                "key": key,
            }
        )

    if not long_keys and not short_keys:
        raise RuntimeError(
            f"no matching Chanlun signal trades for kinds "
            f"{sorted(long_kinds | short_kinds)} on {code}"
        )

    run_id = f"chan_{code.replace('.', '_')}_{period_norm}_{uuid.uuid4().hex[:8]}"
    run_dir = Path.home() / ".vibe-trading" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "code").mkdir(parents=True, exist_ok=True)
    # Ensure path is inside allowed roots before writing engine that backtest will load.
    run_path = safe_run_dir(str(run_dir))

    interval = period_to_interval(period_norm)
    config = {
        "codes": [code],
        "start_date": start_date,
        "end_date": end_date,
        "source": source,
        "interval": interval,
        "engine": "daily",
        "initial_cash": initial_cash,
        "meta": {
            "strategy": "chanlun-cl-czsc",
            "period": period_norm,
            "kinds_long": sorted(long_kinds),
            "kinds_short": sorted(short_kinds),
            "trade_count": len(used),
            "created_at": int(time.time()),
        },
    }
    (run_path / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    engine_src = _ENGINE_TEMPLATE.format(
        code=code,
        period=period_norm,
        kinds_long=sorted(long_kinds),
        kinds_short=sorted(short_kinds),
        long_keys=sorted(long_keys),
        short_keys=sorted(short_keys),
    )
    (run_path / "code" / "signal_engine.py").write_text(engine_src, encoding="utf-8")
    (run_path / "chan_trades.json").write_text(
        json.dumps(used, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "run_dir": str(run_path),
        "config": config,
        "trade_count": len(used),
        "long_keys": sorted(long_keys),
        "short_keys": sorted(short_keys),
        "name": payload.get("name"),
    }


class ChanBacktestTool(BaseTool):
    """Build a Chanlun SignalEngine from chan-kit and run the backtest."""

    name = "chan_backtest"
    description = (
        "Backtest Chanlun buy/sell points from chan-kit (NICHANGLIN/czsc): fetch "
        "trades[], embed them into a sandboxed SignalEngine, and run the "
        "Vibe backtest engine. Prefer signal kinds B1/B2/B3/S1/S2/S3."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "A-share code"},
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            "period": {"type": "string", "default": "day"},
            "kinds": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional subset e.g. [\"B2\",\"S2\"]",
            },
            "limit": {"type": "integer", "default": 800},
            "source": {"type": "string", "default": "auto"},
            "run_only_prepare": {
                "type": "boolean",
                "default": False,
                "description": "If true, only write run_dir without executing",
            },
        },
        "required": ["symbol", "start_date", "end_date"],
    }
    repeatable = True
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        try:
            kinds = kwargs.get("kinds")
            if isinstance(kinds, str):
                kinds = [k.strip() for k in kinds.split(",") if k.strip()]
            prepared = build_chan_backtest_run(
                symbol=str(kwargs["symbol"]),
                period=str(kwargs.get("period", "day")),
                start_date=str(kwargs["start_date"]),
                end_date=str(kwargs["end_date"]),
                kinds=kinds,
                limit=int(kwargs.get("limit", 800) or 800),
                source=str(kwargs.get("source", "auto") or "auto"),
            )
            if bool(kwargs.get("run_only_prepare")):
                return json.dumps(
                    {"status": "ok", "prepared": True, **prepared},
                    ensure_ascii=False,
                )
            emit_progress("simulate", message=f"running backtest in {prepared['run_dir']}")
            bt = json.loads(run_backtest(prepared["run_dir"]))
            return json.dumps(
                {"status": bt.get("status", "error"), "prepared": prepared, "backtest": bt},
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
