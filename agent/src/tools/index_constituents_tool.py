"""Fetch A-share index constituents (free, no API key).

Primary source is the China Securities Index (中证指数) official constituent
file, accessed via ``akshare.index_stock_cons_csindex``. That path needs no
token and covers CSI A500 / CSI 300 / CSI 500 and other published indexes.

Use this before ``chan_scan`` when the user names an index universe instead of
an explicit symbol list. ``search_symbol`` only resolves the index ticker
itself — it does **not** return constituents.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

_MAX_RETURN = 800
_DEFAULT_LIMIT = 800

# Common Chinese / English aliases → CSI published index code (no suffix).
_INDEX_ALIASES: dict[str, str] = {
    "中证a500": "000510",
    "中证ａ500": "000510",
    "a500": "000510",
    "csi a500": "000510",
    "csi_a500": "000510",
    "csia500": "000510",
    "000510": "000510",
    "000510.sh": "000510",
    "930050": "000510",
    "沪深300": "000300",
    "沪深３００": "000300",
    "hs300": "000300",
    "csi300": "000300",
    "csi 300": "000300",
    "000300": "000300",
    "000300.sh": "000300",
    "399300": "000300",
    "399300.sz": "000300",
    "中证500": "000905",
    "中证５００": "000905",
    "zz500": "000905",
    "csi500": "000905",
    "csi 500": "000905",
    "000905": "000905",
    "000905.sh": "000905",
    "中证1000": "000852",
    "中证１０００": "000852",
    "csi1000": "000852",
    "csi 1000": "000852",
    "000852": "000852",
    "000852.sh": "000852",
    "上证50": "000016",
    "上证５０": "000016",
    "sz50": "000016",
    "000016": "000016",
    "000016.sh": "000016",
    "科创50": "000688",
    "科创５０": "000688",
    "000688": "000688",
    "000688.sh": "000688",
    "上证指数": "000001",
    "上证综指": "000001",
    "000001.sh": "000001",
    # Popular index ETFs → tracking index
    "510300": "000300",
    "510300.sh": "000300",
    "159919": "000300",
    "563360": "000510",
    "563360.sh": "000510",
    "512050": "000510",
    "159338": "000510",
    "510500": "000905",
    "512100": "000852",
    "510050": "000016",
    "588000": "000688",
    "159915": "399006",
}

_KNOWN_INDEX_CODES = {
    "000510",
    "000300",
    "399300",
    "000905",
    "000852",
    "000016",
    "000688",
    "000001",
    "399006",
    "399001",
    "000903",
    "000906",
    "000985",
}

_EXCHANGE_SUFFIX: dict[str, str] = {
    "上海证券交易所": "SH",
    "深圳证券交易所": "SZ",
    "北京证券交易所": "BJ",
    "shanghai stock exchange": "SH",
    "shenzhen stock exchange": "SZ",
    "beijing stock exchange": "BJ",
    "sse": "SH",
    "szse": "SZ",
    "bse": "BJ",
}


def _looks_like_etf_or_fund(code: str) -> bool:
    if code in _KNOWN_INDEX_CODES:
        return False
    return code.startswith(("15", "16", "50", "51", "56", "58"))


def _resolve_etf_underlying(etf_code: str) -> str | None:
    """Resolve ETF → tracking index via Eastmoney FundMNBasicInformation."""
    try:
        import urllib.request

        url = (
            "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNBasicInformation"
            f"?FCODE={etf_code}&deviceid=Wap&plat=Wap&product=EFund&version=2.0.0"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        data = payload.get("Datas") or {}
        index_code = str(data.get("INDEXCODE") or "").strip()
        if not re.fullmatch(r"\d{6}", index_code):
            return None
        return "000300" if index_code == "399300" else index_code
    except Exception as exc:  # noqa: BLE001
        logger.info("ETF underlying lookup failed for %s: %s", etf_code, exc)
        return None


def resolve_index_code(query: str) -> str | None:
    """Map a free-text index/ETF name/code to a bare CSI index code.

    Args:
        query: Index/ETF name or ticker (e.g. ``"中证A500"``, ``"510300"``).

    Returns:
        Six-digit index code, or ``None`` when unresolvable.
    """
    raw = (query or "").strip()
    if not raw:
        return None
    key = re.sub(r"\s+", " ", raw).lower()
    key = key.replace("．", ".").replace("。", ".")
    if key in _INDEX_ALIASES:
        return _INDEX_ALIASES[key]

    # Bare 6-digit code, optionally with market suffix.
    m = re.fullmatch(r"(\d{6})(?:\.(?:sh|sz|csi))?", key)
    if m:
        code = m.group(1)
        if code == "399300":
            return "000300"
        if code in _KNOWN_INDEX_CODES or not _looks_like_etf_or_fund(code):
            return code
        return _resolve_etf_underlying(code) or code

    # Fuzzy contains for common names (avoid short keys matching too broadly).
    for alias, code in _INDEX_ALIASES.items():
        if len(alias) >= 3 and alias in key:
            return code
    return None


def _suffix_from_exchange(exchange: Any) -> str | None:
    if exchange is None:
        return None
    text = str(exchange).strip().lower()
    if text in _EXCHANGE_SUFFIX:
        return _EXCHANGE_SUFFIX[text]
    for name, suffix in _EXCHANGE_SUFFIX.items():
        if name in text:
            return suffix
    return None


def _suffix_from_code(code: str) -> str:
    """Best-effort A-share suffix when exchange column is missing."""
    if code.startswith(("5", "6", "9")):
        return "SH"
    if code.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def _normalize_row(row: dict[str, Any]) -> dict[str, str] | None:
    code = str(row.get("成分券代码") or row.get("品种代码") or row.get("code") or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return None
    name = str(row.get("成分券名称") or row.get("品种名称") or row.get("name") or "").strip()
    exchange = row.get("交易所") or row.get("交易所英文名称")
    suffix = _suffix_from_exchange(exchange) or _suffix_from_code(code)
    item = {"symbol": f"{code}.{suffix}", "code": code, "name": name}
    weight = row.get("权重")
    if weight is not None and weight != "":
        try:
            item["weight"] = float(weight)
        except (TypeError, ValueError):
            pass
    return item


def fetch_index_constituents(
    index: str,
    *,
    limit: int = _DEFAULT_LIMIT,
    include_names: bool = True,
) -> dict[str, Any]:
    """Fetch constituents for an A-share index.

    Args:
        index: Free-text index name or code.
        limit: Max constituents to return (1–800).
        include_names: When False, return only symbol strings.

    Returns:
        Result dict with ``ok`` / ``symbols`` / metadata, or ``ok=False`` error.
    """
    index_code = resolve_index_code(index)
    if not index_code:
        return {
            "ok": False,
            "error": (
                f"unrecognized index: {index!r}. "
                "Try 中证A500 / 000510, 沪深300 / 000300, 中证500 / 000905."
            ),
        }

    try:
        import akshare as ak
    except ImportError as exc:
        return {"ok": False, "error": f"akshare not installed: {exc}"}

    try:
        df = ak.index_stock_cons_csindex(symbol=index_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("csindex constituents failed for %s: %s", index_code, exc)
        return {"ok": False, "error": f"failed to fetch constituents for {index_code}: {exc}"}

    if df is None or getattr(df, "empty", True):
        return {"ok": False, "error": f"empty constituent list for {index_code}"}

    rows = df.to_dict(orient="records")
    constituents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = _normalize_row(row if isinstance(row, dict) else {})
        if item is None or item["symbol"] in seen:
            continue
        seen.add(item["symbol"])
        constituents.append(item)

    if not constituents:
        return {"ok": False, "error": f"no parseable constituents for {index_code}"}

    cap = max(1, min(int(limit), _MAX_RETURN))
    truncated = len(constituents) > cap
    constituents = constituents[:cap]
    symbols = [c["symbol"] for c in constituents]

    index_name = ""
    as_of = ""
    if rows:
        first = rows[0] if isinstance(rows[0], dict) else {}
        index_name = str(first.get("指数名称") or "").strip()
        as_of = str(first.get("日期") or "").strip()

    payload: dict[str, Any] = {
        "ok": True,
        "market": "cn",
        "source": "csindex via akshare",
        "index": index_code,
        "index_name": index_name or index.strip(),
        "query": index.strip(),
        "as_of": as_of or None,
        "count": len(symbols),
        "truncated": truncated,
        "symbols": symbols,
    }
    if include_names:
        payload["constituents"] = [
            {"symbol": c["symbol"], "name": c.get("name") or None} for c in constituents
        ]
    return payload


class IndexConstituentsTool(BaseTool):
    """Return A-share index constituent symbols for scanning / research."""

    name = "get_index_constituents"
    description = (
        "Fetch Chinese A-share index constituents (free, no API key) from the "
        "official CSI / 中证指数 list via akshare. Accepts index names/codes "
        "and common tracking ETF codes (e.g. 510300→沪深300, 563360→中证A500). "
        "Use when the user asks to screen an index/ETF universe such as "
        "中证A500 (000510), 沪深300 (000300), 中证500 (000905). "
        "Returns symbols in project form (000001.SZ, 600519.SH). "
        "IMPORTANT: search_symbol only resolves the index ticker itself and "
        "does NOT return constituents. For screening, prefer "
        "chan_scan(index=...) directly; use this tool when you need the list. "
        'Example: get_index_constituents(index="563360").'
    )
    parameters = {
        "type": "object",
        "properties": {
            "index": {
                "type": "string",
                "description": (
                    "Index or ETF name/code, e.g. '中证A500', '000510', "
                    "'沪深300', '510300', '563360', '中证500'."
                ),
            },
            "limit": {
                "type": "integer",
                "default": _DEFAULT_LIMIT,
                "description": f"Max constituents to return (1-{_MAX_RETURN}).",
            },
            "include_names": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Include name alongside each symbol. Default false so large "
                    "indexes stay under the tool-result size limit; use "
                    "chan_scan(index=...) for screening instead."
                ),
            },
        },
        "required": ["index"],
    }
    repeatable = True
    is_readonly = True

    @classmethod
    def check_available(cls) -> bool:
        try:
            import akshare  # noqa: F401
        except ImportError:
            return False
        return True

    def execute(self, **kwargs: Any) -> str:
        index = kwargs.get("index")
        if not isinstance(index, str) or not index.strip():
            return json.dumps(
                {"ok": False, "error": "index must be a non-empty string"},
                ensure_ascii=False,
            )
        limit = kwargs.get("limit", _DEFAULT_LIMIT)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            return json.dumps(
                {"ok": False, "error": "limit must be a positive integer"},
                ensure_ascii=False,
            )
        include_names = kwargs.get("include_names", False)
        if not isinstance(include_names, bool):
            include_names = False
        result = fetch_index_constituents(
            index.strip(),
            limit=limit,
            include_names=include_names,
        )
        return json.dumps(result, ensure_ascii=False)
