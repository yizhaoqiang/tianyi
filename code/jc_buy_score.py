import os
import json
import itertools

# ====== 可配置项 ======
YEAR = 2022
BASE_DIR = f"竞彩/{YEAR}年"

# 分场次指定投注的比分（替换原有的进球数目标）
# 索引0对应第一场，索引1对应第二场
TARGET_SCORES = {
    0: ["1-2", "1-0"],   # 第一场买2-1、1-1
    1: ["1-1", "0-0"]    # 第二场买1-1、0-1
}
DAILY_BET = 104.0  # 每天固定投入 96 元

# 串关设置（这里只示例 2串1；如需支持多种，放入 CHAIN_TYPES）
CHAIN_TYPES = [2]
MULTI_MAP = {2: 13}  # 2串1 每种买8注

# ====== 统计初始化 ======
# 比分统计：{场次索引: {"total": 总场次, "score_count": {比分: 出现次数}}}
score_stats = {
    0: {"total": 0, "score_count": {}},  # 第一场比分
    1: {"total": 0, "score_count": {}}   # 第二场比分
}

# 进球数统计（结构和比分统计一致）
goal_stats = {
    0: {"total": 0, "goal_count": {}},  # 第一场总进球数
    1: {"total": 0, "goal_count": {}}   # 第二场总进球数
}

# ====== 汇总 ======
year_total_bet = 0.0
year_total_bonus = 0.0
daily_stats = []

def get_total_goals_from_score(score):
    """从比分（如"1-1"）提取总进球数，失败返回None"""
    try:
        home, away = score.split("-")
        return str(int(home) + int(away))
    except (ValueError, AttributeError):
        return None

for month in range(1, 13):
    month_dir = os.path.join(BASE_DIR, f"{month}月")
    if not os.path.exists(month_dir):
        continue

    month_has_win = False  # 本月是否已经中奖（中奖后停止本月后续投注）

    for filename in sorted(os.listdir(month_dir)):
        if not filename.endswith(".json"):
            continue

        if month_has_win:
            break

        file_path = os.path.join(month_dir, filename)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                matches = json.load(f)
        except Exception as e:
            print(f"⚠️ 无法解析文件 {file_path}: {e}")
            continue

        # 只分析前两场
        matches = matches[:2]

        # ====== 统计比分 + 总进球数 ======
        for idx in [0, 1]:  # 仅统计第一场和第二场
            if idx >= len(matches):  # 防止文件中比赛数量不足2场
                continue
            match = matches[idx]
            result = match.get("result", {})

            # 1. 统计比分（优先用score_result，兜底用full）
            score_result = result.get("score_result") or result.get("full")
            if score_result and score_result != "-":
                # 统一格式（将0:1转为0-1，保证统计一致性）
                standard_score = score_result.replace(":", "-")
                score_stats[idx]["total"] += 1
                score_stats[idx]["score_count"][standard_score] = score_stats[idx]["score_count"].get(standard_score, 0) + 1

                # 2. 统计总进球数（从标准化比分计算）
                total_goals = get_total_goals_from_score(standard_score)
                if total_goals:
                    goal_stats[idx]["total"] += 1
                    goal_stats[idx]["goal_count"][total_goals] = goal_stats[idx]["goal_count"].get(total_goals, 0) + 1

        # ====== 统计用于下注的“总票数” ======
        # 计算2串1的总票数：第一场选项数 × 第二场选项数 × 倍数
        total_tickets = 0
        if 2 in CHAIN_TYPES:
            combo_2in1 = len(TARGET_SCORES.get(0, [])) * len(TARGET_SCORES.get(1, []))
            total_tickets = combo_2in1 * MULTI_MAP.get(2, 1)

        # ====== 优化金额计算：避免浮点数精度误差 ======
        if total_tickets <= 0:
            stake_per_ticket = 0.0
            actual_total_bet = 0.0
        else:
            stake_per_ticket = DAILY_BET / total_tickets
            actual_total_bet = round(stake_per_ticket * total_tickets, 2)
            if abs(actual_total_bet - DAILY_BET) > 0.01:
                stake_per_ticket = (DAILY_BET - (stake_per_ticket * (total_tickets - 1))) / 1

        # ====== 准备有效的、有赔率的比赛（保留原始索引） ======
        ordered_matches = []
        for idx, m in enumerate(matches):
            result = m.get("result", {})
            # 替换为比分相关字段：score_result（实际比分）、score_odds（比分赔率）
            score_result = result.get("score_result")
            score_odds = result.get("score_odds")

            # 排除比分/赔率为空或赔率为 "-"（延期/取消）
            if not score_result or not score_odds or score_odds == "-":
                continue
            try:
                odds_val = float(score_odds)
            except (ValueError, TypeError):
                continue

            # 检查该场次是否有投注目标
            if idx not in TARGET_SCORES:
                continue

            # 统一比分格式（将0:1转为0-1，匹配投注目标的格式）
            standard_score = score_result.replace(":", "-")

            ordered_matches.append({
                "index": idx,
                "num": m.get("num", ""),
                "score_result": standard_score,  # 标准化后的实际比分
                "score_odds": odds_val,          # 比分赔率
                "target_scores": TARGET_SCORES[idx]  # 该场次投注的比分
            })

        # ====== 命中匹配（比分投注） ======
        hit_matches = []
        for match in ordered_matches:
            idx = match["index"]
            actual_score = match["score_result"]
            target_scores = match["target_scores"]

            # 检查实际比分是否在投注目标中
            if actual_score in target_scores:
                hit_matches.append({
                    "index": idx,
                    "num": match["num"],
                    "actual_score": actual_score,
                    "odds": match["score_odds"]
                })

        # ====== 计算中奖金额 ======
        total_bonus = 0.0
        for n in CHAIN_TYPES:
            if n == 2:
                # 2串1 必须两场都命中
                if len(hit_matches) >= 2:
                    # 找出所有可能的2场组合（这里只有前两场）
                    combos = list(itertools.combinations(hit_matches, 2))
                    for combo in combos:
                        indices = [m["index"] for m in combo]
                        if indices != sorted(indices):
                            continue
                        # 计算组合赔率乘积
                        combo_odds = 1.0
                        for m in combo:
                            combo_odds *= m["odds"]
                        extra_mult = MULTI_MAP.get(n, 1)
                        # 中奖金额 = 赔率 * 每张票注额 * 注数（extra_mult）
                        total_bonus += combo_odds * stake_per_ticket * extra_mult

        # ====== 投注金额固定为 DAILY_BET（96元） ======
        total_bet = DAILY_BET

        # ====== 汇总年度数据 ======
        year_total_bet += total_bet
        year_total_bonus += total_bonus

        profit = total_bonus - total_bet
        profit_status = "✅ 盈利" if profit > 0 else "❌ 亏损"

        daily_stats.append({
            "date": filename.replace(".json", ""),
            "profit": profit,
            "bonus": total_bonus,
            "bet": total_bet,
        })

        # ====== 输出当日结果（适配比分投注） ======
        print(f"\n📅 {filename.replace('.json', '')} （{month}月）")
        print(f"下注总票数：{total_tickets} 张（每张注额 {stake_per_ticket:.4f} 元）")
        print(f"验证总投入：{stake_per_ticket * total_tickets:.2f} 元（预期：{DAILY_BET:.2f} 元）")
        print(f"命中场次：{len(hit_matches)} 场")
        for h in hit_matches:
            print(f" 第{h['index'] + 1}场 | {h['num']} | 比分 {h['actual_score']} | 赔率 {h['odds']}")
        print(f"💰 当日中奖金额：{total_bonus:.2f} 元")
        print(f"💸 当日投入金额：{total_bet:.2f} 元")
        print(f"📊 当日盈亏：{profit:.2f} 元（{profit_status}）")

