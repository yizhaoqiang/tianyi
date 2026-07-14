import os
import json
import itertools
from math import comb  # 用于计算组合数

# ====== 可配置项 ======
YEAR = 2025
BASE_DIR = f"竞彩/{YEAR}年"

# 购买的让球结果顺序（严格顺序）
TARGET_RESULTS = ["0", "0", "0", "0"]
BET_AMOUNT = 2
CHAIN_TYPES = [2, 3, 4]
MULTI_MAP = {2: 1, 3: 1, 4: 5}

# ====== 汇总 ======
year_total_bet = 0.0
year_total_bonus = 0.0
daily_stats = []

ticket_len = len(TARGET_RESULTS)

for month in range(1, 13):
    month_dir = os.path.join(BASE_DIR, f"{month}月")
    if not os.path.exists(month_dir):
        continue

    month_stopped = False  # 当月是否满足“全部命中”后停止购买

    for filename in sorted(os.listdir(month_dir)):
        if not filename.endswith(".json"):
            continue

        date_str = filename.replace(".json", "")

        # 如果当月已经满足全部命中 → 停止购买（记录但不投注）
        if month_stopped:
            print(f"\n📅 {date_str}（当月已全部命中过一次 → 停止购买）")
            daily_stats.append({
                "date": date_str,
                "profit": 0.0,
                "bonus": 0.0,
                "bet": 0.0,
            })
            continue

        # ====== 正常比赛解析 ======
        file_path = os.path.join(month_dir, filename)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                matches = json.load(f)
        except Exception as e:
            print(f"⚠️ 无法解析文件 {file_path}: {e}")
            continue

        matches = matches[:ticket_len]

        # ====== 生成可判定的比赛列表 ======
        ordered_matches = []
        for idx, m in enumerate(matches):
            result = m.get("result", {})
            handicap_result = result.get("handicap_result")
            handicap_odds = result.get("handicap_odds")

            if handicap_result is None or not handicap_odds or handicap_odds == "-":
                continue

            try:
                odds_val = float(handicap_odds)
            except:
                continue

            ordered_matches.append({
                "index": idx,
                "num": m.get("num", ""),
                "result": str(handicap_result),
                "odds": odds_val,
            })

        # ====== 命中判定（基于下单顺序索引） ======
        hit_matches = []
        for i, target_result in enumerate(TARGET_RESULTS):
            match = next((m for m in ordered_matches if m["index"] == i), None)
            if match and match["result"] == target_result:
                hit_matches.append(match)

        # ====== 当日中奖计算 ======
        total_bonus = 0.0
        for n in CHAIN_TYPES:
            combos = list(itertools.combinations(hit_matches, n))
            for combo in combos:
                indices = [x["index"] for x in combo]
                if indices != sorted(indices):
                    continue
                combo_odds = 1
                for m in combo:
                    combo_odds *= m["odds"]
                total_bonus += combo_odds * BET_AMOUNT * MULTI_MAP.get(n, 1)

        # ====== 当日投注金额（按下单时的场数 ticket_len 计算） ======
        total_bet = 0.0
        for n in CHAIN_TYPES:
            combo_count = comb(ticket_len, n) if ticket_len >= n else 0
            total_bet += combo_count * BET_AMOUNT * MULTI_MAP.get(n, 1)

        # ====== 年度累加 ======
        year_total_bet += total_bet
        year_total_bonus += total_bonus

        profit = total_bonus - total_bet

        daily_stats.append({
            "date": date_str,
            "profit": profit,
            "bonus": total_bonus,
            "bet": total_bet,
        })

        # ====== 输出每日结果 ======
        print(f"\n📅 {date_str}")
        print(f"解析到可判定场次：{len(ordered_matches)} / 下单场次：{ticket_len}")
        print(f"命中场次：{len(hit_matches)} 场")

        if hit_matches:
            for h in hit_matches:
                print(f" 第{h['index'] + 1}场 | {h['num']} | 让球结果 {h['result']} | 赔率 {h['odds']}")

        print(f"💰 当日中奖金额：{total_bonus:.2f} 元")
        print(f"💸 当日投入金额：{total_bet:.2f} 元")
        print(f"📊 当日盈亏：{profit:.2f} 元")

        # ====== 若当日全部命中（四个都中） → 本月停止购买 ======
        if len(hit_matches) == ticket_len:
            print("🎉 当日四场全部命中 → 本月剩余日期全部停止购买！")
            month_stopped = True

# ====== 统计附加信息 ======
max_loss_streak = 0
current_streak = 0
for day in daily_stats:
    if day["profit"] < 0:
        current_streak += 1
        max_loss_streak = max(max_loss_streak, current_streak)
    else:
        current_streak = 0

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
print(f"📉 最大连续亏损天数：{max_loss_streak} 天")
if max_bonus_day:
    print(f"🏆 单日最高中奖金额：{max_bonus_day['bonus']:.2f} 元（日期：{max_bonus_day['date']}）")
print("=" * 60)
