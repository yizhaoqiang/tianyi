import os
import json
import itertools

# ====== 可配置项 ======
YEAR = 2023
BASE_DIR = f"竞彩/{YEAR}年"

BET_AMOUNT = 100
CHAIN_TYPES = [2, 3, 4]
MULTI_MAP = {2: 1, 3: 0, 4: 0}

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

        if not matches:
            continue

        # 限制场次数，默认取全部可用场次
        matches = matches[:min(len(matches), 2)]

        # ====== 生成带顺序的比赛列表 ======
        ordered_matches = []
        for idx, m in enumerate(matches):
            result = m.get("result", {})
            handicap = result.get("handicap")
            handicap_result = result.get("handicap_result")
            handicap_odds = result.get("handicap_odds")

            # ⚠️ 跳过无效数据
            if handicap_result is None or not handicap_odds or handicap_odds == "-" or handicap is None:
                continue

            try:
                odds_val = float(handicap_odds)
                handicap_val = float(handicap)
            except ValueError:
                continue

            ordered_matches.append({
                "index": idx,
                "num": m.get("num", ""),
                "handicap": handicap_val,
                "result": str(handicap_result),
                "odds": odds_val
            })

        if not ordered_matches:
            continue

        # ====== 根据 handicap 决定购买结果 ======
        hit_matches = []
        for i, match in enumerate(ordered_matches):
            handicap = match["handicap"]
            actual_result = match["result"]

            if i == 0 :
              # ✅ 简化版投注策略
              if handicap < 0:
                buy_result = "0"  # 主队让球 → 买主胜
              else:
                buy_result = "3"  # 客队受让或平手 → 买主负
            else :
              if handicap < 0:
                buy_result = "3"  # 主队让球 → 买主胜
              else:
                buy_result = "0"  # 客队受让或平手 → 买主负

            # 判断命中
            if actual_result == buy_result:
                hit_matches.append({
                    **match,
                    "buy_result": buy_result
                })

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
            print(
                f" 第{h['index'] + 1}场 | {h['num']} | 让球 {h['handicap']} | 赔率 {h['odds']} | "
                f"购买 {h['buy_result']} | 实际结果 {h['result']}"
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
