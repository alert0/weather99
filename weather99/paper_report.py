"""Readable Chinese summaries for the Windows launcher."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .domain import SHANGHAI

REASONS = {
    "no_eligible_combo": "没有组合同时满足入场条件",
    "one_sided_book": "盘口缺少买价或卖价",
    "ask_out_of_range": "卖价超出允许范围",
    "blended_probability_below_threshold": "综合胜率未达到门槛",
    "market_mode_not_in_combo": "组合未包含市场最看好的温度档",
    "net_edge_below_threshold": "扣除费用后的优势不足",
    "quote_stale_or_future": "盘口时间无效或已过期",
    "market_mode_quote_stale": "市场热门档盘口已过期",
    "spread_too_wide": "买卖价差过大",
    "crossed_book": "盘口买卖价格异常",
    "market_closed": "市场已关闭",
    "extreme_model_disagreement": "各天气模型预测分歧过大",
    "modal_temperature_gap": "模型与市场最可能温度差距过大",
    "daily_loss_limit": "已达到当日亏损上限",
    "consecutive_loss_limit": "已达到连续亏损上限",
    "drawdown_halt": "账户回撤触发暂停",
    "risk_state_missing": "缺少账户风控数据",
    "risk_state_wrong_day": "账户风控日期不匹配",
    "signal_not_confirmed": "信号尚未通过连续确认",
    "below_book_min_order_size": "份额低于市场最小订单要求",
    "insufficient_ask_depth": "卖盘可成交份额不足",
    "position_budget_exceeded": "投入超过单次预算",
    "depth_adjusted_edge_below_threshold": "按实际盘口计算后优势不足",
    "market_already_traded": "该日期市场已建仓",
    "max_open_positions": "未结算持仓已达到上限",
    "settlement_rules_unsupported": "市场结算规则尚未支持，暂停入场",
    "settlement_evidence_missing": "缺少结算源当日有效报文，暂停入场",
    "settlement_observation_stale": "结算源最新有效报文已超过一小时，暂停入场",
    "existing_positions_unpriced": "已有持仓缺少有效估值",
    "insufficient_cash": "可用现金不足",
}


def local_time(value):
    if not value:
        return "暂无"
    try:
        return datetime.fromisoformat(value).astimezone(SHANGHAI).strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "时间无效"


def explain(reason):
    code = str(reason).rsplit(":", 1)[-1]
    return REASONS.get(code, "数据或执行检查未通过，请查看本地日志")


def evaluation_context(row, run_dir):
    """Read original inputs and immutable configuration; never substitute today's settings."""
    raw, signal, config = {}, {}, {}
    try:
        raw = json.loads(Path(row["snapshot"]).read_text(encoding="utf-8"))
    except (KeyError, OSError, ValueError):
        pass
    try:
        uri = (Path(run_dir) / "audit.sqlite3").resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as connection:
            record = connection.execute(
                "SELECT payload FROM evaluations WHERE id=?", (row.get("signal_id"),)
            ).fetchone()
            if record:
                signal = json.loads(record[0])
                artifact = connection.execute(
                    "SELECT payload FROM artifacts WHERE id=?", (signal["config_version"],)
                ).fetchone()
                if artifact:
                    config = json.loads(artifact[0])
    except (KeyError, OSError, ValueError, sqlite3.Error):
        pass
    return raw, signal, config


