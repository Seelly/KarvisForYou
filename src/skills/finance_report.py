# -*- coding: utf-8 -*-
"""
Skill: finance.monthly
每月自动生成财务月报：收支总览、支出分类、资产变动、趋势、LLM 洞察。

触发方式：
  - 定时：每月 8 号 20:00（Scheduler → /system action=finance_monthly_report）
  - 手动：用户说「生成财务月报」「上个月财务报告」

V12 移植：handler 签名 (params, state, ctx)，IO 通过 ctx.IO。
"""
import json
import calendar
from datetime import datetime, timedelta
from finance_utils import (
    load_finance_data, parse_date, parse_amount,
    filter_bills, summarize_bills,
    group_snapshots_by_date, calc_snapshot_summary, compare_snapshots,
    format_currency, normalize_date_str
)
from log_utils import BEIJING_TZ, get_logger

logger = get_logger(__name__)


def execute(params, state, ctx):
    """
    生成财务月报。

    params:
        month: str — 可选，YYYY-MM 格式，默认上个月
    """
    month_str = (params.get("month") or "").strip()

    now = datetime.now(BEIJING_TZ)

    if not month_str:
        # 默认上个月
        first_this = now.replace(day=1)
        last_month = first_this - timedelta(days=1)
        month_str = last_month.strftime("%Y-%m")

    try:
        year, month = int(month_str[:4]), int(month_str[5:7])
    except (ValueError, IndexError):
        return {"success": False, "reply": f"月份格式错误：{month_str}"}

    period_str = f"{year}年{month}月"
    logger.info("[finance.monthly] 生成财务月报: %s", period_str)

    # 1. 先自动导入最新 iCost 数据
    import_result = _auto_import(ctx)

    # 2. 读取财务数据
    data = load_finance_data(ctx, force=True)
    if not data:
        return {"success": False, "reply": "无法读取财务数据，请检查 finance_data.json"}

    bills = data.get("data", {}).get("收支账单", [])
    snapshots = data.get("data", {}).get("资产快照", [])
    salary_records = data.get("data", {}).get("工资与收入", [])

    # 3. 收支统计
    _, days_in_month = calendar.monthrange(year, month)
    start_dt = datetime(year, month, 1)
    end_dt = datetime(year, month, days_in_month, 23, 59, 59)

    month_bills = filter_bills(bills, start_date=start_dt, end_date=end_dt)
    bill_summary = summarize_bills(month_bills)

    # 上月数据（用于环比）
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    _, prev_days = calendar.monthrange(prev_year, prev_month)
    prev_start = datetime(prev_year, prev_month, 1)
    prev_end = datetime(prev_year, prev_month, prev_days, 23, 59, 59)
    prev_bills = filter_bills(bills, start_date=prev_start, end_date=prev_end)
    prev_summary = summarize_bills(prev_bills)

    # 4. 资产快照对比
    snapshot_comparison = None
    snapshot_current = None
    if snapshots:
        grouped = group_snapshots_by_date(snapshots)
        dates = list(grouped.keys())

        # 找当月或最近的快照
        month_prefix = f"{year}-{month:02d}"
        current_date = None
        previous_date = None

        for d in dates:
            if d.startswith(month_prefix) and current_date is None:
                current_date = d
            elif current_date and previous_date is None:
                previous_date = d
                break

        # 如果当月没有快照，取最近的
        if not current_date and dates:
            current_date = dates[0]
            if len(dates) > 1:
                previous_date = dates[1]

        if current_date:
            snapshot_current = {
                "date": current_date,
                "summary": calc_snapshot_summary(grouped[current_date])
            }

        if current_date and previous_date:
            snapshot_comparison = compare_snapshots(
                grouped[current_date], grouped[previous_date]
            )
            snapshot_comparison["current_date"] = current_date
            snapshot_comparison["previous_date"] = previous_date

    # 5. 工资数据
    month_salary = None
    for s in salary_records:
        s_date = s.get("日期", "")
        normalized = normalize_date_str(s_date)
        if normalized and normalized.startswith(f"{year}-{month:02d}"):
            month_salary = s
            break

    # 6. 近 3 月趋势
    trend_data = _calc_trend(bills, year, month, months=3)

    # 7. 构建报告上下文，调 LLM 生成洞察
    report_ctx = _build_report_context(
        period_str, bill_summary, prev_summary,
        snapshot_current, snapshot_comparison,
        month_salary, trend_data, import_result
    )

    # 8. 调 LLM 生成洞察
    insights = _ai_generate_insights(report_ctx)

    # 9. 构建 Markdown 报告
    report_md = _build_report_markdown(
        period_str, month_str, bill_summary, prev_summary,
        snapshot_current, snapshot_comparison,
        month_salary, trend_data, insights, import_result
    )

    # 10. 写入 Obsidian
    file_path = f"{ctx.finance_reports_dir}/财务月报-{month_str}.md"
    ok = ctx.IO.write_text(file_path, report_md)

    if not ok:
        logger.warning("[finance.monthly] 报告写入失败: %s", file_path)

    # 11. 构建企微推送摘要
    wechat_summary = _build_wechat_summary(
        period_str, bill_summary, snapshot_current,
        snapshot_comparison, insights
    )

    logger.info("[finance.monthly] 月报生成完成: %s", period_str)

    return {
        "success": True,
        "reply": wechat_summary,
    }