# ====== 通用百分比格式化函数（适配比分/进球数） ======
def format_percent(stats, field_name, count_key):
    """
    格式化输出百分比
    :param stats: 统计数据字典
    :param field_name: 展示字段名（如"第一场比分统计"）
    :param count_key: 统计计数的key（score_count/goal_count）
    """
    total = stats["total"]
    count_data = stats[count_key]

    if total == 0:
        return f"\n  {field_name}：无有效比赛数据"

    # 按出现次数降序排序
    sorted_items = sorted(count_data.items(), key=lambda x: x[1], reverse=True)
    output = [f"\n  {field_name}（总场次：{total}场）："]

    for item, count in sorted_items:
        percent = (count / total) * 100
        output.append(f"    {item}：{count}次（{percent:.2f}%）")

    return "\n".join(output)

# ====== 生成统计文本 ======
# 比分统计
first_score_percent = format_percent(score_stats[0], "第一场比分统计", "score_count")
second_score_percent = format_percent(score_stats[1], "第二场比分统计", "score_count")

# 进球数统计
first_goal_percent = format_percent(goal_stats[0], "第一场总进球数统计", "goal_count")
second_goal_percent = format_percent(goal_stats[1], "第二场总进球数统计", "goal_count")

# ====== 附加统计 ======
max_loss_streak = 0
current_streak = 0
for day in daily_stats:
    if day["profit"] < 0:
        current_streak += 1
        max_loss_streak = max(max_loss_streak, current_streak)
    else:
        current_streak = 0

max_bonus_day = max(daily_stats, key=lambda x: x["bonus"]) if daily_stats else None

# ====== 年度汇总输出 ======
print("\n" + "=" * 80)
print(f"📅 {YEAR} 年度汇总：")
print(f"💸 总投入金额：{year_total_bet:.2f} 元")
print(f"💰 总中奖金额：{year_total_bonus:.2f} 元")
profit_year = year_total_bonus - year_total_bet
status_year = "✅ 盈利" if profit_year > 0 else "❌ 亏损"
print(f"📊 年度盈亏：{profit_year:.2f} 元（{status_year}）")
print("-" * 80)
print(f"📉 最大连续亏损天数：{max_loss_streak} 天")
if max_bonus_day:
    print(f"🏆 单日最高中奖金额：{max_bonus_day['bonus']:.2f} 元（日期：{max_bonus_day['date']}）")
print("-" * 80)
# 输出比分统计
print(f"📊 {YEAR} 年比分出现百分比统计：")
print(first_score_percent)
print(second_score_percent)
print("-" * 80)
# 输出进球数统计
print(f"⚽ {YEAR} 年总进球数出现百分比统计：")
print(first_goal_percent)
print(second_goal_percent)
print("=" * 80)