def evaluation_lines(row, context):
    raw, signal, config = context
    buckets = {b["token_id"]: b for b in raw.get("buckets", [])}

    def label(key):
        text = buckets.get(key, {}).get("bucket_label", "未知温度档")
        return text.replace(" or below", "及以下").replace(" or higher", "及以上")

    filled = (row.get("fill") or {}).get("status") == "paper_filled"
    statuses = {
        "rejected": "未入场",
        "blocked": "数据检查未通过",
        "confirmed_research_signal": "信号已确认，尚未成交",
        "pending_confirmation": "等待再次确认",
    }
    status = "已模拟买入" if filled else statuses.get(row.get("status"), "已评估，尚未成交")
    lines = [f"  {row['target_date']}：{status}"]
    if raw.get("collected_at"):
        lines.append(f"    评估数据时间：{local_time(raw['collected_at'])}")
    center = (row.get("forecast") or {}).get("center_c")
    if center is not None:
        lines.append(f"    模型最高温预测中心：{center:.1f} ℃")
    if signal.get("market_mode"):
        lines.append(f"    市场最看好的温度档：{label(signal['market_mode'])}")
    reasons = row.get("reasons") or []
    if reasons:
        conclusion = "；".join(dict.fromkeys(explain(r) for r in reasons))
        count = len(signal.get("candidates", []))
        suffix = f"（共检查 {count} 个组合）" if count else ""
        lines.append(f"    整体结论：{conclusion}{suffix}")
    candidate = row.get("selected") or row.get("best_candidate")
    if candidate:
        title = (
            "通过条件的组合"
            if row.get("selected")
            else "被拒候选示例（按模型净优势排序首位，非建议买入）"
        )
        names = "、".join(label(key) for key in candidate["buckets"])
        lines.append(f"    {title}：{names}")
        probability = candidate.get("blended_probability")
        floor = config.get("entry", {}).get("probability_threshold", {}).get(row.get("layer"))
        if probability is not None:
            comparison = (
                f"，入场门槛 {floor:.1%}（{'达标' if probability + 1e-12 >= floor else '未达标'}）"
                if floor is not None
                else "（该次门槛记录不可用）"
            )
            lines.append(f"      综合概率估计：{probability:.1%}{comparison}")
        for reason in candidate.get("reasons") or []:
            key, _, code = reason.rpartition(":")
            code = code or reason
            bucket = buckets.get(key, {})
            if code == "one_sided_book":
                missing = "和".join(
                    name
                    for field, name in [("bid", "买价"), ("ask", "卖价")]
                    if bucket.get(field) is None
                )
                detail = f"{label(key)}：缺少{missing}" if bucket else "候选中的部分档位缺少报价"
            elif code == "ask_out_of_range" and bucket.get("ask") is not None:
                assumptions = config.get("research_assumptions", {})
                low, high = assumptions.get("ask_min"), assumptions.get("ask_max")
                allowed = (
                    f"，允许范围 ${low:g}～${high:g}"
                    if low is not None and high is not None
                    else ""
                )
                detail = f"{label(key)}：卖价 ${bucket['ask']:g}{allowed}"
            elif code == "blended_probability_below_threshold" and probability is not None:
                continue  # Already shown with the actual threshold above.
            elif code == "market_mode_not_in_combo" and signal.get("market_mode"):
                detail = f"该组合未包含策略要求的热门档 {label(signal['market_mode'])}"
            else:
                prefix = f"{label(key)}：" if bucket else ""
                detail = prefix + explain(reason)
            lines.append(f"      未通过：{detail}")
    fill_reason = (row.get("fill") or {}).get("reason")
    if not filled and fill_reason and fill_reason != "signal_not_confirmed":
        lines.append(f"    模拟成交检查：{explain(fill_reason)}")
    lines.append("")
    return lines


def render_status(state, run_dir=None):
    lines = ["\n========== Weather99 模拟盘 ==========", "地点：上海浦东机场  |  时间：北京时间"]
    phase = state.get("phase")
    if phase == "not_started":
        return "\n".join(lines + ["状态：尚未启动，请在菜单选择 1。"])
    phases = {"collecting": "正在采集和评估", "waiting": "等待下一轮", "stopped": "已停止"}
    health = (
        "正常" if state.get("healthy") else "已停止" if phase == "stopped" else "异常：心跳已过期"
    )
    lines.append(f"运行：{health}  |  {phases.get(phase, '状态未知')}")
    interval = state.get("interval_seconds")
    if interval:
        lines.append(f"采集间隔：{interval / 60:g} 分钟")
    lines.append(f"最近心跳：{local_time(state.get('heartbeat'))}")
    if phase == "waiting":
        lines.append(f"下轮计划：{local_time(state.get('next_scan_at'))}")
    mode = (
        "四模型等权基线（尚未历史校准）"
        if state.get("model_mode") == "equal_weight_baseline"
        else "历史训练模型"
    )
    lines += [f"模型：{mode}", "交易方式：本地模拟", ""]
    account = state.get("account") or {}
    if account:
        lines.append(
            f"资金：现金 ${account['cash_usd']:.2f}  |  总资产 ${account['equity_usd']:.2f}"
        )
        lines.append(
            f"当日已实现盈亏：${account['daily_realized_usd']:+.2f}  |  未结算持仓：{account['open_positions']} 个"
        )
        if not account.get("valuation_complete", True):
            lines.append("注意：部分持仓缺少有效报价，估值不完整，暂停新增仓位。")
    weather = state.get("latest_weather") or {}
    summary = weather.get("summary") or {}
    lines.append("")
    if weather:
        lines.append(
            f"和风采集：{'完整' if weather.get('complete') else '不完整'}  |  有效采样点：{summary.get('complete_points', 0)}/{summary.get('expected_points', 5)}"
        )
        times = "、".join(local_time(t) for t in weather.get("valid_times", [])) or "暂无"
        lines.append(f"天气数据有效时间：{times}")
        for key, label in [("current_c", "五点当前温度"), ("forecast_max_c", "五点今日最高温预报")]:
            values = summary.get(key) or {}
            if values.get("min") is not None and values.get("max") is not None:
                lines.append(f"{label}：{values['min']:.1f}～{values['max']:.1f} ℃")
        if weather.get("errors"):
            lines.append("天气采集有错误，详细记录见 runtime/paper/weather/。")
    else:
        lines.append("和风天气：等待本轮采集结果")
    lines += ["", "最近评估："]
    for row in state.get("last_cycle") or []:
        context = evaluation_context(row, run_dir) if run_dir else ({}, {}, {})
        lines.extend(evaluation_lines(row, context))
    if not state.get("last_cycle"):
        lines.append("  本轮尚未完成，稍后再查看。")
    lines += ["", "详细日志和原始数据：runtime/paper/", "====================================="]
    return "\n".join(lines)
