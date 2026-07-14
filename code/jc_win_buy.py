import os
import json
import itertools
from math import comb  # 用于计算组合数

# ====== 可配置项 ======
YEAR = 2019
BASE_DIR = f"竞彩/{YEAR}年"

# 只买前几场（自动根据 handicap 判断买 3 或 0）
BUY_COUNT = 2
BET_AMOUNT = 100
CHAIN_TYPES = [2, 3, 4]
MULTI_MAP = {2: 1, 3: 1, 4: 5}

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

        # 只买前 BUY_COUNT 场
        matches = matches[:BUY_COUNT]

        # ====== 生成带顺序的比赛列表 ======
        ordered_matches = []
        for idx, m in enumerate(matches):
            result = m.get("result", {})
            handicap = result.get("handicap")
            win_draw_lose_result = result.get("win_draw_lose_result")  # 实际结果（3主胜 / 1平 / 0主负）
            win_draw_lose_odds = result.get("win_draw_lose_odds")  # 对应赔率

            if win_draw_lose_result is None or not win_draw_lose_odds or win_draw_lose_odds == "-":
                continue

            try:
                odds_val = float(win_draw_lose_odds)
            except (ValueError, TypeError):
                continue

            # 根据 handicap 判断购买方向
            # handicap < 0 代表主队让球 => 买主胜（3）
            # handicap >= 0 代表受让或平 => 买主负（0）
            try:
                handicap_val = float(handicap)
            except (ValueError, TypeError):
                handicap_val = 0

            # if idx == 0:
            buy_choice = "1"
            # else:
            #     buy_choice = "0" if handicap_val < 0 else "3"

            ordered_matches.append({
                "index": idx,
                "num": m.get("num", ""),
                "handicap": handicap_val,
                "buy": buy_choice,  # 我买的结果（3 或 0）
                "actual": str(win_draw_lose_result),  # 实际比赛结果
                "odds": odds_val
            })

        # ====== 匹配命中 ======
        hit_matches = [m for m in ordered_matches if m["buy"] == m["actual"]]

        # ====== 计算中奖金额 ======
        total_bonus = 0.0
        for n in CHAIN_TYPES:
            combos = list(itertools.combinations(hit_matches, n))
            for combo in combos:
                indices = [m["index"] for m in combo]
                if indices != sorted(indices):
                    continue
                combo_odds = 1
                for m in combo:
                    combo_odds *= m["odds"]
                extra_mult = MULTI_MAP.get(n, 1)
                total_bonus += combo_odds * BET_AMOUNT * extra_mult

        # ====== 计算总投注金额 ======
        total_bet = 0.0
        for n in CHAIN_TYPES:
            combo_count = comb(BUY_COUNT, n) if BUY_COUNT >= n else 0
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
        print(f"解析到可判定场次：{len(ordered_matches)} / 下单场次：{BUY_COUNT}")
        print(f"命中场次：{len(hit_matches)} 场")
        for m in ordered_matches:
            hit_flag = "✅" if m in hit_matches else "❌"
            print(
                f" 第{m['index']+1}场 | {m['num']} | 让球 {m['handicap']} | 买 {m['buy']} | 实际 {m['actual']} | 赔率 {m['odds']} | {hit_flag}"
            )
        print(f"💰 当日中奖金额：{total_bonus:.2f} 元")
        print(f"💸 当日投入金额：{total_bet:.2f} 元")
        print(f"📊 当日盈亏：{profit:.2f} 元（{profit_status}）")

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