def _auto_import(ctx):
    """月报生成前自动导入 iCost 数据"""
    try:
        from skills.finance_import import handle_import
        result = handle_import({}, {}, ctx)
        if result.get("success"):
            agent_ctx = result.get("agent_context", {})
            new_count = agent_ctx.get("total_new_records", 0)
            if new_count > 0:
                logger.info("[finance.monthly] 自动导入: 新增 %s 条", new_count)
                return {"imported": True, "new_count": new_count}
        return {"imported": False, "new_count": 0}
    except Exception as e:
        logger.exception("[finance.monthly] 自动导入异常: %s", e)
        return {"imported": False, "new_count": 0, "error": str(e)}


def _calc_trend(bills, year, month, months=3):
    """计算近 N 个月的收支趋势"""
    trend = []
    for i in range(months - 1, -1, -1):
        m = month - i
        y = year
        while m <= 0:
            m += 12
            y -= 1
        _, days = calendar.monthrange(y, m)
        s = datetime(y, m, 1)
        e = datetime(y, m, days, 23, 59, 59)
        month_bills = filter_bills(bills, start_date=s, end_date=e)
        summary = summarize_bills(month_bills)
        trend.append({
            "month": f"{y}-{m:02d}",
            "label": f"{y}年{m}月",
            "expense": summary["total_expense"],
            "income": summary["total_income"],
            "balance": summary["balance"],
            "savings_rate": summary["savings_rate"],
            "record_count": summary["record_count"],
        })
    return trend


def _build_report_context(period_str, bill_summary, prev_summary,
                          snapshot_current, snapshot_comparison,
                          month_salary, trend_data, import_result):
    """构建传给 LLM 的结构化上下文"""
    report_ctx = {
        "period": period_str,
        "income_expense": {
            "total_income": bill_summary["total_income"],
            "total_expense": bill_summary["total_expense"],
            "balance": bill_summary["balance"],
            "savings_rate": bill_summary["savings_rate"],
            "expense_top5": bill_summary["expense_by_category"][:5],
            "record_count": bill_summary["record_count"],
        },
        "prev_month": {
            "total_income": prev_summary["total_income"],
            "total_expense": prev_summary["total_expense"],
            "balance": prev_summary["balance"],
            "savings_rate": prev_summary["savings_rate"],
        },
    }

    if snapshot_current:
        report_ctx["assets"] = snapshot_current["summary"]
        report_ctx["assets"]["snapshot_date"] = snapshot_current["date"]

    if snapshot_comparison:
        report_ctx["asset_comparison"] = {
            "current_date": snapshot_comparison["current_date"],
            "previous_date": snapshot_comparison["previous_date"],
            "asset_change": snapshot_comparison["asset_change"],
            "asset_change_pct": snapshot_comparison["asset_change_pct"],
            "disposable_change": snapshot_comparison["disposable_change"],
            "disposable_change_pct": snapshot_comparison["disposable_change_pct"],
            "class_changes": snapshot_comparison["class_changes"][:5],
        }

    if month_salary:
        report_ctx["salary"] = month_salary

    report_ctx["trend"] = trend_data

    return report_ctx


