import os
import json
import itertools

# ====== 可配置项 ======
YEAR = 2025
BASE_DIR = f"竞彩/{YEAR}年"

# 购买的比分顺序（严格顺序）
TARGET_SCORES = ["1-1", "2-0", "0-3", "2-2"]
BET_AMOUNT = 2
CHAIN_TYPES = [2, 3, 4]
MULTI_MAP = {2: 0, 3: 0, 4: 5}

# ====== 汇总 ======
year_total_bet = 0.0
year_total_bonus = 0.0
daily_stats = []

for month in range(1, 13):
    month_dir = os.path.join(BASE_DIR, f"{month}月")
    if not os.path.exists(month_dir):
        continue

    for filename in sorted(os.listdir(month_dir)):
        if not filename.endswith(".json"):
            continue

        file_path = os.path.join(month_dir, filename)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                matches = json.load(f)
        except Exception as e:
            print(f"⚠️ 无法解析文件 {file_path}: {e}")
            continue

        matches = matches[:4]  # 只取前4场

        # ====== 生成带顺序的比赛列表 ======
        ordered_matches = []
        for idx, m in enumerate(matches):
            result = m.get("result", {})
            full_score = result.get("full")
            score_odds = result.get("score_odds")

            # ⚠️ 排除赔率为 "-" 或空的比赛
            if not full_score or not score_odds or score_odds == "-":
                continue

            try:
                odds_val = float(score_odds)
            except ValueError:
                # 遇到非法赔率（如"-"或空），跳过
                continue

            ordered_matches.append({
                "index": idx,  # 原始顺序
                "num": m.get("num", ""),
                "score": full_score,
                "odds": odds_val
            })

        # ====== 匹配命中（顺序匹配） ======
        hit_matches = []
        for i, target_score in enumerate(TARGET_SCORES):
            # 如果这一场比赛数据缺失（被跳过），就跳过比对
            match = next((m for m in ordered_matches if m["index"] == i), None)
            if match and match["score"] == target_score:
                hit_matches.append(match)

        # ====== 计算中奖金额 ======
        total_bonus = 0.0
        for n in CHAIN_TYPES:
            combos = list(itertools.combinations(hit_matches, n))
            for combo in combos:
                # 确保比赛顺序递增
                indices = [m["index"] for m in combo]
                if indices != sorted(indices):
                    continue
                combo_odds = 1
                for m in combo:
                    combo_odds *= m["odds"]
                extra_mult = MULTI_MAP.get(n, 1)
                total_bonus += combo_odds * BET_AMOUNT * extra_mult

        # ====== 计算总投注金额 ======
        # ⚠️ 注意：即使有比赛被取消，投注仍然按4场算（假设你已经下单）
        total_bet = 0.0
        for n in CHAIN_TYPES:
            combo_count = len(list(itertools.combinations(matches, n)))
            extra_mult = MULTI_MAP.get(n, 1)
            total_bet += combo_count * BET_AMOUNT * extra_mult

        # ====== 年度汇总 ======
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

        # ====== 输出每日结果 ======
        print(f"\n📅 {filename.replace('.json', '')}")
        print(f"命中场次：{len(hit_matches)} 场")
        for h in hit_matches:
            print(f" 第{h['index'] + 1}场 | {h['num']} | 比分 {h['score']} | 赔率 {h['odds']}")
        print(f"💰 当日中奖金额：{total_bonus:.2f} 元")
        print(f"💸 当日投入金额：{total_bet:.2f} 元")
        print(f"📊 当日盈亏：{profit:.2f} 元（{profit_status}）")

max_bonus_day = max(daily_stats, key=lambda x: x["bonus"]) if daily_stats else None

# ====== 汇总输出 ======
print("\n" + "=" * 60)
print(f"📅 {YEAR} 年度汇总：")
print(f"💸 总投入金额：{year_total_bet:.2f} 元")
print(f"💰 总中奖金额：{year_total_bonus:.2f} 元")
profit_year = year_total_bonus - year_total_bet
status_year = "✅ 盈利" if profit_year > 0 else "❌ 亏损"
print(f"📊 年度盈亏：{profit_year:.2f} 元（{status_year}）")
print("-" * 60)
if max_bonus_day:
    print(f"🏆 单日最高中奖金额：{max_bonus_day['bonus']:.2f} 元（日期：{max_bonus_day['date']}）")
print("=" * 60)
