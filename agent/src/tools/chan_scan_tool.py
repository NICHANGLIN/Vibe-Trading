"""Scan a symbol universe for recent Chanlun signal kinds via chan-kit."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Soft cap when expanding an index into symbols for one scan call.
_MAX_INDEX_SCAN = 500
_DEFAULT_SCAN_WORKERS = 8
_DEFAULT_SCAN_LIMIT = 150
_DEFAULT_MAX_SYMBOLS = 50
_DEFAULT_INDEX_MAX_SYMBOLS = 500


def _match_symbol(
    *,
    code: str,
    period_norm: str,
    wanted: set[str],
    lookback: int,
    limit: int,
) -> tuple[str, dict[str, Any] | None, dict[str, str] | None]:
    """Scan one symbol. Returns ``(code, match|None, error|None)``."""
    try:
        payload = fetch_chan_chart(symbol=code, period=period_norm, limit=limit)
    except Exception as exc:  # noqa: BLE001 - collect per-symbol errors
        return code, None, {"symbol": code, "error": str(exc)}

    candles = payload.get("candles") or []
    if not candles:
        return code, None, None
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
    if not hits:
        return code, None, None
    latest = hits[-1]
    return (
        code,
        {
            "symbol": code,
            "name": payload.get("name"),
            "kind": latest.get("kind"),
            "time": latest.get("time"),
            "price": latest.get("price"),
            "hit_count": len(hits),
        },
        None,
    )


def scan_chan_universe(
    *,
    symbols: list[str],
    period: str = "day",
    kinds: list[str] | None = None,
    lookback_bars: int = 5,
    limit: int = _DEFAULT_SCAN_LIMIT,
    workers: int = _DEFAULT_SCAN_WORKERS,
) -> dict[str, Any]:
    """Return symbols whose recent bars contain matching signal kinds."""
    period_norm = normalize_period(period)
    wanted = {str(k).upper() for k in (kinds or ["B1", "B2", "B3", "S1", "S2", "S3"])}
    lookback = max(1, min(60, int(lookback_bars)))
    chart_limit = max(30, min(800, int(limit)))
    worker_n = max(1, min(16, int(workers)))

    codes: list[str] = []
    errors: list[dict[str, str]] = []
    for raw in symbols:
        try:
            codes.append(to_ashare_code(raw))
        except ValueError as exc:
            errors.append({"symbol": str(raw), "error": str(exc)})

    matches: list[dict[str, Any]] = []
    total = len(codes)
    emit_progress("scan", current=0, total=total, message=f"0/{total}")

    if total == 0:
        return {
            "period": period_norm,
            "kinds": sorted(wanted),
            "lookback_bars": lookback,
            "scanned": 0,
            "match_count": 0,
            "matches": [],
            "errors": errors,
            "workers": worker_n,
        }

    done = 0
    with ThreadPoolExecutor(max_workers=min(worker_n, total)) as pool:
        futures = [
            pool.submit(
                _match_symbol,
                code=code,
                period_norm=period_norm,
                wanted=wanted,
                lookback=lookback,
                limit=chart_limit,
            )
            for code in codes
        ]
        for fut in as_completed(futures):
            code, match, err = fut.result()
            done += 1
            if err:
                errors.append(err)
            if match:
                matches.append(match)
            if done == total or done % 5 == 0:
                emit_progress(
                    "scan",
                    current=done,
                    total=total,
                    message=f"{done}/{total} last={code}",
                )

    # Stable order: follow input symbol order for matches.
    order = {c: i for i, c in enumerate(codes)}
    matches.sort(key=lambda m: order.get(str(m.get("symbol")), 10**9))

    # Keep the LLM-facing payload under the agent tool-result limit (~10k chars).
    # Full error lists for a 500-name index blow past that and cause retry loops.
    error_preview = errors[:5]
    return {
        "period": period_norm,
        "kinds": sorted(wanted),
        "lookback_bars": lookback,
        "scanned": total,
        "match_count": len(matches),
        "matches": matches,
        "error_count": len(errors),
        "errors": error_preview,
        "workers": worker_n,
    }


def _resolve_scan_symbols(
    *,
    symbols: Any,
    index: Any,
    max_symbols: int,
) -> tuple[list[str], dict[str, Any] | None]:
    """Resolve explicit symbols and/or an index name into a scan list.

    Returns:
        ``(symbols, index_meta)``. ``index_meta`` is set when ``index`` was used.
    """
    index_meta: dict[str, Any] | None = None
    resolved: list[str] = []

    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.replace(";", ",").split(",") if s.strip()]
    if isinstance(symbols, list) and symbols:
        resolved = [str(s).strip() for s in symbols if str(s).strip()]

    if isinstance(index, str) and index.strip():
        from src.tools.index_constituents_tool import fetch_index_constituents

        payload = fetch_index_constituents(
            index.strip(),
            limit=max(1, min(int(max_symbols), _MAX_INDEX_SCAN)),
            include_names=False,
        )
        if not payload.get("ok"):
            raise ValueError(payload.get("error") or f"failed to resolve index {index!r}")
        index_symbols = list(payload.get("symbols") or [])
        index_meta = {
            "index": payload.get("index"),
            "index_name": payload.get("index_name"),
            "as_of": payload.get("as_of"),
            "source": payload.get("source"),
            "constituent_count": payload.get("count"),
        }
        if not resolved:
            resolved = index_symbols
        else:
            allow = {s.upper() for s in index_symbols}
            filtered = [s for s in resolved if s.upper() in allow]
            resolved = filtered or resolved

    if not resolved:
        resolved = list(_DEFAULT_UNIVERSE)
    if len(resolved) > max_symbols:
        resolved = resolved[:max_symbols]
        if index_meta is not None:
            index_meta["truncated_to"] = max_symbols
    return resolved, index_meta


class ChanScanTool(BaseTool):
    """Universe scan for recent Chanlun buy/sell points."""

    name = "chan_scan"
    description = (
        "Scan A-share symbols for recent Chanlun signals (B1/B2/B3/S1/S2/S3) "
        "using chan-kit / NICHANGLIN/czsc (parallel). Prefer ONE call with "
        "index='中证A500' and max_symbols=500 rather than many small batches. "
        "一买 = kinds=['B1']. Emits scan progress while running."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Symbols to scan; defaults to a small hot list when index omitted",
            },
            "index": {
                "type": "string",
                "description": (
                    "Optional index name/code (e.g. 中证A500, 000510, 沪深300). "
                    "When set, constituents are fetched automatically."
                ),
            },
            "period": {"type": "string", "default": "day"},
            "kinds": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Signal kinds to match (B1=一买, B2=二买, ...)",
            },
            "lookback_bars": {
                "type": "integer",
                "default": 5,
                "description": "Only count signals in the last N bars",
            },
            "limit": {
                "type": "integer",
                "default": _DEFAULT_SCAN_LIMIT,
                "description": "Bars per symbol chart fetch (default 150).",
            },
            "max_symbols": {
                "type": "integer",
                "default": _DEFAULT_MAX_SYMBOLS,
                "description": (
                    "Max symbols to scan in one call (1-500). When index is set "
                    "and this is omitted, defaults to 500 for a full-index scan."
                ),
            },
            "workers": {
                "type": "integer",
                "default": _DEFAULT_SCAN_WORKERS,
                "description": "Parallel chart workers (1-16, default 8).",
            },
        },
        "required": [],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        try:
            index = kwargs.get("index")
            has_index = isinstance(index, str) and bool(index.strip())
            if "max_symbols" in kwargs and kwargs.get("max_symbols") is not None:
                max_symbols = int(kwargs.get("max_symbols") or _DEFAULT_MAX_SYMBOLS)
            else:
                max_symbols = (
                    _DEFAULT_INDEX_MAX_SYMBOLS if has_index else _DEFAULT_MAX_SYMBOLS
                )
            max_symbols = max(1, min(_MAX_INDEX_SCAN, max_symbols))
            symbols, index_meta = _resolve_scan_symbols(
                symbols=kwargs.get("symbols"),
                index=index,
                max_symbols=max_symbols,
            )
            kinds = kwargs.get("kinds")
            if isinstance(kinds, str):
                kinds = [k.strip() for k in kinds.split(",") if k.strip()]
            workers = int(kwargs.get("workers", _DEFAULT_SCAN_WORKERS) or _DEFAULT_SCAN_WORKERS)
            result = scan_chan_universe(
                symbols=list(symbols),
                period=str(kwargs.get("period", "day")),
                kinds=kinds,
                lookback_bars=int(kwargs.get("lookback_bars", 5) or 5),
                limit=int(kwargs.get("limit", _DEFAULT_SCAN_LIMIT) or _DEFAULT_SCAN_LIMIT),
                workers=workers,
            )
            payload: dict[str, Any] = {"status": "ok", **result}
            if index_meta:
                payload["universe"] = index_meta
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
