---
name: chanlun-cl-czsc
description: 使用 chan-kit（引擎 NICHANGLIN/czsc）微服务做缠论分析（分型/笔/中枢/一二三类买卖点）。优先于内置 waditu-czsc 的 chanlun skill。提供 chan_chart、chan_backtest、chan_scan、chan_event_study、chan_shadow_align、chan_schedule_scan 工具。
category: strategy
---
# 缠论（chan-kit / NICHANGLIN/czsc）

> Skill id 仍为 `chanlun-cl-czsc`（兼容旧对话与 Web 提示词）；计算引擎为 [`NICHANGLIN/czsc`](https://github.com/NICHANGLIN/czsc)，经 chan-kit HTTP API 暴露。

## 何时使用

分析 **A 股缠论结构**、解释当前买卖点、回测信号、选股扫描、事件研究或交易复盘时，**必须**使用本 skill 与下列工具。

不要调用内置 `chanlun` skill 里的 waditu `czsc` 示例代码——信号定义可能与划线产品不一致。  
消歧说明：`/opt/chan/chan-kit/docs/knowledge-base/chanlun-skills.md`。

## 工具一览

| 工具 | 用途 |
|------|------|
| `chan_chart` | 读取分型/笔/中枢/B1–B3/S1–S3 |
| `chan_backtest` | 将 trades[] 映射为 SignalEngine 并回测 |
| `chan_scan` | 股票池扫描近期信号（可传 `index=` 自动取成分股） |
| `get_index_constituents` | **拉取指数成分股**（中证A500/沪深300/中证500 等，免费无需 key） |
| `chan_event_study` | 信号后 N 根 K 线收益事件研究 |
| `chan_shadow_align` | 实盘/Shadow 成交与买卖点对齐 |
| `chan_schedule_scan` | 定时扫描（配合 scheduler + IM） |

### 指数选股（必读）

用户说「从中证A500 / 沪深300 筛选一买」时：

1. **禁止**只用 `search_symbol`——它只能解析指数代码本身，**不会**返回成分股。
2. **优先一次调用**：`chan_scan(index="中证A500", kinds=["B1"], period="day", max_symbols=500)`（内部并行拉图，勿拆成十几次小批量）。
3. `chan_scan` 返回的 `matches[]`（含 symbol/name/kind/time/price）就是完整结论依据；**禁止**再对每只股票 `search_symbol` / web_search「核实身份」。
4. 若用户只要 Top N：直接按 `matches` 排序/挑选后用中文回答，不要重扫。
5. 扫描约 1–2 分钟；不要中途改用 web_search。

## 环境变量

```bash
CHAN_API_BASE_URL=http://127.0.0.1:8000
CHAN_SERVICE_TOKEN=<与 chan-kit 相同的服务令牌>
```

## 推荐对话流程

1. `load_skill("chanlun-cl-czsc")`
2. `chan_chart(symbol=..., period="day")` 获取结构摘要
3. 用缠论术语解释最近中枢与买卖点（一/二/三类）
4. 需要验证时：`chan_backtest` 或 `chan_event_study`
5. 指数选股：`chan_scan(index="中证A500", kinds=["B1"], max_symbols=500)`
6. 定时：`chan_schedule_scan(schedule="86400000", symbols=[...])`

## 周期映射

| chan-kit | Vibe interval |
|----------|---------------|
| day | 1D |
| 60m | 1H |
| 30m | 30m |

## 与 Shadow / Alpha

- Shadow：先 `analyze_trade_journal` / 解析成交，再 `chan_shadow_align`
- Alpha：用 `chan_event_study` 做事件窗，再用 `alpha_bench` / `factor_analysis` 做因子横评
