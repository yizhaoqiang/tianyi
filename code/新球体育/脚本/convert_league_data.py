#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
联赛初始数据 → 结构化JSON 转化脚本

自动扫描 初始数据 目录下所有联赛/赛季的联赛数据.txt，
提取关键字段输出为结构化 JSON。

用法：
  python3 convert_league_data.py

模式控制（修改下方 MODE 变量）：
  MODE = "FULL"          # 全量：重新转化所有联赛所有赛季
  MODE = "INCREMENTAL"   # 增量：跳过已存在输出文件的赛季
"""

import os
import re
import json
import time

INPUT_DIR = "/Users/yzq/Desktop/sports_db/code/新球体育/联赛数据/初始数据"
OUTPUT_DIR = "/Users/yzq/Desktop/sports_db/code/新球体育/联赛数据/转化数据"

# ═══════════════════════════════════════
#  ↓↓↓ 在这里修改运行模式 ↓↓↓
# ═══════════════════════════════════════
MODE = "INCREMENTAL"  # "FULL" = 全量重新转化, "INCREMENTAL" = 增量（跳过已有）
# ═══════════════════════════════════════


def parse_js_array(text, var_name):
    """解析 JS 数组变量赋值，返回该变量的值字符串"""
    # 匹配 var varName = [...] 或 varName = [...]
    # 支持 arrTeam 和 jh["R_1"] 两种格式
    patterns = [
        rf"var\s+{re.escape(var_name)}\s*=\s*(\[.*?\])\s*;",
        rf"{re.escape(var_name)}\s*=\s*(\[.*?\])\s*;",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.DOTALL)
        if m:
            return m.group(1)
    return None


def parse_league_name(text):
    """从 arrLeague 提取联赛中文简称
    arrLeague 结构: [leagueId,'全称',...,'league_match/images/xxx.png','简称',...]
    取 image 之后的第一个中文名称
    """
    raw = parse_js_array(text, "arrLeague")
    if not raw:
        return ""
    # 找到 'league_match/images/' 后的第一个单引号字段
    parts = raw.split("','")
    for i, p in enumerate(parts):
        if "league_match/images/" in p:
            if i + 1 < len(parts):
                return parts[i + 1].strip("'")
    return ""


def parse_rounds(text):
    """解析所有 R_N 轮次数据，返回 [{round, matches}]"""
    rounds = []
    # 找出所有 R_1, R_2, ..., R_38
    for m in re.finditer(r'jh\["R_(\d+)"\]\s*=\s*(\[.*?\])\s*;', text, re.DOTALL):
        round_num = int(m.group(1))
        raw = m.group(2)
        matches = []
        # 解析数组中的每个比赛
        for mm in re.finditer(r"\[(.*?)\]", raw):
            fields_str = mm.group(1)
            # 分割字段（兼容引号内的逗号）
            fields = _smart_split(fields_str)
            if len(fields) < 11:
                continue
            try:
                match_id = int(fields[0])
                home_id = int(fields[4])
                away_id = int(fields[5])
                full_score = fields[6].strip("'\"")
                half_score = fields[7].strip("'\"")
                match_time = fields[3].strip("'\"")
                home_rank = fields[8].strip("'\"")
                away_rank = fields[9].strip("'\"")
                handicap_str = fields[10].strip("'\"")
                handicap = float(handicap_str) if handicap_str else 0.0
            except (ValueError, IndexError):
                continue

            matches.append({
                "match_id": match_id,
                "match_time": match_time,
                "home_id": home_id,
                "away_id": away_id,
                "full_score": full_score,
                "half_score": half_score,
                "home_rank": home_rank,
                "away_rank": away_rank,
                "handicap": handicap,
            })
        if matches:
            rounds.append({
                "round": round_num,
                "matches": matches,
            })
    return rounds


def parse_team_stat_array(text, var_name, team_map, field_names):
    """解析 JS 中的球队统计数组（如 TotalBs, TotalPanLu 等）
    格式: [[idx, teamId, ...], [idx, teamId, ...]]
    field_names: 字段名列表，如 ["index", "team_id", "total", "over", "draw", "under", "over_pct", "draw_pct", "under_pct"]
    返回: [{field: value, team_name: ...}, ...]
    """
    raw = parse_js_array(text, var_name)
    if not raw:
        return []
    result = []
    for m in re.finditer(r"\[(.*?)\]", raw):
        vals = _smart_split(m.group(1))
        if len(vals) < len(field_names):
            continue
        row = {}
        for i, name in enumerate(field_names):
            raw_val = vals[i].strip("'\"")
            try:
                if "." in raw_val:
                    row[name] = float(raw_val)
                else:
                    row[name] = int(raw_val)
            except (ValueError, IndexError):
                row[name] = raw_val
        tid = row.get("team_id", 0)
        row["team_name"] = team_map.get(int(tid) if not isinstance(tid, int) else tid, str(tid))
        result.append(row)
    return result


def parse_handicap_array(text, var_name, team_map, field_names):
    """解析亚盘数据（PanLu 字段多了中间字段，特殊处理）"""
    raw = parse_js_array(text, var_name)
    if not raw:
        return []
    result = []
    for m in re.finditer(r"\[(.*?)\]", raw):
        vals = _smart_split(m.group(1))
        if len(vals) < len(field_names):
            continue
        row = {}
        for i, name in enumerate(field_names):
            raw_val = vals[i].strip("'\"")
            try:
                if "." in raw_val:
                    row[name] = float(raw_val)
                else:
                    row[name] = int(raw_val)
            except (ValueError, IndexError):
                row[name] = raw_val
        tid = row.get("team_id", 0)
        row["team_name"] = team_map.get(int(tid) if not isinstance(tid, int) else tid, str(tid))
        result.append(row)
    return result


def _smart_split(text):
    """智能分割逗号，处理引号内的内容"""
    result = []
    current = []
    in_quote = False
    quote_char = None
    for ch in text:
        if in_quote:
            if ch == quote_char:
                in_quote = False
            current.append(ch)
            continue
        if ch in ("'", '"'):
            in_quote = True
            quote_char = ch
            current.append(ch)
            continue
        if ch == ",":
            result.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    if current:
        result.append("".join(current).strip())
    return result


def parse_file(filepath):
    """解析单个 联赛数据.txt 文件，返回结构化数据"""
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    # 内部保留ID映射以填充队名，输出时只存名称列表
    team_id_to_name = {}
    raw = parse_js_array(content, "arrTeam")
    if raw:
        for m in re.finditer(r"\[(\d+),'([^']*)'", raw):
            team_id_to_name[int(m.group(1))] = m.group(2)
    team_names = sorted(set(team_id_to_name.values()))

    rounds = parse_rounds(content)

    # 填充队伍名称
    all_matches = []
    for rd in rounds:
        for m in rd["matches"]:
            m["home_name"] = team_id_to_name.get(m["home_id"], str(m["home_id"]))
            m["away_name"] = team_id_to_name.get(m["away_id"], str(m["away_id"]))
            all_matches.append(m)

    return {
        "league_info": parse_league_name(content),
        "total_teams": len(team_names),
        "total_rounds": len(rounds),
        "total_matches": len(all_matches),
        "teams": team_names,
        "rounds": rounds,
        "matches": all_matches,
        "team_id_to_name": team_id_to_name,
    }


# 大小球字段定义
BS_FIELDS = ["index", "team_id", "total_games", "over", "draw", "under",
             "over_pct", "draw_pct", "under_pct"]

# 亚盘盘路字段定义
PANLU_FIELDS = ["index", "team_id", "total_games", "up", "mid", "down",
                "win", "draw", "lose", "net", "win_pct", "draw_pct", "lose_pct"]


def parse_big_small(content, team_map):
    """解析 大小球数据.txt 内容，返回 {'total': ..., 'home': ..., 'guest': ..., ...}"""
    result = {}
    for key, var in [("total", "TotalBs"), ("home", "HomeBs"), ("guest", "GuestBs"),
                     ("half_total", "TotalBsHalf"), ("half_home", "HomeBsHalf"), ("half_guest", "GuestBsHalf")]:
        result[key] = parse_team_stat_array(content, var, team_map, BS_FIELDS)
    return result


def parse_handicap(content, team_map):
    """解析 亚盘让球数据.txt 内容，返回 {'total_panlu': ..., 'home': ..., 'guest': ..., ...}"""
    result = {}
    for key, var in [("total_panlu", "TotalPanLu"), ("home", "HomePanLu"), ("guest", "GuestPanLu"),
                     ("half_total", "TotalHalfPanLu"), ("half_home", "HomeHalfPanLu"), ("half_guest", "GuestHalfPanLu")]:
        result[key] = parse_handicap_array(content, var, team_map, PANLU_FIELDS)
    return result


# 半全场结果编码（无分隔符格式）
HF_CODES = ["33", "31", "30", "13", "11", "10", "03", "01", "00"]

# 半全场反转映射（客队视角 ↔ 主队视角）
HF_INVERT = {
    "33": "00", "31": "01", "30": "03",
    "13": "10", "11": "11", "10": "13",
    "03": "30", "01": "31", "00": "33",
}


def _get_hf_code(half_score, full_score):
    """从半场/全场比分计算半全场编码（主队视角）"""
    try:
        hh, ha = map(int, half_score.split("-"))
        fh, fa = map(int, full_score.split("-"))
    except (ValueError, AttributeError):
        return None
    hr = "3" if hh > ha else ("1" if hh == ha else "0")
    fr = "3" if fh > fa else ("1" if fh == fa else "0")
    return hr + fr


def compute_half_full(matches, team_names):
    """从比赛数据统计每支球队的半全场表现
    返回: {total: [{team_name, total_games, 33, 31, ...}], home: [...], guest: [...]}
    """
    # 初始化
    total_stats = {}
    home_stats = {}
    guest_stats = {}
    for name in team_names:
        total_stats[name] = {"team_name": name, "total_games": 0}
        home_stats[name] = {"team_name": name, "total_games": 0}
        guest_stats[name] = {"team_name": name, "total_games": 0}
        for c in HF_CODES:
            total_stats[name][c] = 0
            home_stats[name][c] = 0
            guest_stats[name][c] = 0

    for m in matches:
        hf_code = _get_hf_code(m.get("half_score", ""), m.get("full_score", ""))
        if not hf_code:
            continue
        home_name = m.get("home_name", "")
        away_name = m.get("away_name", "")

        # 主队统计
        if home_name in total_stats:
            total_stats[home_name]["total_games"] += 1
            total_stats[home_name][hf_code] += 1
            home_stats[home_name]["total_games"] += 1
            home_stats[home_name][hf_code] += 1

        # 客队统计（反转视角）
        inv_hf = HF_INVERT.get(hf_code, hf_code)
        if away_name in total_stats:
            total_stats[away_name]["total_games"] += 1
            total_stats[away_name][inv_hf] += 1
            guest_stats[away_name]["total_games"] += 1
            guest_stats[away_name][inv_hf] += 1

    def to_list(d):
        return [v for v in d.values() if v["total_games"] > 0]

    return {
        "total": to_list(total_stats),
        "home": to_list(home_stats),
        "guest": to_list(guest_stats),
    }


def compute_odd_even(matches, team_names):
    """从比赛数据统计每支球队的单双（总进球奇偶）
    返回: {total: [{team_name, total_games, odd, even}], home: [...], guest: [...]}
    """
    total_stats = {}
    home_stats = {}
    guest_stats = {}
    for name in team_names:
        total_stats[name] = {"team_name": name, "total_games": 0, "odd": 0, "even": 0}
        home_stats[name] = {"team_name": name, "total_games": 0, "odd": 0, "even": 0}
        guest_stats[name] = {"team_name": name, "total_games": 0, "odd": 0, "even": 0}

    for m in matches:
        try:
            fh, fa = map(int, m.get("full_score", "").split("-"))
        except (ValueError, AttributeError):
            continue
        total_goals = fh + fa
        is_odd = total_goals % 2 == 1
        home_name = m.get("home_name", "")
        away_name = m.get("away_name", "")

        # 主队视角
        if home_name in total_stats:
            total_stats[home_name]["total_games"] += 1
            total_stats[home_name]["odd" if is_odd else "even"] += 1
            home_stats[home_name]["total_games"] += 1
            home_stats[home_name]["odd" if is_odd else "even"] += 1

        # 客队视角（单双不反转，全场总进球一样）
        if away_name in total_stats:
            total_stats[away_name]["total_games"] += 1
            total_stats[away_name]["odd" if is_odd else "even"] += 1
            guest_stats[away_name]["total_games"] += 1
            guest_stats[away_name]["odd" if is_odd else "even"] += 1

    def to_list(d):
        return [v for v in d.values() if v["total_games"] > 0]

    return {
        "total": to_list(total_stats),
        "home": to_list(home_stats),
        "guest": to_list(guest_stats),
    }


def compute_goals(matches, team_names):
    """从比赛数据统计每支球队的进球数分布（0,1,2,3,4,5+）
    返回: {total: [{team_name, total_games, 0,1,2,3,4,5}], home: [...], guest: [...]}
    """
    goal_labels = ["0", "1", "2", "3", "4", "5+"]
    total_stats = {}
    home_stats = {}
    guest_stats = {}
    for name in team_names:
        total_stats[name] = {"team_name": name, "total_games": 0}
        home_stats[name] = {"team_name": name, "total_games": 0}
        guest_stats[name] = {"team_name": name, "total_games": 0}
        for gl in goal_labels:
            total_stats[name][gl] = 0
            home_stats[name][gl] = 0
            guest_stats[name][gl] = 0

    for m in matches:
        try:
            fh, fa = map(int, m.get("full_score", "").split("-"))
        except (ValueError, AttributeError):
            continue
        total_goals = fh + fa
        bucket = f"{min(total_goals, 5)}+" if total_goals >= 5 else str(total_goals)

        home_name = m.get("home_name", "")
        away_name = m.get("away_name", "")

        for name in (home_name, away_name):
            if name in total_stats:
                total_stats[name]["total_games"] += 1
                total_stats[name][bucket] += 1

        if home_name in home_stats:
            home_stats[home_name]["total_games"] += 1
            home_stats[home_name][bucket] += 1

        if away_name in guest_stats:
            guest_stats[away_name]["total_games"] += 1
            guest_stats[away_name][bucket] += 1

    def to_list(d):
        return [v for v in d.values() if v["total_games"] > 0]

    return {
        "total": to_list(total_stats),
        "home": to_list(home_stats),
        "guest": to_list(guest_stats),
    }


def compute_scores(matches, team_names):
    """从比赛数据统计每支球队的比分分布
    总进球<5 → 精确比分（如 0-0, 0-1, ..., 4-4），总进球≥5 → 归入 "5+"
    total/guest 使用该队视角的比分（客队时反转）
    返回: {total: [...], home: [...], guest: [...]}
    """
    score_buckets = [f"{h}-{a}" for h in range(5) for a in range(5)]
    score_buckets.append("5+")
    all_buckets = set(score_buckets)

    total_stats = {}
    home_stats = {}
    guest_stats = {}
    for name in team_names:
        total_stats[name] = {"team_name": name, "total_games": 0}
        home_stats[name] = {"team_name": name, "total_games": 0}
        guest_stats[name] = {"team_name": name, "total_games": 0}
        for b in score_buckets:
            total_stats[name][b] = 0
            home_stats[name][b] = 0
            guest_stats[name][b] = 0

    for m in matches:
        try:
            fh, fa = map(int, m.get("full_score", "").split("-"))
        except (ValueError, AttributeError):
            continue

        total_goals = fh + fa
        home_name = m.get("home_name", "")
        away_name = m.get("away_name", "")

        # 主队视角比分
        home_score_key = f"{fh}-{fa}" if total_goals < 5 else "5+"
        # 客队视角比分（反转）
        away_score_key = f"{fa}-{fh}" if total_goals < 5 else "5+"

        # 总统计（用该队视角）
        if home_name in total_stats:
            total_stats[home_name]["total_games"] += 1
            total_stats[home_name][home_score_key] += 1
        if away_name in total_stats:
            total_stats[away_name]["total_games"] += 1
            total_stats[away_name][away_score_key] += 1

        # 主场统计（主队视角）
        if home_name in home_stats:
            home_stats[home_name]["total_games"] += 1
            home_stats[home_name][home_score_key] += 1

        # 客场统计（客队视角，已反转）
        if away_name in guest_stats:
            guest_stats[away_name]["total_games"] += 1
            guest_stats[away_name][away_score_key] += 1

    def to_list(d):
        return [v for v in d.values() if v["total_games"] > 0]

    return {
        "total": to_list(total_stats),
        "home": to_list(home_stats),
        "guest": to_list(guest_stats),
    }


def convert_league(league_name, season):
    """转化单个联赛某个赛季的数据"""
    src_path = os.path.join(INPUT_DIR, league_name, season, "联赛数据.txt")
    if not os.path.exists(src_path):
        print(f"  ⚠ 未找到文件: {src_path}")
        return False

    # 增量模式：输出已存在则跳过
    out_path = os.path.join(OUTPUT_DIR, league_name, f"{season}.json")
    if MODE == "INCREMENTAL" and os.path.exists(out_path):
        return True, True  # (成功, 跳过)

    base_dir = os.path.join(INPUT_DIR, league_name, season)

    print(f"\n  处理: {league_name}/{season}")
    data = parse_file(src_path)
    team_map = data.pop("team_id_to_name", {})
    print(f"    联赛数据: {data['total_teams']} 队, {data['total_rounds']} 轮, {data['total_matches']} 场比赛")

    # 读取并合并大小球数据
    bs_path = os.path.join(base_dir, "大小球数据.txt")
    if os.path.exists(bs_path):
        with open(bs_path, "r", encoding="utf-8", errors="replace") as f:
            bs_content = f.read()
        bs_data = parse_big_small(bs_content, team_map)
        data["big_small"] = bs_data
        total_bs = bs_data.get("total", [])
        print(f"    大小球: {len(total_bs)} 支球队")

    # 读取并合并亚盘让球数据
    hc_path = os.path.join(base_dir, "亚盘让球数据.txt")
    if os.path.exists(hc_path):
        with open(hc_path, "r", encoding="utf-8", errors="replace") as f:
            hc_content = f.read()
        hc_data = parse_handicap(hc_content, team_map)
        data["handicap"] = hc_data
        total_hc = hc_data.get("total_panlu", [])
        print(f"    亚盘: {len(total_hc)} 支球队")

    # 半全场统计（从比赛数据计算）
    hf_stats = compute_half_full(data["matches"], data["teams"])
    data["half_full"] = hf_stats
    hf_total = len(hf_stats.get("total", []))
    print(f"    半全场: {hf_total} 支球队")

    # 单双统计（从比赛数据计算）
    oe_stats = compute_odd_even(data["matches"], data["teams"])
    data["odd_even"] = oe_stats
    oe_total = len(oe_stats.get("total", []))
    print(f"    单双: {oe_total} 支球队")

    # 进球数分布（从比赛数据计算）
    gl_stats = compute_goals(data["matches"], data["teams"])
    data["goals"] = gl_stats
    gl_total = len(gl_stats.get("total", []))
    print(f"    进球数: {gl_total} 支球队")

    # 比分分布（从比赛数据计算）
    sc_stats = compute_scores(data["matches"], data["teams"])
    data["scores"] = sc_stats
    sc_total = len(sc_stats.get("total", []))
    print(f"    比分: {sc_total} 支球队")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"    ✓ 已保存: {out_path} ({os.path.getsize(out_path):,} bytes)")
    return True, False


def main():
    """自动扫描初始数据目录，转化所有联赛所有赛季"""
    if not os.path.isdir(INPUT_DIR):
        print(f"错误: 初始数据目录不存在 ({INPUT_DIR})")
        return

    leagues = sorted([d for d in os.listdir(INPUT_DIR)
                      if os.path.isdir(os.path.join(INPUT_DIR, d)) and not d.startswith(".")])
    if not leagues:
        print(f"初始数据目录下未找到任何联赛文件夹")
        return

    print(f"{'=' * 60}")
    print(f"  自动扫描: {INPUT_DIR}")
    print(f"  模式: {'全量' if MODE == 'FULL' else '增量（跳过已有）'}")
    print(f"  发现 {len(leagues)} 个联赛: {', '.join(leagues)}")
    print(f"  输出: {OUTPUT_DIR}")
    print(f"{'=' * 60}")

    total_ok = 0
    total_skip = 0
    total_all = 0
    for league_name in leagues:
        league_dir = os.path.join(INPUT_DIR, league_name)
        seasons = sorted([d for d in os.listdir(league_dir)
                          if os.path.isdir(os.path.join(league_dir, d)) and not d.startswith(".")])
        if not seasons:
            continue

        print(f"\n  ▶ {league_name} ({len(seasons)} 个赛季)")

        ok = 0
        skipped = 0
        for season in seasons:
            result, was_skipped = convert_league(league_name, season)
            if result:
                ok += 1
                if was_skipped:
                    skipped += 1
            time.sleep(0.05)

        total_ok += ok
        total_all += len(seasons)
        total_skip += skipped
        print(f"  ✓ {league_name}: {ok}/{len(seasons)} 个赛季完成{' (' + str(len(seasons)-ok) + '个跳过)' if MODE == 'INCREMENTAL' and len(seasons)-ok > 0 else ''}")

    print(f"\n{'=' * 60}")
    print(f"  全部完成! 成功 {total_ok}/{total_all} 个赛季"
          f"{f' (跳过 {total_skip})' if MODE == 'INCREMENTAL' and total_skip > 0 else ''}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
