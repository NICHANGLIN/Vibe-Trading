"""Create a scheduled research job that runs chan_scan on a cron/interval."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import requests

from src.agent.tools import BaseTool
from src.config.accessor import get_env_config


def _default_prompt(symbols: list[str], period: str, kinds: list[str], lookback: int) -> str:
    syms = ", ".join(symbols)
    kind_s = ",".join(kinds)
    return (
        "你是缠论选股助手。请调用工具 chan_scan，参数："
        f"symbols=[{syms}], period={period!r}, kinds=[{kind_s}], "
        f"lookback_bars={lookback}。"
        "汇总命中标的的最新信号种类、价格与名称；若无命中请明确说明。"
        "不要使用内置 waditu chanlun 示例，必须用 chan_scan / chan_chart（chan-kit / NICHANGLIN/czsc）。"
        "若 IM 通道已启用，用简洁中文汇报结果。"
    )


def create_chan_scan_schedule(
    *,
    schedule: str,
    symbols: list[str],
    period: str = "day",
    kinds: list[str] | None = None,
    lookback_bars: int = 5,
    prompt: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Persist a scheduled research job via local store (preferred) or HTTP."""
    kinds = [str(k).upper() for k in (kinds or ["B2", "B3"])]
    lookback = max(1, int(lookback_bars))
    final_prompt = prompt or _default_prompt(symbols, period, kinds, lookback)
    jid = job_id or f"chan-scan-{uuid.uuid4().hex[:8]}"

    # Prefer in-process store (works when tool runs inside the API/agent process).
    try:
        from src.scheduled_research.models import (
            JobStatus,
            ScheduledResearchJob,
            validate_schedule,
        )
        from src.scheduled_research.store import ScheduledResearchJobStore

        validate_schedule(schedule)
        now_ms = int(time.time() * 1000)
        job = ScheduledResearchJob(
            id=jid,
            prompt=final_prompt,
            schedule=schedule,
            next_run_at=now_ms,
            status=JobStatus.PENDING,
            created_at=now_ms,
            config={
                "chan_scan": {
                    "symbols": symbols,
                    "period": period,
                    "kinds": kinds,
                    "lookback_bars": lookback,
                }
            },
        )
        store = ScheduledResearchJobStore()
        store.upsert(job)
        return {
            "mode": "local_store",
            "job": {
                "id": job.id,
                "schedule": job.schedule,
                "next_run_at": job.next_run_at,
                "status": job.status.value if hasattr(job.status, "value") else str(job.status),
                "prompt": job.prompt,
                "config": job.config,
            },
            "hint": (
                "Enable executor with VIBE_TRADING_ENABLE_SCHEDULER=1 and keep "
                "the API server running. Connect Feishu/WeCom channels for IM push."
            ),
        }
    except Exception as local_exc:
        # Fall back to HTTP against the local Vibe API.
        cfg = get_env_config()
        base = (cfg.api.vibe_trading_api_url or "http://127.0.0.1:8899").rstrip("/")
        # Historical default in schema points at :8000; prefer 8899 for Vibe API.
        if base.endswith(":8000"):
            base = "http://127.0.0.1:8899"
        headers = {"Content-Type": "application/json"}
        key = (cfg.api.api_auth_key or cfg.api.vibe_trading_api_key or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            resp = requests.post(
                f"{base}/scheduled-runs",
                headers=headers,
                json={
                    "id": jid,
                    "prompt": final_prompt,
                    "schedule": schedule,
                    "config": {
                        "chan_scan": {
                            "symbols": symbols,
                            "period": period,
                            "kinds": kinds,
                            "lookback_bars": lookback,
                        }
                    },
                },
                timeout=30,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")
            return {
                "mode": "http",
                "job": resp.json(),
                "local_store_error": str(local_exc),
                "hint": "Scheduler must be enabled on the API server for jobs to fire.",
            }
        except Exception as http_exc:
            raise RuntimeError(
                f"failed to create schedule (local: {local_exc}; http: {http_exc})"
            ) from http_exc


class ChanScheduleScanTool(BaseTool):
    """Register a cron/interval job that scans Chanlun signals."""

    name = "chan_schedule_scan"
    description = (
        "Create a scheduled research job that periodically runs chan_scan "
        "(Chanlun watchlist). schedule is interval-ms or a 5-field cron. "
        "Requires VIBE_TRADING_ENABLE_SCHEDULER=1 for execution; IM channels "
        "deliver results when configured."
    )
    parameters = {
        "type": "object",
        "properties": {
            "schedule": {
                "type": "string",
                "description": "e.g. '0 15 * * 1-5' or interval ms '3600000'",
            },
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Watchlist symbols",
            },
            "period": {"type": "string", "default": "day"},
            "kinds": {"type": "array", "items": {"type": "string"}},
            "lookback_bars": {"type": "integer", "default": 5},
            "prompt": {"type": "string", "description": "Optional custom prompt"},
            "job_id": {"type": "string"},
        },
        "required": ["schedule", "symbols"],
    }
    repeatable = True
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        try:
            symbols = kwargs.get("symbols") or []
            if isinstance(symbols, str):
                symbols = [s.strip() for s in symbols.replace(";", ",").split(",") if s.strip()]
            if not symbols:
                return json.dumps(
                    {"status": "error", "error": "symbols is required"},
                    ensure_ascii=False,
                )
            kinds = kwargs.get("kinds")
            if isinstance(kinds, str):
                kinds = [k.strip() for k in kinds.split(",") if k.strip()]
            result = create_chan_scan_schedule(
                schedule=str(kwargs["schedule"]),
                symbols=list(symbols),
                period=str(kwargs.get("period", "day")),
                kinds=kinds,
                lookback_bars=int(kwargs.get("lookback_bars", 5) or 5),
                prompt=kwargs.get("prompt"),
                job_id=kwargs.get("job_id"),
            )
            return json.dumps({"status": "ok", **result}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
