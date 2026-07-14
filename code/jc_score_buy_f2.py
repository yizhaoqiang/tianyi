import os
import json
import itertools

# ====== 可配置项 ======
YEAR = 2015
BASE_DIR = f"竞彩/{YEAR}年"

# 只买前两场，购买的两个比分（顺序严格匹配）
TARGET_SCORES = ["1-0", "1-1"]
DAILY_BET = 100.0  # 每天固定投入 100 元

# 串关设置（这里只示例 2串1；如需支持多种，放入 CHAIN_TYPES）
CHAIN_TYPES = [2]
MULTI_MAP = {2: 1}  # 2串1 每种买几注

# ====== 汇总 ======
year_total_bet = 0.0
year_total_bonus = 0.0
daily_stats = []

for month in range(1, 13):
    month_dir = os.path.join(BASE_DIR, f"{month}月")
    if not os.path.exists(month_dir):
        continue

    month_has_win = False  # 本月是否已经中奖（中奖后停止本月后续投注）

    for filename in sorted(os.listdir(month_dir)):
        if not filename.endswith(".json"):
            continue

        if month_has_win:
            # 本月已中奖，停止购买
            break

        file_path = os.path.join(month_dir, filename)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                matches = json.load(f)
        except Exception as e:
            print(f"⚠️ 无法解析文件 {file_path}: {e}")
            continue

        # 只分析前两场（原始 matches 保留用于计算总票数）
        matches = matches[:2]

        # ====== 统计用于下注的“总票数” ======
        # 这里按照原始 matches（即便某场赔率为 "-" 或缺失），计算组合数量与 MULTI_MAP
        total_tickets = 0
        for n in CHAIN_TYPES:
            combo_count = len(list(itertools.combinations(matches, n)))
            extra_mult = MULTI_MAP.get(n, 1)
            total_tickets += combo_count * extra_mult

        if total_tickets <= 0:
            stake_per_ticket = 0.0
        else:
            stake_per_ticket = DAILY_BET / total_tickets

        # ====== 准备有效的、有赔率的比赛（保留原始索引） ======
        ordered_matches = []
        for idx, m in enumerate(matches):
            result = m.get("result", {})
            full_score = result.get("full")
            score_odds = result.get("score_odds")
            # 排除比分/赔率为空或赔率为 "-"（延期/取消）
            if not full_score or not score_odds or score_odds == "-":
                continue
            try:
                odds_val = float(score_odds)
            except (ValueError, TypeError):
                continue
            ordered_matches.append({
                "index": idx,
                "num": m.get("num", ""),
                "score": full_score,
                "odds": odds_val
            })

        # ====== 命中匹配（按顺序） ======
        # 先找出哪些场次在位置上命中
        hit_matches = []
        for i, target_score in enumerate(TARGET_SCORES):
            match = next((mm for mm in ordered_matches if mm["index"] == i), None)
            if match and match["score"] == target_score:
                hit_matches.append(match)

        # ====== 计算中奖金额 ======
        total_bonus = 0.0
        for n in CHAIN_TYPES:
            # 对命中场次做 n 组合（只从 hit_matches 中挑出真正命中的组合）
            combos = list(itertools.combinations(hit_matches, n))
            for combo in combos:
                indices = [m["index"] for m in combo]
                # 确保组合在原比赛中的顺序是递增的（严格顺序）
                if indices != sorted(indices):
                    continue
                # 计算组合赔率乘积
                combo_odds = 1.0
                for m in combo:
                    combo_odds *= m["odds"]
                extra_mult = MULTI_MAP.get(n, 1)
                # 中奖金额 = 赔率 * 每张票注额 * 注数（extra_mult）
                total_bonus += combo_odds * stake_per_ticket * extra_mult

        # ====== 投注金额固定为 DAILY_BET ======
        total_bet = DAILY_BET

        # ====== 汇总年度数据（仅将实际下的注计入年度投入） ======
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

        # ====== 输出当日结果 ======
        print(f"\n📅 {filename.replace('.json', '')} （{month}月）")
        print(f"下注总票数：{total_tickets} 张（每张注额 {stake_per_ticket:.2f} 元）")
        print(f"命中场次：{len(hit_matches)} 场")
        for h in hit_matches:
            print(f" 第{h['index'] + 1}场 | {h['num']} | 比分 {h['score']} | 赔率 {h['odds']}")
        print(f"💰 当日中奖金额：{total_bonus:.2f} 元")
        print(f"💸 当日投入金额：{total_bet:.2f} 元")
        print(f"📊 当日盈亏：{profit:.2f} 元（{profit_status}）")

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
print("\n" + "=" * 60)
print(f"📅 {YEAR} 年度汇总：")
print(f"💸 总投入金额：{year_total_bet:.2f} 元")
print(f"💰 总中奖金额：{year_total_bonus:.2f} 元")
profit_year = year_total_bonus - year_total_bet
status_year = "✅ 盈利" if profit_year > 0 else "❌ 亏损"
print(f"📊 年度盈亏：{profit_year:.2f} 元（{status_year}）")
print("-" * 60)
print(f"📉 最大连续亏损天数：{max_loss_streak} 天")
if max_bonus_day:
    print(f"🏆 单日最高中奖金额：{max_bonus_day['bonus']:.2f} 元（日期：{max_bonus_day['date']}）")
print("=" * 60)