def _ai_generate_insights(report_ctx):
    """调 LLM 生成财务洞察"""
    try:
        from brain import call_llm
        import prompts

        ctx_str = json.dumps(report_ctx, ensure_ascii=False, indent=2)

        messages = [
            {"role": "system", "content": prompts.FINANCE_REPORT_SYSTEM},
            {"role": "user", "content": f"{prompts.FINANCE_REPORT_USER}\n\n{ctx_str}"}
        ]

        response = call_llm(messages, model_tier="think", max_tokens=1200, temperature=0.7)

        if not response:
            return None

        # 解析 JSON
        text = response.strip()

        # 剥离 thinking 模式的 <think>...</think> 标签
        if "<think>" in text:
            think_end = text.find("</think>")
            if think_end >= 0:
                text = text[think_end + len("</think>"):].strip()
            else:
                text = text.replace("<think>", "").strip()

        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()
        try:
            return json.loads(text)
        except Exception as e:
            logger.warning("JSON parse failed, trying extraction: %s", e)
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except Exception as e:
                    logger.debug("JSON extraction also failed: %s", e)
                    pass
        logger.warning("[finance.monthly] AI 洞察 JSON 解析失败: %s", text[:200])
        return None

    except Exception as e:
        logger.exception("[finance.monthly] AI 洞察生成异常: %s", e)
        return None


