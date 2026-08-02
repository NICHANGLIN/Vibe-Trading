"""Unit tests for chan-kit integration tools."""

from __future__ import annotations

import json
from unittest.mock import patch

import pandas as pd

from src.tools.chan_backtest_tool import ChanBacktestTool, build_chan_backtest_run
from src.tools.chan_chart_tool import ChanChartTool
from src.tools.chan_client import (
    normalize_period,
    period_to_interval,
    summarize_chart,
    to_ashare_code,
)
from src.tools.chan_event_study_tool import ChanEventStudyTool, run_chan_event_study
from src.tools.chan_scan_tool import ChanScanTool
from src.tools.chan_shadow_align_tool import ChanShadowAlignTool, align_trades_to_chan


def test_symbol_and_period_helpers():
    assert to_ashare_code("000001", "sz") == "000001.SZ"
    assert to_ashare_code("600519") == "600519.SH"
    assert to_ashare_code("000001.sz") == "000001.SZ"
    assert normalize_period("1D") == "day"
    assert normalize_period("1H") == "60m"
    assert period_to_interval("day") == "1D"
    assert period_to_interval("60m") == "1H"


def test_summarize_chart():
    summary = summarize_chart(
        {
            "symbol": "000001",
            "name": "平安银行",
            "period": "day",
            "fx": [1, 2],
            "bi": [1],
            "zs": [{"zg": 10, "zd": 9}],
            "trades": [
                {"kind": "B2", "level": "signal", "time": 1, "side": "buy"},
                {"kind": "BI_B", "level": "bi", "time": 2, "side": "buy"},
            ],
            "stats": {"bars": 100},
        }
    )
    assert summary["fx_count"] == 2
    assert summary["signal_trade_count"] == 1
    assert summary["latest_signal"]["kind"] == "B2"
    assert "czsc" in summary["engine"] and "chan-kit" in summary["engine"]


def _fake_chart(**_kwargs):
    return {
        "symbol": "000001",
        "name": "平安银行",
        "market": "sz",
        "period": "day",
        "candles": [
            {"time": 1700000000 + i * 86400, "open": 10, "high": 11, "low": 9, "close": 10 + i * 0.01, "vol": 1}
            for i in range(40)
        ],
        "fx": [],
        "bi": [],
        "zs": [],
        "trades": [
            {
                "time": 1700000000 + 10 * 86400,
                "kind": "B2",
                "side": "buy",
                "price": 10.1,
                "level": "signal",
                "label": "二买",
            },
            {
                "time": 1700000000 + 20 * 86400,
                "kind": "S2",
                "side": "sell",
                "price": 10.2,
                "level": "signal",
                "label": "二卖",
            },
        ],
        "stats": {"bars": 40},
    }


def test_chan_chart_tool_ok():
    with patch("src.tools.chan_chart_tool.fetch_chan_chart", side_effect=_fake_chart):
        out = json.loads(ChanChartTool().execute(symbol="000001.SZ", period="day"))
    assert out["status"] == "ok"
    assert out["summary"]["name"] == "平安银行"


def test_chan_scan_tool_match():
    with patch("src.tools.chan_scan_tool.fetch_chan_chart", side_effect=_fake_chart):
        out = json.loads(
            ChanScanTool().execute(
                symbols=["000001.SZ"],
                period="day",
                kinds=["B2"],
                lookback_bars=40,
            )
        )
    assert out["status"] == "ok"
    assert out["match_count"] == 1
    assert out["matches"][0]["kind"] == "B2"


def test_chan_event_study():
    with patch("src.tools.chan_event_study_tool.fetch_chan_chart", side_effect=_fake_chart):
        out = run_chan_event_study(symbol="000001.SZ", kinds=["B2"], horizons=[1, 5])
    assert out["event_count"] >= 1
    assert "B2" in out["summary"]


def test_chan_shadow_align():
    with patch("src.tools.chan_shadow_align_tool.fetch_chan_chart", side_effect=_fake_chart):
        out = align_trades_to_chan(
            symbol="000001.SZ",
            trades=[{"time": 1700000000 + 10 * 86400, "side": "buy", "price": 10.1}],
            window_seconds=86400,
        )
        tool_out = json.loads(
            ChanShadowAlignTool().execute(
                symbol="000001.SZ",
                trades_json=json.dumps(
                    [{"time": 1700000000 + 10 * 86400, "side": "buy"}],
                    ensure_ascii=False,
                ),
            )
        )
    assert out["aligned_count"] == 1
    assert tool_out["status"] == "ok"
    assert tool_out["aligned_count"] == 1


def test_build_chan_backtest_prepare(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # path_utils uses Path.home(); also patch safe_run_dir to accept our dir
    with patch("src.tools.chan_backtest_tool.fetch_chan_chart", side_effect=_fake_chart), patch(
        "src.tools.chan_backtest_tool.safe_run_dir",
        side_effect=lambda p: __import__("pathlib").Path(p),
    ), patch(
        "src.tools.chan_backtest_tool.Path.home",
        return_value=home,
    ):
        prepared = build_chan_backtest_run(
            symbol="000001.SZ",
            period="day",
            start_date="2023-01-01",
            end_date="2024-01-01",
            kinds=["B2", "S2"],
        )
    run_dir = __import__("pathlib").Path(prepared["run_dir"])
    assert (run_dir / "config.json").is_file()
    engine = (run_dir / "code" / "signal_engine.py").read_text(encoding="utf-8")
    assert "class SignalEngine" in engine
    assert "B2" in json.dumps(prepared)

    # Engine must be importable / executable without network
    ns: dict = {}
    exec(compile(engine, str(run_dir / "code" / "signal_engine.py"), "exec"), ns)
    eng = ns["SignalEngine"]()
    idx = pd.date_range("2023-11-01", periods=5, freq="D")
    df = pd.DataFrame(
        {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        index=idx,
    )
    signals = eng.generate({"000001.SZ": df})
    assert "000001.SZ" in signals


def test_chan_backtest_tool_prepare_only(monkeypatch, tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    with patch("src.tools.chan_backtest_tool.fetch_chan_chart", side_effect=_fake_chart), patch(
        "src.tools.chan_backtest_tool.safe_run_dir",
        side_effect=lambda p: __import__("pathlib").Path(p),
    ), patch(
        "src.tools.chan_backtest_tool.Path.home",
        return_value=home,
    ):
        out = json.loads(
            ChanBacktestTool().execute(
                symbol="000001",
                start_date="2023-01-01",
                end_date="2024-06-01",
                run_only_prepare=True,
                kinds=["B2", "S2"],
            )
        )
    assert out["status"] == "ok"
    assert out.get("prepared") is True
    assert out.get("run_dir")


def test_chan_event_study_tool_wrapper():
    with patch("src.tools.chan_event_study_tool.fetch_chan_chart", side_effect=_fake_chart):
        out = json.loads(ChanEventStudyTool().execute(symbol="000001.SZ"))
    assert out["status"] == "ok"
