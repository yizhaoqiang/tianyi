import json
import os

# ====== 输入配置 ======
YEAR = 2025
INPUT_ROOT = f"竞彩/{YEAR}年"
OUTPUT_ROOT = f"竞彩分析/{YEAR}年"

# ====== 公共函数（保持原逻辑） ======
def generate_result_for_scene(scene_matches, order_val, team_ids):
    has_postponed = any(
        m.get("result", {}).get("win_draw_lose_result") == "延期"
        or m.get("result", {}).get("full") in ["", "-"]
        for m in scene_matches
    )
    if has_postponed:
        return None

    valid_scores, valid_handicaps, valid_half_fulls, valid_size = [], [], [], []
    total_odds_score = total_odds_handicap = total_odds_half_full = total_odds_size = 1.0

    for m in scene_matches:
        result = m.get("result", {})

        full_score = result.get("full", "")
        handicap = result.get("handicap_result", "")
        half_full = result.get("half_full_result", "").replace("-", "")
        size = result.get("goal_result", "")

        score_odds = result.get("score_odds", "1")
        handicap_odds = result.get("handicap_odds", "1")
        half_full_odds = result.get("half_full_odds", "1")
        size_odds = result.get("goal_odds", "1")

        valid_scores.append(full_score)
        valid_handicaps.append(handicap)
        valid_half_fulls.append(half_full)
        valid_size.append(size)

        try: total_odds_score *= float(score_odds)
        except: pass
        try: total_odds_handicap *= float(handicap_odds)
        except: pass
        try: total_odds_half_full *= float(half_full_odds)
        except: pass
        try: total_odds_size *= float(size_odds)
        except: pass

    score_text = "_".join(valid_scores) + f"_{int(total_odds_score * 100)}"
    handicap_text = "_".join(valid_handicaps) + f"_{int(total_odds_handicap * 100)}"
    half_full_text = "_".join(valid_half_fulls) + f"_{int(total_odds_half_full * 100)}"
    size_text = "_".join(valid_size) + f"_{int(total_odds_size * 100)}"

    if len(scene_matches) > 4:
        score_text = half_full_text = size_text = "-"

    return {
        "type": {"order": order_val, "count": str(len(scene_matches))},
        "result": {
            "score": score_text,
            "handicap": handicap_text,
            "halfFull": half_full_text,
            "size": size_text
        },
        "team": team_ids
    }


# ====== 遍历整年所有月份与 JSON 文件 ======
for month_dir in sorted(os.listdir(INPUT_ROOT)):
    month_path = os.path.join(INPUT_ROOT, month_dir)
    if not os.path.isdir(month_path):
        continue  # 跳过非文件夹

    # 输出目录
    output_month_dir = os.path.join(OUTPUT_ROOT, month_dir)
    os.makedirs(output_month_dir, exist_ok=True)

    for filename in sorted(os.listdir(month_path)):
        if not filename.endswith(".json"):
            continue

        input_file = os.path.join(month_path, filename)
        output_file = os.path.join(output_month_dir, filename)

        # ====== 读取原 JSON ======
        with open(input_file, "r", encoding="utf-8") as f:
            matches = json.load(f)

        if not isinstance(matches, list) or len(matches) < 2:
            print(f"⚠️ {filename} 比赛数量不足，跳过。")
            continue

        total_count = len(matches)
        first_time = matches[0].get("match_time", "").split(" ")[0]

        output_data = {"time": first_time, "content": [], "matches": []}

        # ====== F2 ~ F8 ======
        max_count = min(total_count, 8)
        for count in range(2, max_count + 1):
            scene_matches = matches[:count]
            team_ids = "_".join([f"{i:03d}" for i in range(1, count + 1)])
            item = generate_result_for_scene(scene_matches, f"F{count}", team_ids)
            if item:
                output_data["content"].append(item)

        # ====== FL (首尾) ======
        first_last = [matches[0], matches[-1]]
        team_ids = f"{1:03d}_{total_count:03d}"
        item = generate_result_for_scene(first_last, "FL", team_ids)
        if item:
            output_data["content"].append(item)

        # ====== FL2 (前两+后两) ======
        if total_count >= 3:
            first_two = matches[:2]
            last_two = matches[-2:]
            combined, seen = [], set()
            for m in first_two + last_two:
                mid = m.get("matchId", "")
                if mid not in seen:
                    combined.append(m)
                    seen.add(mid)
            team_ids = "_".join([m.get("num", "")[-3:] for m in combined])
            item = generate_result_for_scene(combined, "FL2", team_ids)
            if item:
                output_data["content"].append(item)

        # ====== matches 输出逻辑 ======
        if total_count > 8:
            top8 = matches[:8]
            last2 = matches[-2:]
            matches_to_output, seen = [], set()
            for m in top8 + last2:
                mid = m.get("matchId", "")
                if mid not in seen:
                    matches_to_output.append(m)
                    seen.add(mid)
        else:
            matches_to_output = matches

        # ====== 填充 matches ======
        for m in matches_to_output:
            match_item = {
                "num": m.get("num", ""),
                "match_time": m.get("match_time", ""),
                "matchId": m.get("matchId", ""),
                "league": m.get("league", ""),
                "home": m.get("home", ""),
                "away": m.get("away", ""),
                "result": m.get("result", {})
            }
            output_data["matches"].append(match_item)

        # ====== 写出 ======
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)

        print(f"✅ 已生成: {output_file}")

print("🎯 全部处理完成！")