def _build_report_markdown(period_str, month_str, bill_summary, prev_summary,
                           snapshot_current, snapshot_comparison,
                           month_salary, trend_data, insights, import_result):
    """构建完整的 Markdown 月报"""
    lines = [
        "---",
        "type: finance-monthly-report",
        f"period: {month_str}",
        f"generated: {datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M')}",
        "---",
        "",
        f"# 📊 {period_str} 财务月报",
        "",
    ]

    # 导入信息
    if import_result and import_result.get("imported"):
        lines.append(f"> 📥 月报生成前自动导入了 {import_result['new_count']} 条新账单")
        lines.append("")

    # ═══ 收支总览 ═══
    lines.extend([
        "## 💰 收支总览",
        "",
        "| 项目 | 本月 | 上月 | 环比 |",
        "|------|-----:|-----:|-----:|",
    ])

    def _mom(curr, prev):
        """月环比"""
        if prev == 0:
            return "-"
        pct = (curr - prev) / abs(prev) * 100
        return f"{pct:+.1f}%"

    lines.append(f"| 总收入 | {format_currency(bill_summary['total_income'])} "
                 f"| {format_currency(prev_summary['total_income'])} "
                 f"| {_mom(bill_summary['total_income'], prev_summary['total_income'])} |")
    lines.append(f"| 总支出 | {format_currency(bill_summary['total_expense'])} "
                 f"| {format_currency(prev_summary['total_expense'])} "
                 f"| {_mom(bill_summary['total_expense'], prev_summary['total_expense'])} |")
    lines.append(f"| 结余 | {format_currency(bill_summary['balance'])} "
                 f"| {format_currency(prev_summary['balance'])} "
                 f"| {_mom(bill_summary['balance'], prev_summary['balance'])} |")
    lines.append(f"| 储蓄率 | {bill_summary['savings_rate']} "
                 f"| {prev_summary['savings_rate']} | - |")
    lines.append("")

    # 工资信息
    if month_salary:
        lines.extend([
            "### 📋 工资明细",
            "",
        ])
        for k, v in month_salary.items():
            if k != "日期" and v:
                lines.append(f"- **{k}**：{v}")
        lines.append("")

    # ═══ 支出 Top 分类 ═══
    expense_cats = bill_summary.get("expense_by_category", [])
    if expense_cats:
        lines.extend([
            "## 📂 支出分类 Top 10",
            "",
            "| 排名 | 分类 | 金额 | 占比 |",
            "|:----:|------|-----:|-----:|",
        ])
        for i, cat in enumerate(expense_cats[:10], 1):
            lines.append(f"| {i} | {cat['category']} "
                         f"| {format_currency(cat['amount'])} | {cat['percent']} |")
        lines.append("")

    # ═══ 资产变动 ═══
    if snapshot_current:
        summary = snapshot_current["summary"]
        lines.extend([
            "## 🏦 资产状况",
            "",
            f"> 快照日期：{snapshot_current['date']}",
            "",
            "| 项目 | 金额 |",
            "|------|-----:|",
            f"| 总资产 | {format_currency(summary['total_assets'])} |",
            f"| 总负债 | {format_currency(summary['total_liabilities'])} |",
            f"| 净资产 | {format_currency(summary['net_assets'])} |",
            f"| 可支配资产 | {format_currency(summary['disposable_assets'])} |",
            f"| 负债率 | {summary['debt_ratio']} |",
            "",
        ])

        # 按资产类别
        if summary.get("by_asset_class"):
            lines.extend([
                "### 资产类别明细",
                "",
                "| 类别 | 金额 |",
                "|------|-----:|",
            ])
            for cls, amt in summary["by_asset_class"].items():
                lines.append(f"| {cls} | {format_currency(amt)} |")
            lines.append("")

    # 快照对比
    if snapshot_comparison:
        lines.extend([
            "### 📈 资产变动（快照对比）",
            "",
            f"对比期间：{snapshot_comparison['previous_date']} → {snapshot_comparison['current_date']}",
            "",
            "| 项目 | 变动金额 | 变动比例 |",
            "|------|--------:|--------:|",
            f"| 总资产 | {format_currency(snapshot_comparison['asset_change'])} "
            f"| {snapshot_comparison['asset_change_pct']} |",
            f"| 可支配资产 | {format_currency(snapshot_comparison['disposable_change'])} "
            f"| {snapshot_comparison['disposable_change_pct']} |",
            "",
        ])

        class_changes = snapshot_comparison.get("class_changes", [])
        if class_changes:
            lines.extend([
                "| 类别 | 本期 | 上期 | 变动 | 比例 |",
                "|------|-----:|-----:|-----:|-----:|",
            ])
            for c in class_changes[:8]:
                lines.append(f"| {c['class']} | {format_currency(c['current'])} "
                             f"| {format_currency(c['previous'])} "
                             f"| {format_currency(c['change'])} | {c['change_pct']} |")
            lines.append("")

    # ═══ 趋势 ═══
    if trend_data and len(trend_data) > 1:
        lines.extend([
            "## 📉 近期趋势",
            "",
            "| 月份 | 收入 | 支出 | 结余 | 储蓄率 |",
            "|------|-----:|-----:|-----:|-------:|",
        ])
        for t in trend_data:
            lines.append(f"| {t['label']} | {format_currency(t['income'])} "
                         f"| {format_currency(t['expense'])} "
                         f"| {format_currency(t['balance'])} | {t['savings_rate']} |")
        lines.append("")

    # ═══ 洞察 ═══
    if insights:
        # 现金流深度分析
        cashflow = insights.get("cashflow")
        if cashflow:
            lines.extend(["## 💸 现金流分析", ""])
            if cashflow.get("headline"):
                lines.append(f"**{cashflow['headline']}**")
                lines.append("")
            if cashflow.get("real_balance") and cashflow.get("real_savings_rate"):
                lines.append("| 指标 | 数值 |")
                lines.append("|------|-----:|")
                lines.append(f"| 真实结余 | {cashflow['real_balance']} |")
                lines.append(f"| 真实储蓄率 | {cashflow['real_savings_rate']} |")
                verdict_map = {"surplus": "✅ 盈余", "breakeven": "⚖️ 打平", "deficit": "🔴 赤字"}
                lines.append(f"| 判定 | {verdict_map.get(cashflow.get('verdict', ''), cashflow.get('verdict', ''))} |")
                lines.append("")
            if cashflow.get("detail"):
                lines.append(cashflow["detail"])
                lines.append("")

        # 消费洞察
        spending = insights.get("spending_insight")
        if spending:
            lines.extend(["## 🛒 消费洞察", ""])
            if spending.get("top_concern"):
                lines.append(f"- **关注点**：{spending['top_concern']}")
            if spending.get("pattern"):
                lines.append(f"- **消费模式**：{spending['pattern']}")
            if spending.get("compare"):
                lines.append(f"- **环比变化**：{spending['compare']}")
            lines.append("")

        # 资产健康度
        asset_health = insights.get("asset_health")
        if asset_health:
            lines.extend(["## 🏦 资产健康度", ""])
            if asset_health.get("headline"):
                lines.append(f"**{asset_health['headline']}**")
                lines.append("")
            if asset_health.get("goose_growth"):
                lines.append(f"- **生钱资产**：{asset_health['goose_growth']}")
            if asset_health.get("rsu_risk"):
                lines.append(f"- **RSU 风险**：{asset_health['rsu_risk']}")
            if asset_health.get("diversification_score"):
                lines.append(f"- **分散度**：{asset_health['diversification_score']}")
            if asset_health.get("detail"):
                lines.append(f"\n{asset_health['detail']}")
            lines.append("")

        # FIRE 进度
        fire = insights.get("fire_progress")
        if fire and fire.get("fire_target"):
            lines.extend(["## 🔥 FIRE 进度", ""])
            lines.append("| 指标 | 数值 |")
            lines.append("|------|-----:|")
            if fire.get("annual_expense_estimate"):
                lines.append(f"| 年化支出 | {fire['annual_expense_estimate']} |")
            if fire.get("fire_target"):
                lines.append(f"| FIRE 目标金额（×25） | {fire['fire_target']} |")
            if fire.get("current_assets_toward_fire"):
                lines.append(f"| 当前生钱资产 | {fire['current_assets_toward_fire']} |")
            if fire.get("progress_pct"):
                lines.append(f"| **进度** | **{fire['progress_pct']}** |")
            lines.append("")
            if fire.get("comment"):
                lines.append(fire["comment"])
                lines.append("")

        # 行动项
        actions = insights.get("action_items", [])
        if actions:
            lines.extend(["## ✅ 下月行动", ""])
            for act in actions:
                lines.append(f"- {act}")
            lines.append("")

        # 总结
        summary_text = insights.get("summary", "")
        if summary_text:
            lines.extend([
                "## 💌 总结",
                "",
                summary_text,
                "",
            ])

    # 尾部
    lines.extend([
        "---",
        "",
        f"*🤖 基于 {bill_summary['record_count']} 条收支记录自动生成于 "
        f"{datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M')}*",
    ])

    return "\n".join(lines)


