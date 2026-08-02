"""Tests for get_index_constituents and chan_scan(index=...)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pandas as pd

from src.tools.chan_scan_tool import ChanScanTool
from src.tools.index_constituents_tool import (
    IndexConstituentsTool,
    fetch_index_constituents,
    resolve_index_code,
)


def test_resolve_index_aliases():
    assert resolve_index_code("中证A500") == "000510"
    assert resolve_index_code("a500") == "000510"
    assert resolve_index_code("000510.SH") == "000510"
    assert resolve_index_code("沪深300") == "000300"
    assert resolve_index_code("399300.SZ") == "000300"
    assert resolve_index_code("中证500") == "000905"
    assert resolve_index_code("") is None
    assert resolve_index_code("not-an-index") is None


def test_fetch_index_constituents_normalizes_symbols():
    df = pd.DataFrame(
        [
            {
                "日期": "2026-07-31",
                "指数代码": "000510",
                "指数名称": "中证A500",
                "成分券代码": "000001",
                "成分券名称": "平安银行",
                "交易所": "深圳证券交易所",
            },
            {
                "日期": "2026-07-31",
                "指数代码": "000510",
                "指数名称": "中证A500",
                "成分券代码": "600519",
                "成分券名称": "贵州茅台",
                "交易所": "上海证券交易所",
            },
        ]
    )
    with patch("akshare.index_stock_cons_csindex", return_value=df):
        payload = fetch_index_constituents("中证A500", include_names=True)

    assert payload["ok"] is True
    assert payload["index"] == "000510"
    assert payload["symbols"] == ["000001.SZ", "600519.SH"]
    assert payload["count"] == 2
    assert payload["constituents"][0]["name"] == "平安银行"

    with patch("akshare.index_stock_cons_csindex", return_value=df):
        compact = fetch_index_constituents("中证A500", include_names=False)
    assert "constituents" not in compact
    assert compact["symbols"] == ["000001.SZ", "600519.SH"]


def test_tool_execute_json_envelope():
    df = pd.DataFrame(
        [
            {
                "日期": "2026-07-31",
                "指数代码": "000300",
                "指数名称": "沪深300",
                "成分券代码": "600036",
                "成分券名称": "招商银行",
                "交易所": "上海证券交易所",
            }
        ]
    )
    with patch("akshare.index_stock_cons_csindex", return_value=df):
        raw = IndexConstituentsTool().execute(index="沪深300", include_names=False)

    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["symbols"] == ["600036.SH"]
    assert "constituents" not in payload


def test_chan_scan_accepts_index(monkeypatch):
    fake_universe = {
        "ok": True,
        "index": "000510",
        "index_name": "中证A500",
        "as_of": "2026-07-31",
        "source": "csindex via akshare",
        "count": 2,
        "symbols": ["000001.SZ", "600519.SH"],
    }

    def _fake_fetch(index, *, limit=800, include_names=True):
        assert "A500" in index or index == "000510" or "中证" in index
        return {**fake_universe, "symbols": fake_universe["symbols"][:limit]}

    def _fake_chart(*, symbol, period, limit):
        return {
            "symbol": symbol,
            "name": symbol,
            "candles": [{"time": i} for i in range(10)],
            "trades": [
                {"kind": "B1", "level": "signal", "time": 9, "side": "buy", "price": 10}
            ],
            "quote": {},
        }

    monkeypatch.setattr(
        "src.tools.index_constituents_tool.fetch_index_constituents",
        _fake_fetch,
    )
    with patch("src.tools.chan_scan_tool.fetch_chan_chart", side_effect=_fake_chart):
        raw = ChanScanTool().execute(
            index="中证A500",
            kinds=["B1"],
            max_symbols=50,
            lookback_bars=5,
            workers=2,
        )

    payload = json.loads(raw)
    assert payload["status"] == "ok"
    assert payload["scanned"] == 2
    assert payload["match_count"] == 2
    assert payload["universe"]["index"] == "000510"