def _build_wechat_summary(period_str, bill_summary, snapshot_current,
                          snapshot_comparison, insights):
    """构建企微推送的摘要消息"""
    parts = [f"📊 {period_str} 财务月报"]
    parts.append("")

    # 收支
    parts.append(f"💰 收入 {format_currency(bill_summary['total_income'])} "
                 f"| 支出 {format_currency(bill_summary['total_expense'])}")

    # 真实结余（优先用 AI 计算的数据）
    cashflow = insights.get("cashflow", {}) if insights else {}
    if cashflow.get("real_balance") and cashflow.get("verdict"):
        verdict_emoji = {"surplus": "✅", "breakeven": "⚖️", "deficit": "🔴"}.get(
            cashflow["verdict"], "📈")
        parts.append(f"{verdict_emoji} 真实结余 {cashflow['real_balance']}（储蓄率 {cashflow.get('real_savings_rate', '-')}）")
    else:
        parts.append(f"📈 结余 {format_currency(bill_summary['balance'])} "
                     f"| 储蓄率 {bill_summary['savings_rate']}")

    # 支出 Top 3
    top3 = bill_summary.get("expense_by_category", [])[:3]
    if top3:
        top_str = " / ".join(f"{c['category']} {format_currency(c['amount'])}" for c in top3)
        parts.append(f"🏷️ Top3: {top_str}")

    # 资产
    if snapshot_current:
        s = snapshot_current["summary"]
        parts.append(f"🏦 净资产 {format_currency(s['net_assets'])}")

    # FIRE 进度
    fire = insights.get("fire_progress", {}) if insights else {}
    if fire and fire.get("progress_pct"):
        parts.append(f"🔥 FIRE 进度 {fire['progress_pct']}")

    # 总结
    if insights:
        summary = insights.get("summary", "")
        if summary:
            parts.append("")
            parts.append(f"💌 {summary[:150]}")

    # 行动项
    actions = insights.get("action_items", []) if insights else []
    if actions:
        parts.append("")
        for act in actions[:2]:
            parts.append(f"👉 {act}")

    parts.append("")
    parts.append("完整报告已写入 Obsidian 📝")

    return "\n".join(parts)


def handle_monthly(params, state, ctx):
    """财务月报入口（用户手动触发时走 skill 系统）"""
    return execute(params, state, ctx)


# Skill 热加载注册表（V12: visibility=private，仅管理员可见可用）
SKILL_REGISTRY = {
    "finance.monthly": {
        "handler": handle_monthly,
        "visibility": "private",
        "description": "财务月报生成",
    },
}
