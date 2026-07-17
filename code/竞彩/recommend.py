#!/usr/bin/env python3
"""
竞彩投注推荐器（胜平负+让球版）
支持两种输入方式：
1. 从txt文件读取多场比赛数据，批量生成推荐
2. 交互式逐场输入
txt文件格式：
  每场比赛用 [match] 开头，包含以下字段：
    league=英超
    home=曼城
    away=伯恩利
    handicap=-2
    wdl_3=1.85        (胜平负-主胜赔率，留空表示没开)
    wdl_1=3.50        (胜平负-平局赔率)
    wdl_0=4.20        (胜平负-客胜赔率)
    handicap_3=1.85   (让球-赢盘赔率)
    handicap_1=3.50   (让球-走盘赔率)
    handicap_0=4.20   (让球-输盘赔率)
    (没开的玩法直接不写即可)
用法：
  python3 recommend.py --file input.txt     # 从txt文件读取
  python3 recommend.py                       # 交互式输入
"""
import os
import sys
import json
import sqlite3
import math
import re
import glob
from datetime import datetime
BASE_DIR = "/Users/yzq/Desktop/sports_db/code/竞彩"
DB_PATH = os.path.join(BASE_DIR, "pattern_db.sqlite")
# ============ 玩法映射 ============
WDL_MAP = {"3": "主胜", "1": "平局", "0": "客胜"}
HANDICAP_RESULT_MAP = {"3": "赢盘", "1": "走盘", "0": "输盘"}
HALF_FULL_MAP = {
    "3-3": "主胜-主胜", "1-1": "平-平", "0-0": "客胜-客胜",
    "1-3": "平-主胜", "1-0": "平-客胜", "3-1": "主胜-平",
    "3-0": "主胜-客胜", "0-1": "客胜-平", "0-3": "客胜-主胜",
}
GOAL_MAP = {
    "0": "0球", "1": "1球", "2": "2球", "3": "3球",
    "4": "4球", "5": "5球", "6": "6球", "7": "7+球",
}

# 各玩法的配置
BET_TYPES = {
    "wdl": {
        "name": "胜平负",
        "prefix": "wdl",
        "results": ["3", "1", "0"],
        "result_map": WDL_MAP,
    },
    "handicap": {
        "name": "让球胜平负",
        "prefix": "handicap",
        "results": ["3", "1", "0"],
        "result_map": HANDICAP_RESULT_MAP,
    },
}
# 置信度分级
CONFIDENCE = [(500, "★★★ 高"), (200, "★★ 中"), (50, "★ 低"), (0, "⚠ 极低")]

# ============ 赔率分桶配置（与 update_pattern_db.py 保持一致） ============

ODDS_BUCKET_CONFIG = {
    "wdl": [(1.01, 1.3), (1.3, 1.6), (1.6, 2.0), (2.0, 2.5), (2.5, 3.5), (3.5, 5.0), (5.0, 8.0), (8.0, 99)],
    "handicap": [(1.01, 1.4), (1.4, 1.7), (1.7, 2.0), (2.0, 2.5), (2.5, 3.5), (3.5, 5.0), (5.0, 99)],
}


def get_odds_bucket(bet_type, odds):
    """根据赔率值返回对应的分桶标签"""
    if not odds or odds <= 0:
        return None
    for lo, hi in ODDS_BUCKET_CONFIG.get(bet_type, []):
        if lo <= odds < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return None


def get_adjacent_buckets(bet_type, bucket_label):
    """返回与当前桶按距离递增的相邻桶标签列表。"""
    conf = ODDS_BUCKET_CONFIG.get(bet_type, [])
    labels = [f"{lo:.2f}-{hi:.2f}" for lo, hi in conf]
    if bucket_label not in labels:
        return []
    idx = labels.index(bucket_label)
    out = []
    step = 1
    while idx - step >= 0 or idx + step < len(labels):
        if idx - step >= 0:
            out.append(labels[idx - step])
        if idx + step < len(labels):
            out.append(labels[idx + step])
        step += 1
    return out
# 查询层级优先级
LEVELS = ["league_handicap", "league", "handicap", "global"]
def safe_float(val, default=None):
    if val is None or val == "" or val == "-":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default
def get_confidence(sample):
    for threshold, label in CONFIDENCE:
        if sample >= threshold:
            return label
    return "⚠ 极低"
def confidence_score(sample):
    """将样本量映射为0~1置信分数，用于排序加权"""
    if sample is None or sample <= 0:
        return 0.0
    return min(1.0, sample / 500.0)
def cold_index(hit_rate, odds, confidence):
    """冷门指数(0~100): 越高表示越偏冷/爆冷属性越强"""
    if not odds or odds <= 0:
        return 0.0
    hit_rate = max(0.0, min(1.0, hit_rate or 0.0))
    conf = max(0.0, min(1.0, confidence or 0.0))
    rare = 1.0 - hit_rate
    odds_factor = min(1.0, math.log(max(odds, 1.01)) / math.log(50.0))
    # 样本置信度低时，对冷门指数降温，避免小样本异常放大
    conf_factor = 0.5 + 0.5 * conf
    # 去掉EV影响，冷门仅反映“低命中+高赔率”的相对爆冷属性
    score = (0.65 * rare + 0.35 * odds_factor) * conf_factor
    return max(0.0, min(100.0, score * 100.0))
def connect_db():
    if not os.path.exists(DB_PATH):
        print(f"错误: 模式库不存在 ({DB_PATH})")
        print("请先运行: python3 update_pattern_db.py")
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
def query_patterns(conn, granularity, league=None, handicap=None, bet_type=None):
    """查询指定维度的模式统计"""
    if granularity == "global":
        rows = conn.execute("""
            SELECT result_key, sample_count, hit_count, hit_rate, avg_odds, min_odds, max_odds, expected_value
            FROM patterns WHERE granularity='global' AND bet_type=? AND odds_bucket IS NULL ORDER BY hit_rate DESC
        """, (bet_type,)).fetchall()
    elif granularity == "league":
        rows = conn.execute("""
            SELECT result_key, sample_count, hit_count, hit_rate, avg_odds, min_odds, max_odds, expected_value
            FROM patterns WHERE granularity='league' AND league=? AND bet_type=? AND odds_bucket IS NULL ORDER BY hit_rate DESC
        """, (league, bet_type)).fetchall()
    elif granularity == "handicap":
        rows = conn.execute("""
            SELECT result_key, sample_count, hit_count, hit_rate, avg_odds, min_odds, max_odds, expected_value
            FROM patterns WHERE granularity='handicap' AND handicap=? AND bet_type=? AND odds_bucket IS NULL ORDER BY hit_rate DESC
        """, (handicap, bet_type)).fetchall()
    elif granularity == "league_handicap":
        rows = conn.execute("""
            SELECT result_key, sample_count, hit_count, hit_rate, avg_odds, min_odds, max_odds, expected_value
            FROM patterns WHERE granularity='league_handicap' AND league=? AND handicap=? AND bet_type=? AND odds_bucket IS NULL ORDER BY hit_rate DESC
        """, (league, handicap, bet_type)).fetchall()
    else:
        return []
    return [dict(r) for r in rows]
# 置信度分级阈值（用于平衡不同粒度的数据）
SAMPLE_THRESHOLDS = [
    (1000, 1.0, "★★★ 充足"),
    (500, 0.9, "★★☆ 较多"),
    (200, 0.75, "★★ 中等"),
    (50, 0.5, "★ 偏低"),
    (0, 0.0, "⚠ 不足"),
]


def get_sample_tier(sample_count):
    """返回 (阈值下限, 置信权重, 标签)"""
    for threshold, weight, label in SAMPLE_THRESHOLDS:
        if sample_count >= threshold:
            return threshold, weight, label
    return 0, 0.0, "⚠ 不足"


def find_best_level(conn, league, handicap, bet_type):
    """找到样本量最充足的最高精确度层级
    返回: (primary_level, primary_data, fallback_level, fallback_data)
    - primary: 最精准且样本足够的层级
    - fallback: 当 primary 样本偏低(<200)时，用于融合的更高层粒度数据
    """
    if handicap:
        candidate_levels = LEVELS
    else:
        candidate_levels = ["league", "global"]

    primary_level = "global"
    primary_data = query_patterns(conn, "global", bet_type=bet_type)
    fallback_level = None
    fallback_data = None

    for level in candidate_levels:
        if level == "league_handicap":
            data = query_patterns(conn, level, league=league, handicap=handicap, bet_type=bet_type)
        elif level == "league":
            data = query_patterns(conn, level, league=league, bet_type=bet_type)
        elif level == "handicap":
            data = query_patterns(conn, level, handicap=handicap, bet_type=bet_type)
        else:
            data = query_patterns(conn, level, bet_type=bet_type)

        if not data:
            continue

        max_sample = max(d["sample_count"] for d in data)
        _, tier_weight, _ = get_sample_tier(max_sample)

        if tier_weight >= 1.0:
            # ≥1000: 高置信度，不需要融合
            return level, data, None, None
        elif tier_weight >= 0.75:
            # 200-999: 中等置信度，存当前为 primary，之前的降为 fallback
            fallback_level, fallback_data = primary_level, primary_data
            primary_level, primary_data = level, data
        elif tier_weight >= 0.5:
            # 50-199: 低置信度，存当前为 primary，之前的降为 fallback
            fallback_level, fallback_data = primary_level, primary_data
            primary_level, primary_data = level, data
        # <50: 跳过

    return primary_level, primary_data, fallback_level, fallback_data
def query_team_patterns(conn, scope, team_name, bet_type, league=None):
    if not team_name:
        return []
    if league:
        rows = conn.execute("""
            SELECT result_key, sample_count, hit_count, hit_rate, avg_odds, min_odds, max_odds, expected_value
            FROM team_patterns
            WHERE scope=? AND league=? AND team_name=? AND bet_type=?
            ORDER BY hit_rate DESC
        """, (scope, league, team_name, bet_type)).fetchall()
        if rows:
            return [dict(r) for r in rows]
    rows = conn.execute("""
        SELECT result_key,
               SUM(sample_count) AS sample_count,
               SUM(hit_count) AS hit_count,
               CASE WHEN SUM(sample_count) > 0 THEN 1.0 * SUM(hit_count) / SUM(sample_count) ELSE 0 END AS hit_rate,
               CASE WHEN SUM(hit_count) > 0 THEN 1.0 * SUM(avg_odds * hit_count) / SUM(hit_count) ELSE 0 END AS avg_odds,
               MIN(min_odds) AS min_odds,
               MAX(max_odds) AS max_odds,
               0 AS expected_value
        FROM team_patterns
        WHERE scope=? AND team_name=? AND bet_type=?
        GROUP BY result_key
        ORDER BY hit_rate DESC
    """, (scope, team_name, bet_type)).fetchall()
    return [dict(r) for r in rows]
def get_team_league_share(conn, team_name, league):
    if not team_name or not league:
        return 0.0, 0
    row = conn.execute("""
        SELECT league_share, is_primary_league
        FROM team_league_profile
        WHERE team_name=? AND league=?
    """, (team_name, league)).fetchone()
    if not row:
        return 0.0, 0
    return float(row[0] or 0), int(row[1] or 0)


def query_odds_bucketed(conn, granularity, bet_type, result_key, odds_bucket,
                        league=None, handicap=None):
    """查询单个结果在赔率分桶内的历史统计。odds_bucket 列不存在时返回 None。"""
    try:
        if granularity == "league_handicap":
            row = conn.execute("""
                SELECT sample_count, hit_count, hit_rate, avg_odds, expected_value
                FROM patterns WHERE granularity='league_handicap' AND league=? AND handicap=?
                AND bet_type=? AND result_key=? AND odds_bucket=?
            """, (league, handicap, bet_type, result_key, odds_bucket)).fetchone()
        elif granularity == "league":
            row = conn.execute("""
                SELECT sample_count, hit_count, hit_rate, avg_odds, expected_value
                FROM patterns WHERE granularity='league' AND league=?
                AND bet_type=? AND result_key=? AND odds_bucket=?
            """, (league, bet_type, result_key, odds_bucket)).fetchone()
        elif granularity == "handicap":
            row = conn.execute("""
                SELECT sample_count, hit_count, hit_rate, avg_odds, expected_value
                FROM patterns WHERE granularity='handicap' AND handicap=?
                AND bet_type=? AND result_key=? AND odds_bucket=?
            """, (handicap, bet_type, result_key, odds_bucket)).fetchone()
        else:  # global
            row = conn.execute("""
                SELECT sample_count, hit_count, hit_rate, avg_odds, expected_value
                FROM patterns WHERE granularity='global'
                AND bet_type=? AND result_key=? AND odds_bucket=?
            """, (bet_type, result_key, odds_bucket)).fetchone()
        if row:
            return {"sample_count": row[0], "hit_count": row[1], "hit_rate": row[2],
                    "avg_odds": row[3], "expected_value": row[4]}
        return None
    except Exception as e:
        return None


def query_recent_hit_rate(conn, granularity, bet_type, result_key, days=180, league=None, handicap=None):
    """按近N天窗口查询命中率（从 matches 原始表实时聚合）。"""
    col_map = {
        "wdl": "wdl_result",
        "handicap": "handicap_result",
    }
    result_col = col_map.get(bet_type)
    if not result_col:
        return None

    where = ["is_valid = 1", f"{result_col} != ''", "date(match_date) >= date('now', ?)"]
    params = [f"-{int(days)} day"]

    if granularity == "league_handicap":
        where.append("league = ?")
        where.append("handicap = ?")
        params.extend([league, handicap])
    elif granularity == "league":
        where.append("league = ?")
        params.append(league)
    elif granularity == "handicap":
        where.append("handicap = ?")
        params.append(handicap)

    sql = f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN {result_col} = ? THEN 1 ELSE 0 END) AS hit
        FROM matches
        WHERE {' AND '.join(where)}
    """
    params.append(result_key)
    row = conn.execute(sql, params).fetchone()
    if not row:
        return None
    total = int(row[0] or 0)
    hit = int(row[1] or 0)
    if total <= 0:
        return None
    return {"sample_count": total, "hit_count": hit, "hit_rate": hit / total}


def is_cross_play_conflict(match_item):
    """同一场里，胜平负与让球胜平负是否方向相悖。"""
    top = match_item.get("pick")
    hc = (match_item.get("handicap_pick") or {}).get("pick")
    if not top or not hc:
        return False

    if match_item.get("bet_type") != "wdl":
        return False

    top_rk = top.get("result_key")
    hc_rk = hc.get("result_key")

    # 胜平负主胜 + 让球输盘，或 胜平负客胜 + 让球赢盘
    if top_rk == "3" and hc_rk == "0":
        return True
    if top_rk == "0" and hc_rk == "3":
        return True
    return False


def stability_score(pick):
    """用于冲突替换时比较稳定性（越大越稳）。"""
    if not pick:
        return -1.0
    return (
        pick.get("confidence_score", 0) * 1.2
        + pick.get("hit_rate", 0)
        + min(0.2, pick.get("current_odds", 0) / 20.0)
    )


def pick_best_pair(candidates, pick_field="pick", same_league_penalty=0.20):
    """从候选里选两场，优先高稳定性，同时惩罚同联赛相关性。"""
    if len(candidates) < 2:
        return None, None
    best = None
    best_score = -1e9
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            a = candidates[i]
            b = candidates[j]
            pa = a.get(pick_field) if pick_field == "pick" else a.get(pick_field, {}).get("pick")
            pb = b.get(pick_field) if pick_field == "pick" else b.get(pick_field, {}).get("pick")
            if not pa or not pb:
                continue
            score = stability_score(pa) + stability_score(pb)
            if (a.get("league") or "") == (b.get("league") or ""):
                score -= same_league_penalty
            if score > best_score:
                best_score = score
                best = (a, b)
    return best if best else (None, None)
def recommend_bet_type(conn, league, handicap, home, away, bet_type, input_odds, bet_info):
    """对单个玩法生成推荐（联赛+球队风格加权）"""
    best_level, best_data, fallback_level, fallback_data = find_best_level(conn, league, handicap, bet_type)
    hit_rate_map = {d["result_key"]: d["hit_rate"] for d in best_data}
    avg_odds_map = {d["result_key"]: d["avg_odds"] for d in best_data}
    sample_map = {d["result_key"]: d["sample_count"] for d in best_data}

    # 低置信度时（primary 样本<200），从 fallback 取更粗粒度的命中率做软化融合
    primary_max_sample = max(sample_map.values()) if sample_map else 0
    _, primary_tier_weight, _ = get_sample_tier(primary_max_sample)
    fallback_hit_map = {}
    fallback_sample_map = {}
    if fallback_data and primary_tier_weight < 0.75:
        fallback_hit_map = {d["result_key"]: d["hit_rate"] for d in fallback_data}
        fallback_sample_map = {d["result_key"]: d["sample_count"] for d in fallback_data}

    league_rows = query_patterns(conn, "league", league=league, bet_type=bet_type) or query_patterns(conn, "global", bet_type=bet_type)
    league_hit_map = {d["result_key"]: d["hit_rate"] for d in league_rows}
    league_sample_map = {d["result_key"]: d["sample_count"] for d in league_rows}
    home_home_rows = query_team_patterns(conn, "home", home, bet_type, league=league)
    away_away_rows = query_team_patterns(conn, "away", away, bet_type, league=league)
    home_overall_rows = query_team_patterns(conn, "overall", home, bet_type)
    away_overall_rows = query_team_patterns(conn, "overall", away, bet_type)
    hh_hit = {d["result_key"]: d["hit_rate"] for d in home_home_rows}
    hh_sample = {d["result_key"]: d["sample_count"] for d in home_home_rows}
    aa_hit = {d["result_key"]: d["hit_rate"] for d in away_away_rows}
    aa_sample = {d["result_key"]: d["sample_count"] for d in away_away_rows}
    ho_hit = {d["result_key"]: d["hit_rate"] for d in home_overall_rows}
    ho_sample = {d["result_key"]: d["sample_count"] for d in home_overall_rows}
    ao_hit = {d["result_key"]: d["hit_rate"] for d in away_overall_rows}
    ao_sample = {d["result_key"]: d["sample_count"] for d in away_overall_rows}
    result_map = bet_info.get("result_map") or {}
    # 主联赛占比：用于动态调权（主联赛占比高 -> 权重放大）
    home_share, home_is_primary = get_team_league_share(conn, home, league)
    away_share, away_is_primary = get_team_league_share(conn, away, league)
    home_mult = 0.5 + min(1.0, home_share)          # [0.5, 1.5]
    away_mult = 0.5 + min(1.0, away_share)          # [0.5, 1.5]
    league_mult = 1.2 if (home_is_primary and away_is_primary) else 1.0
    # 赔率分桶预取：对每个结果查询同赔率区间的历史命中率
    ob_hit_map = {}
    ob_sample_map = {}
    for rk, odds in input_odds.items():
        if not odds or odds <= 0:
            continue
        ob = get_odds_bucket(bet_type, odds)
        if not ob:
            continue
        ob_row = query_odds_bucketed(conn, best_level, bet_type, rk, ob,
                                     league=league, handicap=handicap)
        if ob_row and ob_row["sample_count"] >= 10:
            ob_hit_map[rk] = ob_row["hit_rate"]
            ob_sample_map[rk] = ob_row["sample_count"]

    # 近180天窗口预取
    recent_hit_map = {}
    recent_sample_map = {}
    for rk in input_odds.keys():
        rrow = query_recent_hit_rate(
            conn, best_level, bet_type, rk, days=180, league=league, handicap=handicap
        )
        if rrow and rrow["sample_count"] >= 20:
            recent_hit_map[rk] = rrow["hit_rate"]
            recent_sample_map[rk] = rrow["sample_count"]
    def compute_weighted_prob(rk):
        overall_n = ho_sample.get(rk, 0) + ao_sample.get(rk, 0)
        overall_p = ((ho_hit.get(rk, 0) * ho_sample.get(rk, 0)) + (ao_hit.get(rk, 0) * ao_sample.get(rk, 0))) / (overall_n or 1)
        raw_sources = [
            ("league", league_hit_map.get(rk, hit_rate_map.get(rk, 0)), league_sample_map.get(rk, 0), 0.40 * league_mult),
            ("home_home", hh_hit.get(rk, 0), hh_sample.get(rk, 0), 0.20 * home_mult),
            ("away_away", aa_hit.get(rk, 0), aa_sample.get(rk, 0), 0.20 * away_mult),
            ("teams_overall", overall_p, overall_n, 0.20 * ((home_mult + away_mult) / 2.0)),
        ]
        weighted = []
        for name, p, n, base_w in raw_sources:
            conf = min(1.0, n / 200.0) if n > 0 else 0.0
            w = base_w * conf
            weighted.append((name, p, n, w))
        total_w = sum(x[3] for x in weighted)
        if total_w <= 0:
            historical_p = hit_rate_map.get(rk, 0)
        else:
            historical_p = sum(p * w for _, p, _, w in weighted) / total_w

        # === 层级降级融合：primary 样本不足时，与 fallback 混合 ===
        if primary_tier_weight < 0.75 and rk in fallback_hit_map:
            fb_p = fallback_hit_map[rk]
            fb_n = fallback_sample_map.get(rk, 0)
            fb_weight = max(0.1, min(0.5, 1.0 - primary_tier_weight))
            # 混合时考虑 fallback 自身的置信度
            fb_conf = min(1.0, fb_n / 200.0) if fb_n > 0 else 0.0
            fb_blend = fb_weight * fb_conf
            if fb_blend > 0:
                historical_p = historical_p * (1.0 - fb_blend) + fb_p * fb_blend

        # === 近期加权：50/50 混合历史与近期 ===
        recent_p = recent_hit_map.get(rk)
        recent_n = recent_sample_map.get(rk, 0)
        if recent_p is not None and recent_n >= 30:
            # 60/40 混合：历史权重稍高，避免小样本偏差
            final_p = 0.6 * historical_p + 0.4 * recent_p
            weighted.append(("recent_365d", recent_p, recent_n, 0.4))
        else:
            final_p = historical_p

        return final_p, weighted
    results = []
    for rk, odds in input_odds.items():
        if not odds or odds <= 0:
            continue
        hit_rate, source_weights = compute_weighted_prob(rk)
        hist_avg = avg_odds_map.get(rk, 0)
        sample = max(
            sample_map.get(rk, 0),
            league_sample_map.get(rk, 0),
            hh_sample.get(rk, 0),
            aa_sample.get(rk, 0),
            ho_sample.get(rk, 0) + ao_sample.get(rk, 0),
        )
        ev = hit_rate * odds - 1
        deviation = odds - hist_avg if hist_avg > 0 else None
        desc = result_map.get(rk, rk)
        conf_score = confidence_score(sample)
        cidx = cold_index(hit_rate, odds, conf_score)
        results.append({
            "result_key": rk,
            "description": desc,
            "current_odds": odds,
            "hit_rate": hit_rate,
            "hist_avg_odds": hist_avg,
            "odds_deviation": deviation,
            "ev": ev,
            "sample": sample,
            "confidence": get_confidence(sample),
            "confidence_score": conf_score,
            "cold_index": cidx,
            "level": best_level,
            "source_weights": source_weights,
        })
    # 同玩法内相对冷门指数（百分位），避免不同玩法赔率结构差异导致失真
    cold_values = sorted([r.get("cold_index", 0.0) for r in results])
    for r in results:
        if not cold_values:
            r["cold_percentile"] = 0.0
            continue
        less_or_equal = sum(1 for v in cold_values if v <= r.get("cold_index", 0.0))
        r["cold_percentile"] = less_or_equal / len(cold_values)
    # 全玩法统一：命中率优先；冷门仅用于同分细化，不参与主导
    max_hit_rate = max((r["hit_rate"] for r in results), default=0)
    close_gap = 0.03  # 命中率差距<=3%视为接近
    ev_bonus_scale = 0.06
    for r in results:
        gap = max_hit_rate - r["hit_rate"]
        ev = r["ev"] if r["ev"] is not None else -999
        ev_norm = math.tanh(ev / 2.0) if ev > -999 else -1
        close_ratio = max(0.0, 1.0 - gap / close_gap)
        # 冷门指数用于同分细化，不压过命中率+EV主逻辑
        cold_boost = (r.get("cold_index", 0.0) / 100.0) * 0.01
        r["hybrid_score"] = r["hit_rate"] + close_ratio * ev_bonus_scale * ev_norm + cold_boost
    results.sort(
        key=lambda r: (
            r.get("confidence_score", 0),
            r.get("hybrid_score", r["hit_rate"]),
            r["hit_rate"],
            r["ev"] if r["ev"] is not None else -999,
            r.get("cold_index", 0),
        ),
        reverse=True
    )
    all_levels = {}
    for level in LEVELS:
        if level == "league_handicap":
            all_levels[level] = query_patterns(conn, level, league=league, handicap=handicap, bet_type=bet_type)
        elif level == "league":
            all_levels[level] = query_patterns(conn, level, league=league, bet_type=bet_type)
        elif level == "handicap":
            all_levels[level] = query_patterns(conn, level, handicap=handicap, bet_type=bet_type)
        else:
            all_levels[level] = query_patterns(conn, level, bet_type=bet_type)

    # ============ 赔率分桶独立推荐（多桶加权平均）============
    ob_results = []
    for rk, odds in input_odds.items():
        if not odds or odds <= 0:
            continue
        ob = get_odds_bucket(bet_type, odds)
        if not ob:
            continue

        # 多桶加权平均：取最近3-5个桶，按距离倒数加权
        # 精确桶 + 相邻桶 + 再远桶
        all_candidate_buckets = [ob] + get_adjacent_buckets(bet_type, ob)[:4]
        # 距离权重：精确桶=1.0, 邻桶=0.6, 次邻=0.35, 更远=0.15
        dist_weights = [1.0, 0.6, 0.35, 0.15, 0.08]

        weighted_hit_sum = 0.0
        total_weight = 0.0
        total_sample = 0
        best_bucket = ob
        best_bucket_n = 0
        best_avg_odds = 0
        best_ev = 0
        active_buckets = 0

        for i, bucket in enumerate(all_candidate_buckets):
            dw = dist_weights[i] if i < len(dist_weights) else 0.05
            row = query_odds_bucketed(conn, best_level, bet_type, rk, bucket,
                                     league=league, handicap=handicap)
            if row and row["sample_count"] >= 5:
                sample_weight = min(1.0, row["sample_count"] / 100.0)
                w = dw * sample_weight
                weighted_hit_sum += row["hit_rate"] * w
                total_weight += w
                total_sample += row["sample_count"]
                active_buckets += 1
                if row["sample_count"] > best_bucket_n:
                    best_bucket = bucket
                    best_bucket_n = row["sample_count"]
                    best_avg_odds = row["avg_odds"]
                    best_ev = row["expected_value"]

        if total_weight > 0 and total_sample >= 10:
            weighted_hit = weighted_hit_sum / total_weight
            ob_results.append({
                "result_key": rk,
                "description": result_map.get(rk, rk) if result_map else rk,
                "current_odds": odds,
                "hit_rate": weighted_hit,
                "sample_count": total_sample,
                "avg_odds": best_avg_odds,
                "expected_value": best_ev,
                "odds_bucket": best_bucket,
                "input_bucket": ob,
                "bucket_source": f"多桶{active_buckets}个",
            })
    # 按命中率×赔率排序
    ob_results.sort(key=lambda r: r["hit_rate"] * r["current_odds"], reverse=True)

    return best_level, results, all_levels, ob_results
def print_bet_recommendation(bet_name, best_level, results, all_levels, league, handicap):
    """打印单个玩法的推荐结果"""
    level_names = {
        "league_handicap": f"{league}|handicap={handicap}",
        "league": f"{league}",
        "handicap": f"handicap={handicap}",
        "global": "全局",
    }
    if not results:
        print(f"\n  【{bet_name}】 无赔率输入，跳过")
        return
    max_sample = max(r["sample"] for r in results) if results else 0
    print(f"\n  【{bet_name}】 推荐依据: {level_names.get(best_level, best_level)} (样本={max_sample}场 {get_confidence(max_sample)})")
    print(f"  {'#':>2}  {'结果':<10} {'描述':<10} {'赔率':>6} {'命中率':>8} {'EV':>8} {'冷门指数':>8} {'赔率偏差':>8} {'置信度':<10}")
    print("  " + "-" * 60)
    # 精简输出：胜平负最多5条，其他玩法最多3条
    max_rows = 5 if bet_name == "胜平负" else 3
    display_results = results[:max_rows]

    for i, r in enumerate(display_results, 1):
        odds_str = f"{r['current_odds']:.2f}"
        ev_str = f"+{r['ev']:.3f}" if r["ev"] >= 0 else f"{r['ev']:.3f}"
        dev_str = f"+{r['odds_deviation']:.2f}" if r["odds_deviation"] is not None and r["odds_deviation"] >= 0 \
            else f"{r['odds_deviation']:.2f}" if r["odds_deviation"] is not None else "—"
        value_hint = ""
        if r["odds_deviation"] is not None:
            if r["odds_deviation"] > 0.5:
                value_hint = "偏高✓"
            elif r["odds_deviation"] < -0.5:
                value_hint = "偏低✗"
        show_cold = (bet_name == "胜平负")
        cold_str = f"{r.get('cold_index', 0):.1f}" if show_cold else "-"
        print(f"  {i:>2}  {r['result_key']:<10} {r['description']:<10} {odds_str:>6} "
              f"{r['hit_rate']:>7.1%} {ev_str:>8} {cold_str:>8} {dev_str:>8} {r['confidence']:<10} {value_hint}")
        # 其他玩法不展开来源长明细，减少噪音
        if bet_name == "胜平负":
            src_parts = []
            for src_name, src_p, src_n, src_w in r.get("source_weights", []):
                src_parts.append(f"{src_name}:w={src_w:.2f},n={src_n},p={src_p:.1%}")
            if src_parts:
                print(f"      来源: {' | '.join(src_parts)}")
        if bet_name == "胜平负" and r.get("cold_percentile", 0) >= 0.85 and r.get("hit_rate", 0) <= 0.20:
            print(f"      提醒: 冷门指数较高({r['cold_index']:.1f}, 分位={r.get('cold_percentile',0):.0%})，存在爆冷风险/机会，建议小仓位。")
    # 各层级对比（针对推荐结果）
    best_rk = results[0]["result_key"] if results else None
    if best_rk and bet_name == "胜平负":
        result_map = None
        for bt, info in BET_TYPES.items():
            if info["name"] == bet_name:
                result_map = info.get("result_map") or {}
                break
        desc = result_map.get(best_rk, best_rk) if result_map else best_rk
        print(f"\n  各层级对比: {best_rk}({desc})")
        print(f"  {'层级':<20} {'命中率':>8} {'平均赔率':>8} {'EV':>8} {'样本':>8} {'置信度':<10}")
        print("  " + "-" * 60)
        for level in LEVELS:
            data = all_levels.get(level, [])
            for d in data:
                if d["result_key"] == best_rk:
                    ev_str = f"+{d['expected_value']:.3f}" if d["expected_value"] >= 0 else f"{d['expected_value']:.3f}"
                    print(f"  {level_names.get(level, level):<20} {d['hit_rate']:>7.1%} "
                          f"{d['avg_odds']:>8.2f} {ev_str:>8} {d['sample_count']:>8} {get_confidence(d['sample_count']):<10}")
    # 结论
    best = results[0] if results else None
    if best:
        print(f"  ★ 命中率优先推荐: {best['result_key']}({best['description']}) "
              f"命中率={best['hit_rate']:.1%} 赔率={best['current_odds']:.2f} EV={best['ev']:+.3f} {best['confidence']}")
        if len(results) >= 2:
            conservative = results[1]
            print(f"  ☆ 保守备选: {conservative['result_key']}({conservative['description']}) "
                  f"命中率={conservative['hit_rate']:.1%} 赔率={conservative['current_odds']:.2f} EV={conservative['ev']:+.3f} {conservative['confidence']}")
        if best.get("cold_percentile", 0) >= 0.85 and best.get("hit_rate", 0) <= 0.20:
            print(f"  ⚠ 冷门提醒: 当前主推项冷门分位较高({best.get('cold_percentile',0):.0%})，建议控制仓位。")

def print_odds_bucket_recommendation(bet_name, ob_results):
    """打印赔率分桶独立推荐（多桶加权平均）"""
    if not ob_results:
        return
    # 低命中率过滤：命中率<15%不展示
    ob_results = [r for r in ob_results if r.get("hit_rate", 0) >= 0.15]
    if not ob_results:
        return
    print(f"\n  【{bet_name} · 赔率分桶推荐（多桶加权平均）】")
    print(f"  {'#':>2}  {'结果':<10} {'描述':<10} {'赔率':>6} {'命中率':>8} {'主桶':>8} {'样本':>6} {'EV':>8} {'桶数':>6}")
    print("  " + "-" * 68)
    max_rows = 5 if bet_name == "胜平负" else 3
    display_ob = ob_results[:max_rows]
    for i, r in enumerate(display_ob, 1):
        ev_str = f"+{r['expected_value']:.3f}" if r["expected_value"] >= 0 else f"{r['expected_value']:.3f}"
        print(f"  {i:>2}  {r['result_key']:<10} {r['description']:<10} {r['current_odds']:>6.2f} "
              f"{r['hit_rate']:>7.1%} {r.get('odds_bucket','-'):>8} {r['sample_count']:>6} {ev_str:>8} {r.get('bucket_source',''):>8}")
    best = ob_results[0]
    print(f"  ★ 多桶推荐: {best['result_key']}({best['description']}) "
          f"命中率={best['hit_rate']:.1%} 赔率={best['current_odds']:.2f} "
          f"主桶={best['odds_bucket']} 总样本={best['sample_count']} {best.get('bucket_source','')}")

def fetch_today_matches(target_date=None):
    """
    从澳客网抓取指定日期的竞彩数据（含赔率）。
    target_date: datetime 对象，默认今天。
    返回: match_list（与 JSON 文件格式一致）
    """
    if target_date is None:
        target_date = datetime.now()
    date_str = target_date.strftime("%Y-%m-%d")
    month_str = target_date.strftime("%Y-%m")
    day_str = target_date.strftime("%d")

    url = (
        f"https://www.okooo.com/jingcai/kaijiang/"
        f"?LotteryType=SportteryWDL&StartDate={month_str}-{day_str}&EndDate={month_str}-{day_str}"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.okooo.com/",
    }

    try:
        import requests
        from bs4 import BeautifulSoup

        resp = requests.get(url, headers=headers, timeout=15,
                             proxies={"http": None, "https": None})
        resp.encoding = "gb2312"
    except ImportError:
        print("错误: 需要 requests 和 beautifulsoup4 库")
        print("请运行: pip3 install requests beautifulsoup4")
        return None
    except Exception as e:
        print(f"错误: 抓取澳客网数据失败 ({e})")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.find_all("tr", class_="trClass")

    match_list = []
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 18:
            continue

        home_link = cols[3].find("a")
        match_id = home_link["href"].split("/")[3] if home_link else None

        match_dict = {
            "num": cols[0].text.strip(),
            "match_time": f"{date_str} {cols[2].text.strip()}",
            "matchId": match_id,
            "league": cols[1].text.strip(),
            "home": cols[3].text.strip(),
            "away": cols[4].text.strip(),
            "result": {
                "half": cols[5].text.strip(),
                "full": cols[6].text.strip(),
                "win_draw_lose_result": cols[7].text.strip(),
                "win_draw_lose_odds": cols[8].text.strip(),
                "handicap": cols[9].text.strip(),
                "handicap_result": cols[10].text.strip(),
                "handicap_odds": cols[11].text.strip(),
                "score_result": cols[12].text.strip(),
                "score_odds": cols[13].text.strip(),
                "goal_result": cols[14].text.strip(),
                "goal_odds": cols[15].text.strip(),
                "half_full_result": cols[16].text.strip(),
                "half_full_odds": cols[17].text.strip(),
            }
        }
        match_list.append(match_dict)

    return match_list


def json_to_match_data(match_list):
    """
    将抓取的原始 JSON match_list 转换为 recommend.py 的 match_data 格式。
    只保留有赔率的玩法（过滤已完赛或未开售的场次）。
    """
    matches = []
    for m in match_list:
        result = m.get("result", {})
        league = m.get("league", "")
        home = m.get("home", "")
        away = m.get("away", "")
        handicap = result.get("handicap", "")

        bet_odds = {}

        # 胜平负赔率
        wdl_odds_raw = result.get("win_draw_lose_odds", "")
        wdl_rk = result.get("win_draw_lose_result", "")
        if wdl_odds_raw and wdl_odds_raw not in ("-", ""):
            v = safe_float(wdl_odds_raw)
            if v and v > 0:
                bet_odds.setdefault("wdl", {})
                bet_odds["wdl"][wdl_rk] = v

        # 让球胜平负赔率
        hc_odds_raw = result.get("handicap_odds", "")
        hc_rk = result.get("handicap_result", "")
        if hc_odds_raw and hc_odds_raw not in ("-", ""):
            v = safe_float(hc_odds_raw)
            if v and v > 0:
                bet_odds.setdefault("handicap", {})
                bet_odds["handicap"][hc_rk] = v

        # 至少有一个玩法的赔率才纳入推荐
        if bet_odds:
            matches.append({
                "league": league,
                "home": home,
                "away": away,
                "handicap": handicap,
                "bet_odds": bet_odds,
            })

    return matches


def parse_txt_file(filepath):
    """
    解析txt文件，返回多场比赛数据列表
    
    格式示例：
    [match]
    league=英超
    home=曼城
    away=伯恩利
    handicap=-2
    handicap_3=1.85
    handicap_1=3.50
    handicap_0=4.20
    """
    matches = []
    current = None
    current_date_full = None   # e.g. 2026-05-27
    current_weekday = None     # e.g. 周三
    pending_num = None         # e.g. 007

    def normalize_num(s):
        d = "".join(ch for ch in s if ch.isdigit())
        if not d:
            return None
        return d.zfill(3)
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # 注释里可能含场次标签
            if line.startswith("#"):
                content = line.lstrip("#").strip()
                # 支持: #2026-05-27 周三 或 # 2026-05-27 周三
                m_date = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(周[一二三四五六日天])$", content)
                if m_date:
                    current_date_full = m_date.group(1)
                    current_weekday = m_date.group(2)
                    continue
                # 支持: # 007 或 #007
                m_num = re.match(r"^(\d{1,3})$", content)
                if m_num:
                    pending_num = normalize_num(m_num.group(1))
                continue
            if not line:
                continue
            if line == "[match]":
                if current:
                    matches.append(current)
                current = {"bet_odds": {}}
                if current_date_full:
                    current["match_date_full"] = current_date_full
                if current_weekday:
                    current["match_weekday"] = current_weekday
                if pending_num:
                    current["match_num"] = pending_num
                if current_weekday and pending_num:
                    current["match_label"] = f"{current_weekday}{pending_num}"
                elif pending_num:
                    current["match_label"] = pending_num
                pending_num = None
                continue
            if current is None:
                continue
            # 解析 key=value
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                # 基本信息字段
                if key in ("league", "home", "away", "handicap"):
                    current[key] = value
                else:
                    # 赔率字段: 格式为 prefix_result=odds
                    # 如 handicap_3=1.85, half_full_3-3=1.80, score_1:0=6.50
                    for bt, info in BET_TYPES.items():
                        prefix = info["prefix"]
                        if key.startswith(prefix + "_"):
                            result_key = key[len(prefix) + 1:]
                            odds = safe_float(value)
                            if odds and odds > 0:
                                if bt not in current["bet_odds"]:
                                    current["bet_odds"][bt] = {}
                                current["bet_odds"][bt][result_key] = odds
                            break
    if current:
        matches.append(current)
    return matches
def check_wdl_handicap_consistency(results_dict, handicap_line):
    """检查 WDL vs Handicap 推荐一致性

    核心原则：WDL 和 Handicap 指向同一方向（主队或客队）才合理。
    当 WDL 明确指向某方，但 Handicap 指向反方向时，说明数据矛盾。

    矛盾判断逻辑：
      - WDL=3(主胜) & Handicap=0(输盘=客队赢盘)：
        → 浅让球(>=0)下是矛盾的；深让球(-2,-3)下可能合理(主队小胜但未穿盘)
      - WDL=0(客胜) & Handicap=3(赢盘=主队赢盘)：
        → 浅让球(<=0)下是矛盾的；深受让(+2,+3)下可能合理

    返回： (adjusted_handicap_results, adjustment_desc, is_inconsistent)
    - adjusted_handicap_results: 调整后的 Handicap results 列表（排序保留）
    - adjustment_desc: 调整描述说明
    - is_inconsistent: 是否存在矛盾
    """
    wdl_results = results_dict.get("wdl")
    hc_results = results_dict.get("handicap")
    if not wdl_results or not hc_results:
        return None, "", False

    # 提取 WDL 的各方命中率
    wdl_hit = {}
    for r in wdl_results:
        wdl_hit[r["result_key"]] = r["hit_rate"]

    hc_hit = {}
    for r in hc_results:
        hc_hit[r["result_key"]] = r["hit_rate"]

    wdl_home = wdl_hit.get("3", 0)   # 主胜
    wdl_away = wdl_hit.get("0", 0)   # 客胜
    wdl_draw = wdl_hit.get("1", 0)   # 平局
    hc_home = hc_hit.get("3", 0)     # 赢盘（主队方向）
    hc_away = hc_hit.get("0", 0)     # 输盘（客队方向）

    try:
        hc_line_val = float(handicap_line) if handicap_line else 0
    except (ValueError, TypeError):
        hc_line_val = 0

    # === 判断矛盾 ===
    # 场景 A: WDL 主胜 > 40%，Handicap 客队赢盘 > 主队赢盘
    side_contradiction = False
    if wdl_home > 0.35 and hc_away > hc_home:
        # 主队胜率明显，但 Handicap 倾向客队
        # 深让球下合理（如-2下主队2-0赢但不穿盘），浅让球不合理
        if hc_line_val >= -1:
            side_contradiction = True
    elif wdl_away > 0.35 and hc_home > hc_away:
        # 客队胜率明显，但 Handicap 倾向主队
        if hc_line_val <= 0:
            side_contradiction = True

    if not side_contradiction:
        return None, "", False

    # === 计算统一的"主场方向"概率 ===
    # WDL 侧概率：normalize 主/客
    wdl_total = wdl_home + wdl_draw + wdl_away
    if wdl_total <= 0:
        return None, "", False

    wdl_side_home = wdl_home / wdl_total  # WDL 视角：主胜的概率
    hc_total = hc_home + hc_away + hc_hit.get("1", 0)
    if hc_total <= 0:
        return None, "", False
    hc_side_home = hc_home / hc_total   # Handicap 视角：主队赢盘的概率

    # 矛盾严重程度
    conflict_strength = abs(wdl_side_home - hc_side_home)
    if conflict_strength < 0.15:
        return None, "", False  # 轻微矛盾，忽略

    # 保存调整前的值用于显示
    old_hc_home = hc_home
    old_hc_away = hc_away

    blend_factor = min(0.4, conflict_strength * 0.5)  # 最多拉40%

    # 统一主场概率 = WDL 侧概率 + 基础偏移
    unified_home_prob = 0.55 * wdl_side_home + 0.05
    unified_away_prob = max(0.05, 1.0 - unified_home_prob)

    # 调整 Handicap 命中率
    adj_hc_home = hc_home * (1.0 - blend_factor) + unified_home_prob * blend_factor
    adj_hc_away = hc_away * (1.0 - blend_factor) + unified_away_prob * blend_factor

    # 保持走盘率不变，重新归一化
    hc_push = hc_hit.get("1", 0)
    adj_total = adj_hc_home + adj_hc_away + hc_push
    if adj_total > 0:
        adj_hc_home /= adj_total
        adj_hc_away /= adj_total

    # ===== 调整结果列表 =====
    adjusted = []
    for r in hc_results:
        nr = dict(r)
        rk = r["result_key"]
        if rk == "3":
            nr["hit_rate"] = adj_hc_home
            nr["adjusted"] = True
        elif rk == "0":
            nr["hit_rate"] = adj_hc_away
            nr["adjusted"] = True
        else:
            nr["adjusted"] = False
        adjusted.append(nr)

    # 重新排序
    adjusted.sort(key=lambda x: x["hit_rate"] * x["current_odds"], reverse=True)

    if wdl_home > wdl_away:
        desc = (f"【一致性修正】WDL主胜强→Handicap倾向客队，"
                f"调整后主赢盘{old_hc_home:.0%}→{adj_hc_home:.0%} "
                f"(融合系数{blend_factor:.0%})")
    else:
        desc = (f"【一致性修正】WDL客胜强→Handicap倾向主队，"
                f"调整后客赢盘{old_hc_away:.0%}→{adj_hc_away:.0%} "
                f"(融合系数{blend_factor:.0%})")

    return adjusted, desc, True


def compute_same_match_parlay(wdl_results, hc_results, match_info,
                               wdl_level="global", hc_level="global"):
    """从同一场比赛的 WDL + Handicap 推荐中，找出最佳 2串1 组合

    核心逻辑：
    - 枚举 WDL 和 Handicap 的所有排列组合
    - 排除方向矛盾的组合（WDL主胜+Handicap客队赢盘）
    - 按 组合命中率 × 组合赔率 × 置信度 排序
    - 返回 Top 3 组合

    返回: [(combo_odds, combo_hit, ev, pick1, pick2, reason), ...]
    """
    if not wdl_results or not hc_results:
        return []

    valid_wdl = [r for r in wdl_results if r.get("hit_rate", 0) >= 0.15 and r.get("confidence_score", 0) >= 1]
    valid_hc = [r for r in hc_results if r.get("hit_rate", 0) >= 0.15 and r.get("confidence_score", 0) >= 1]
    if not valid_wdl or not valid_hc:
        return []

    combos = []
    for wr in valid_wdl:
        w_rk = wr["result_key"]
        w_hit = wr["hit_rate"]
        w_odds = wr["current_odds"]
        w_conf = wr.get("confidence_score", 1)
        w_desc = wr.get("description", w_rk)
        w_ev = wr.get("ev", None)
        w_sample = wr.get("sample", 0)
        w_confidence = wr.get("confidence", "低")

        for hr in valid_hc:
            h_rk = hr["result_key"]
            h_hit = hr["hit_rate"]
            h_odds = hr["current_odds"]
            h_conf = hr.get("confidence_score", 1)
            h_desc = hr.get("description", h_rk)
            h_ev = hr.get("ev", None)
            h_sample = hr.get("sample", 0)
            h_confidence = hr.get("confidence", "低")

            # === 排除方向矛盾的组合 ===
            # WDL 3=主胜, Handicap 3=赢盘(主队方向)
            # WDL 0=客胜, Handicap 0=输盘(客队方向)
            if (w_rk == "3" and h_rk == "0") or (w_rk == "0" and h_rk == "3"):
                continue

            # === 计算组合赔率 ===
            combo_odds = w_odds * h_odds
            if combo_odds < 2.0:
                continue  # 组合赔率太低没意义

            # === 联合概率（考虑相关性） ===
            # WDL 和 Handicap 正相关：P(A∩B) ≈ min(P(A), P(B)) × (1 - 0.15) 保守估计
            # 方向相同（如WDL3+HC3）：高度相关，联合概率≈较高者的90%
            if w_rk == h_rk:
                # 完全同方向（如WDL3+Handicap3）
                joint_hit = max(w_hit, h_hit) * 0.90
            elif w_rk == "1":
                # WDL平局 + Handicap任一方向 → 弱相关
                joint_hit = max(w_hit, h_hit) * 0.60
            elif h_rk == "1":
                # WDL任一方向 + Handicap走盘 → 弱相关
                joint_hit = max(w_hit, h_hit) * 0.60
            else:
                # 不同方向但不相悖（如WDL3+HC1, WDL1+HC3等）
                joint_hit = w_hit * h_hit * 1.2  # 弱相关，稍高于独立
                joint_hit = min(joint_hit, max(w_hit, h_hit) * 0.85)

            # === 综合评分 ===
            # 高赔组合偏好：hit^2 × odds × conf（平方突出命中率，避免纯博冷）
            hit_weighted = joint_hit * joint_hit  # 平方压制低命中
            score = hit_weighted * combo_odds * (w_conf + h_conf) / 2.0

            # 预期价值
            ev = joint_hit * combo_odds - 1.0

            # 推荐理由
            reason = (f"WDL {w_rk}({w_desc}) 命中率{w_hit:.0%}, "
                      f"Handicap {h_rk}({h_desc}) 命中率{h_hit:.0%}")

            combos.append({
                "combo_odds": combo_odds,
                "joint_hit": joint_hit,
                "ev": ev,
                "score": score,
                "wdl_pick": {"rk": w_rk, "desc": w_desc, "odds": w_odds,
                             "hit": w_hit, "conf": w_confidence,
                             "conf_score": w_conf, "sample": w_sample,
                             "ev": w_ev},
                "hc_pick": {"rk": h_rk, "desc": h_desc, "odds": h_odds,
                            "hit": h_hit, "conf": h_confidence,
                            "conf_score": h_conf, "sample": h_sample,
                            "ev": h_ev},
                "reason": reason,
                "level_wdl": wdl_level,
                "level_hc": hc_level,
            })

    combos.sort(key=lambda x: x["score"], reverse=True)
    return combos[:5]


def build_reason_text(pick_info, level, bet_type_name):
    """生成单场推荐的理由说明"""
    lines = []
    rk = pick_info["rk"]
    desc = pick_info["desc"]
    hit = pick_info["hit"]
    odds = pick_info["odds"]
    conf = pick_info["conf"]
    sample = pick_info["sample"]
    ev = pick_info["ev"]

    lines.append(f"{bet_type_name} {rk}({desc})")
    lines.append(f"  数据源: {level}")
    if sample > 0:
        lines.append(f"  样本量: {sample}场 {conf}")
    lines.append(f"  命中率: {hit:.1%}  赔率: {odds:.2f}")
    if ev is not None:
        lines.append(f"  期望值(EV): {ev:+.4f}")
    return "\n".join(lines)


def recommend_conditional_derived(conn, league, handicap, wdl_pick_key, hc_pick_key, min_sample=20):
    """
    基于预测的胜平负+让球结果，分析历史条件分布，推导比分/进球数/半全场推荐。

    查询逻辑：
    - 先查 league_handicap 维度（最精准），样本不足时自动降级
    - 返回各玩法 Top 5 结果（含命中率、平均赔率）
    """
    bt_configs = [
        ("score",   "比分",     "score_result", "score_odds"),
        ("goal",    "进球数",   "goal_result",  "goal_odds"),
        ("half_full", "半全场", "half_full_result", "half_full_odds"),
    ]

    result_maps = {
        "score": None,
        "goal": GOAL_MAP,
        "half_full": HALF_FULL_MAP,
    }

    derived = {}

    for bt_key, bt_name, result_col, odds_col in bt_configs:
        rmap = result_maps.get(bt_key)

        # === 尝试 league+handicap 维度 ===
        rows = None
        level_name = None
        if league and handicap:
            rows = _query_conditional(conn, result_col, odds_col,
                                       league=league, handicap=handicap,
                                       wdl_rk=wdl_pick_key, hc_rk=hc_pick_key)
            if rows and sum(r["cnt"] for r in rows) >= min_sample:
                level_name = f"{league}|handicap={handicap}"

        # === 降级到 league 维度 ===
        if not level_name and league:
            rows = _query_conditional(conn, result_col, odds_col,
                                       league=league, handicap=None,
                                       wdl_rk=wdl_pick_key, hc_rk=hc_pick_key)
            if rows and sum(r["cnt"] for r in rows) >= min_sample:
                level_name = f"{league}（不限让球）"

        # === 降级到 handicap 维度 ===
        if not level_name and handicap:
            rows = _query_conditional(conn, result_col, odds_col,
                                       league=None, handicap=handicap,
                                       wdl_rk=wdl_pick_key, hc_rk=hc_pick_key)
            if rows and sum(r["cnt"] for r in rows) >= min_sample:
                level_name = f"handicap={handicap}（不限联赛）"

        # === 最后降级到全局 ===
        if not level_name:
            rows = _query_conditional(conn, result_col, odds_col,
                                       league=None, handicap=None,
                                       wdl_rk=wdl_pick_key, hc_rk=hc_pick_key)
            if rows and sum(r["cnt"] for r in rows) >= min_sample:
                level_name = "全局"

        if not rows:
            derived[bt_key] = {"name": bt_name, "level": None, "items": []}
            continue

        total = sum(r["cnt"] for r in rows)
        items = []
        for r in rows:
            desc = (rmap or {}).get(r["rk"], r["rk"])
            items.append({
                "result_key": r["rk"],
                "description": desc,
                "hit_rate": r["cnt"] / total if total > 0 else 0,
                "sample": r["cnt"],
                "avg_odds": r["avg_odds"] or 0,
            })

        derived[bt_key] = {"name": bt_name, "level": level_name, "items": items, "total": total}

    return derived


def _query_conditional(conn, result_col, odds_col, league, handicap, wdl_rk, hc_rk):
    """查询给定条件下的结果分布"""
    where = ["is_valid = 1", "wdl_result = ?", "handicap_result = ?",
             f"{result_col} != ''"]
    params = [wdl_rk, hc_rk]

    if league:
        where.append("league = ?")
        params.append(league)
    if handicap:
        where.append("handicap = ?")
        params.append(handicap)

    sql = f"""
        SELECT {result_col} AS rk, COUNT(*) AS cnt, AVG({odds_col}) AS avg_odds
        FROM matches
        WHERE {' AND '.join(where)}
        GROUP BY rk
        ORDER BY cnt DESC
        LIMIT 6
    """
    try:
        cursor = conn.execute(sql, params)
        return [{"rk": row[0], "cnt": row[1], "avg_odds": row[2]} for row in cursor]
    except Exception:
        return None


def pick_coherent_pair(wdl_results, hc_results, handicap_line):
    """
    从 WDL 和 Handicap 推荐结果中选择最一致的(pair, pair)。

    一致性规则：
    - WDL 方向必须与 Handicap 方向一致（主队方向或客队方向）
    - 平局/走盘视为中性，可与任何方向搭配

    返回: {
        "wdl_pick": 最佳 WDL 结果,
        "hc_pick":  与之最一致的 Handicap 结果,
        "conflict_desc": 冲突说明（无冲突为空字符串）,
        "cold_desc": 冷门说明（无冷门为空字符串）,
        "coherent": 是否一致,
        "_swapped": 是否因方向不一致调整了推荐,
    }
    """
    # 方向标签
    def direction(rk):
        if rk == "3": return "home"
        if rk == "0": return "away"
        return "neutral"

    def is_coherent(w_rk, h_rk):
        """检查 WDL 和 Handicap 是否方向一致"""
        wd = direction(w_rk)
        hd = direction(h_rk)
        if wd == "neutral" or hd == "neutral":
            return True  # 平局/走盘是中性
        return wd == hd  # 方向必须相同

    # 记录原始 Top 结果（仅用于生成说明）
    raw_wdl_top = wdl_results[0]["result_key"] if wdl_results else None
    raw_hc_top = hc_results[0]["result_key"] if hc_results else None

    # 按 confidence_score 排序找最佳一致组合
    best = {"wdl_pick": None, "hc_pick": None, "conflict_desc": "", "cold_desc": "", "coherent": True, "_swapped": False}

    for wr in wdl_results:
        for hr in hc_results:
            if not is_coherent(wr["result_key"], hr["result_key"]):
                continue
            score = (wr.get("confidence_score", 0) + hr.get("confidence_score", 0)) / 2.0
            if best["wdl_pick"] is None or score > best.get("_score", 0):
                best["wdl_pick"] = wr
                best["hc_pick"] = hr
                best["_score"] = score

    if not best["wdl_pick"] or not best["hc_pick"]:
        # 实在找不到一致组合，用各自第一
        best["wdl_pick"] = wdl_results[0] if wdl_results else None
        best["hc_pick"] = hc_results[0] if hc_results else None
        best["coherent"] = False
        best["conflict_desc"] = "WDL与让球方向不一致，建议分开判断或避开这场比赛"

    # === 检查是否替换了原始 Top 推荐，生成说明 ===
    if best["wdl_pick"] and best["hc_pick"]:
        sel_w = best["wdl_pick"]["result_key"]
        sel_h = best["hc_pick"]["result_key"]
        # 如果某一边从原始 Top 换成了其他结果，说明被方向一致性约束了
        swapped_side = None
        if sel_w != raw_wdl_top:
            swapped_side = "WDL"
        elif sel_h != raw_hc_top:
            swapped_side = "Handicap"
        if swapped_side:
            # 获取被换掉的那边的详细数据
            raw_hc_info = None
            raw_wdl_info = None
            for r in hc_results:
                if r["result_key"] == raw_hc_top:
                    raw_hc_info = r
                    break
            for r in wdl_results:
                if r["result_key"] == raw_wdl_top:
                    raw_wdl_info = r
                    break
            hc_detail = ""
            if raw_hc_info:
                hc_detail = (f"（Handicap {raw_hc_top} {HANDICAP_RESULT_MAP.get(raw_hc_top,raw_hc_top)}"
                             f" 命中率={raw_hc_info['hit_rate']:.1%} 赔率={raw_hc_info['current_odds']:.2f}）")
            orig_desc = (f"WDL {raw_wdl_top}({WDL_MAP.get(raw_wdl_top,raw_wdl_top)})"
                         f" + Handicap {raw_hc_top}({HANDICAP_RESULT_MAP.get(raw_hc_top,raw_hc_top)})"
                         f" 方向相悖，调整为当前方向一致的组合{hc_detail}")
            best["conflict_desc"] = orig_desc
            best["_swapped"] = True

    # === 检查 WDL 冷门 ===
    w_rk = best["wdl_pick"]["result_key"]
    w_cold = best["wdl_pick"].get("cold_percentile", 0)
    w_hit = best["wdl_pick"].get("hit_rate", 0)
    if w_cold >= 0.85 and w_hit <= 0.20:
        best["cold_desc"] = (f"WDL主推项({WDL_MAP.get(w_rk,w_rk)})冷门指数较高"
                             f"({best['wdl_pick'].get('cold_index',0):.1f}, 分位{w_cold:.0%})，存在爆冷风险")

    # === 检查 WDL 第二推荐是否与 Handicap 更一致 ===
    if not best["conflict_desc"] and len(wdl_results) >= 2 and len(hc_results) >= 2:
        top1_w = wdl_results[0]
        top2_w = wdl_results[1]
        if not is_coherent(top1_w["result_key"], best["hc_pick"]["result_key"]) and \
           is_coherent(top2_w["result_key"], best["hc_pick"]["result_key"]):
            gap = top1_w["hit_rate"] - top2_w["hit_rate"]
            if gap < 0.10:  # 差距不大
                best["conflict_desc"] = (f"WDL首选{top1_w['result_key']}({top1_w['description']})与让球方向有偏差，"
                                         f"备选{top2_w['result_key']}({top2_w['description']})方向更一致，可参考")

    return best


def format_unified_recommendation(best, derived):
    """打印统一推荐结果"""
    wp = best["wdl_pick"]
    hp = best["hc_pick"]
    w_name = WDL_MAP.get(wp["result_key"], wp["result_key"])
    h_name = HANDICAP_RESULT_MAP.get(hp["result_key"], hp["result_key"])

    # 方向标记
    def direction_tag(rk, bet_type):
        """给结果加上方向标记"""
        if rk == "3":
            return "🔵 主队方向" if bet_type == "wdl" else "🔵 主队赢盘"
        if rk == "0":
            return "🔴 客队方向" if bet_type == "wdl" else "🔴 客队赢盘"
        return "⚪ 中性"

    w_tag = direction_tag(wp["result_key"], "wdl")
    h_tag = direction_tag(hp["result_key"], "handicap")

    lines = []
    lines.append(f"\n  ★ 统一推荐")
    lines.append(f"  ┌────────────────────────────────────────────────────────────────")
    lines.append(f"  │ 胜平负 → {wp['result_key']}({w_name}) {w_tag}  "
                 f"命中率={wp['hit_rate']:.1%} 赔率={wp['current_odds']:.2f}")
    lines.append(f"  │ 让球   → {hp['result_key']}({h_name}) {h_tag}  "
                 f"命中率={hp['hit_rate']:.1%} 赔率={hp['current_odds']:.2f}")

    # 条件推导
    for bt_key in ["score", "goal", "half_full"]:
        dd = derived.get(bt_key)
        if dd and dd.get("items"):
            top = dd["items"][0]
            lines.append(f"  │ {dd['name']} → {top['result_key']}({top['description']})  "
                         f"命中率={top['hit_rate']:.1%}")

    lines.append(f"  └────────────────────────────────────────────────────────────────")

    # 冲突/冷门提示
    warnings = []
    if best.get("conflict_desc"):
        warnings.append(f"  ⚠ 注意: {best['conflict_desc']}")
    if best.get("cold_desc"):
        warnings.append(f"  ⚠ 冷门: {best['cold_desc']}")

    # 检查推导与推荐方向是否矛盾
    if derived and not best.get("conflict_desc"):
        conflict_detail = _check_derived_conflict(best, derived)
        if conflict_detail:
            warnings.append(f"  ⚠ 冷门: {conflict_detail}")

    lines.append("")
    for w in warnings:
        lines.append(w)

    return "\n".join(lines)


def _check_derived_conflict(best, derived):
    """
    检查条件推导的结果是否与推荐方向有潜在矛盾。
    例如：推荐主胜，但 derived 中半全场 0-3(客胜-客胜) 排名很高 → 说明样本中有异常
    """
    wp = best["wdl_pick"]
    hp = best["hc_pick"]
    w_rk = wp["result_key"]
    h_rk = hp["result_key"]

    # 只看半全场：半全场的最终结果应与 WDL 方向一致
    hf = derived.get("half_full")
    if not hf or not hf.get("items") or len(hf["items"]) < 2:
        return None

    # 取 Top3 半全场，检查是否有非最终结果指向
    top_hf = hf["items"][:3]
    direction_mismatch = []
    for item in top_hf:
        hf_result = item["result_key"]  # e.g. "3-3", "1-3", "0-3"
        full_part = hf_result.split("-")[1]  # full-time result: 3, 1, 0
        if w_rk == "3" and full_part == "0":
            direction_mismatch.append(item)
        elif w_rk == "0" and full_part == "3":
            direction_mismatch.append(item)

    if direction_mismatch:
        items_str = ", ".join(
            f"{m['result_key']}({HALF_FULL_MAP.get(m['result_key'], m['result_key'])} "
            f"命中率={m['hit_rate']:.1%})"
            for m in direction_mismatch
        )
        return (f"半全场出现与方向不一致的高频结果: {items_str}，"
                f"建议留意对手反击得分能力")

    return None
    """查询给定条件下的结果分布"""
    where = ["is_valid = 1", "wdl_result = ?", "handicap_result = ?",
             f"{result_col} != ''"]
    params = [wdl_rk, hc_rk]

    if league:
        where.append("league = ?")
        params.append(league)
    if handicap:
        where.append("handicap = ?")
        params.append(handicap)

    sql = f"""
        SELECT {result_col} AS rk, COUNT(*) AS cnt, AVG({odds_col}) AS avg_odds
        FROM matches
        WHERE {' AND '.join(where)}
        GROUP BY rk
        ORDER BY cnt DESC
        LIMIT 6
    """
    try:
        cursor = conn.execute(sql, params)
        return [{"rk": row[0], "cnt": row[1], "avg_odds": row[2]} for row in cursor]
    except Exception:
        return None



def process_match(conn, match_data):
    """处理单场比赛，生成推荐，并返回本场最稳候选用于串关"""
    league = match_data.get("league", "")
    home = match_data.get("home", "")
    away = match_data.get("away", "")
    handicap = match_data.get("handicap", "")
    bet_odds = match_data.get("bet_odds", {})
    if not league:
        print("  ⚠ 缺少联赛信息，跳过")
        return None
    if match_data.get("match_label"):
        print(f"  编号: {match_data.get('match_label')}")
    print(f"  联赛: {league}")
    print(f"  主队: {home}  vs  客队: {away}")
    if handicap:
        hc_desc = "主场让球" if handicap.startswith("-") else "主场受让" if handicap.startswith("+") else "平手"
        print(f"  让球: {handicap} ({hc_desc})")
    else:
        print("  让球: 缺失（已自动降级为联赛/全局推荐）")
    for bt, odds in bet_odds.items():
        name = BET_TYPES[bt]["name"]
        parts = [f"{rk}={odds:.2f}" for rk, odds in odds.items()]
        print(f"  {name}赔率: {', '.join(parts)}")
    all_best = []
    handicap_best = None
    all_results = {}  # bt -> (results, ob_results, best_level, all_levels)
    for bt, input_odds in bet_odds.items():
        bet_info = BET_TYPES[bt]
        best_level, results, all_levels, ob_results = recommend_bet_type(
            conn, league, handicap, home, away, bt, input_odds, bet_info
        )
        all_results[bt] = {
            "results": results,
            "ob_results": ob_results,
            "best_level": best_level,
            "all_levels": all_levels,
        }
        if results:
            all_best.append((bt, bet_info["name"], results[0]))
            if bt == "handicap" and handicap_best is None:
                handicap_best = (bt, bet_info["name"], results[0])

    # === 跨玩法一致性检查：WDL vs Handicap ===
    adj_handicap, adj_desc, is_inconsistent = check_wdl_handicap_consistency(
        {bt: all_results[bt]["results"] for bt in all_results if bt in ("wdl", "handicap")},
        handicap
    )
    if is_inconsistent and adj_handicap:
        # 用调整后的 handicap results 替换
        all_results["handicap"]["results"] = adj_handicap
        # 更新 all_best 和 handicap_best
        for i, (bt, name, _) in enumerate(all_best):
            if bt == "handicap":
                all_best[i] = (bt, name, adj_handicap[0])
                handicap_best = (bt, name, adj_handicap[0])
                break

    # === 打印所有玩法（handicap 可能已被调整）===
    for bt, input_odds in bet_odds.items():
        bet_info = BET_TYPES[bt]
        data = all_results[bt]
        results = data["results"]
        ob_results = data["ob_results"]
        best_level = data["best_level"]
        all_levels = data["all_levels"]
        print_bet_recommendation(bet_info["name"], best_level, results, all_levels, league, handicap)
        print_odds_bucket_recommendation(bet_info["name"], ob_results)
        if not ob_results and input_odds:
            print(f"  【{bet_info['name']} · 赔率分桶推荐】无对应样本数据（同桶及相邻桶样本<10或无历史命中）。")
    if is_inconsistent and adj_desc:
        print(f"  {adj_desc}")
    if not all_best:
        return None

    # === 选取最一致组合 ===
    wdl_results = (all_results.get("wdl") or {}).get("results", [])
    hc_results = (all_results.get("handicap") or {}).get("results", [])
    coherent = pick_coherent_pair(wdl_results, hc_results, handicap)
    coherent_wdl = coherent["wdl_pick"]
    coherent_hc = coherent["hc_pick"]

    # === 基于一致组合推导比分/进球数/半全场 ===
    derived = {}
    if coherent_wdl and coherent_hc:
        derived = recommend_conditional_derived(
            conn, league, handicap,
            coherent_wdl["result_key"], coherent_hc["result_key"]
        )

    # === 统一推荐输出 ===
    print(format_unified_recommendation(coherent, derived))

    # === 单场 WDL+Handicap 2串1（仅当方向一致时推荐）===
    same_match_parlays = []
    if coherent["coherent"] and coherent_wdl and coherent_hc:
        wdl_level = all_results.get("wdl", {}).get("best_level", "global")
        hc_level = all_results.get("handicap", {}).get("best_level", "global")
        same_match_parlays = compute_same_match_parlay(
            [coherent_wdl], [coherent_hc], match_data,
            wdl_level=wdl_level, hc_level=hc_level,
        )

    if same_match_parlays:
        sp0 = same_match_parlays[0]
        print(f"\n  ★ 同场2串1（一致方向）: WDL {coherent_wdl['result_key']}({coherent_wdl['description']}) + "
              f"让球 {coherent_hc['result_key']}({coherent_hc['description']})")
        print(f"    组合赔率: {sp0['combo_odds']:.2f}  联合命中率: {sp0['joint_hit']:.1%}  EV: {sp0['ev']:+.4f}")

    # === 条件推导详情 ===
    if derived:
        for bt_key in ["score", "goal", "half_full"]:
            data = derived.get(bt_key)
            if not data or not data["level"] or not data["items"]:
                continue
            level_label = (f"条件: WDL={WDL_MAP.get(coherent_wdl['result_key'],coherent_wdl['result_key'])}"
                           f" + 让球={HANDICAP_RESULT_MAP.get(coherent_hc['result_key'],coherent_hc['result_key'])}"
                           f" ({data['level']})")
            print(f"\n  【{data['name']}详情】基于{level_label}")
            print(f"  {'#':>2}  {'结果':<12} {'命中率':>8} {'样本':>6} {'平均赔率':>8}")
            print("  " + "-" * 38)
            for i, item in enumerate(data["items"][:5], 1):
                odds_str = f"{item['avg_odds']:.2f}" if item['avg_odds'] > 0 else "—"
                print(f"  {i:>2}  {item['result_key']:<10} {item['description']:<2} "
                      f"{item['hit_rate']:>7.1%} {item['sample']:>6} {odds_str:>8}")
            print(f"  总样本: {data['total']}场")

    # === 设置返回值 ===
    top_bt = "wdl"
    if coherent_wdl:
        top_name = "胜平负"
        top_pick = coherent_wdl
    elif all_best:
        _, top_name, top_pick = all_best[0]
    else:
        top_name, top_pick = None, None

    ret = {
        "league": league,
        "home": home,
        "away": away,
        "bet_type": top_bt,
        "bet_name": top_name,
        "pick": top_pick,
        "match_label": match_data.get("match_label", ""),
        "match_date_full": match_data.get("match_date_full", ""),
        "match_num": match_data.get("match_num", ""),
        "same_match_parlays": same_match_parlays,
        "all_wdl_results": wdl_results[:3] if wdl_results else [],
        "all_hc_results": hc_results[:3] if hc_results else [],
        "all_bet_types": {bt: data.get("best_level", "") for bt, data in all_results.items()},
    }
    if handicap_best:
        ret["handicap_pick"] = {
            "bet_type": handicap_best[0],
            "bet_name": handicap_best[1],
            "pick": handicap_best[2],
        }
    return ret


def evaluate_hit(match_json_item, rec):
    """根据 JSON 实际赛果判断推荐是否命中。"""
    result = (match_json_item or {}).get("result", {})
    bt = rec.get("bet_type")
    rk = (rec.get("pick") or {}).get("result_key")
    if not bt or not rk:
        return None
    if bt == "wdl":
        actual = result.get("win_draw_lose_result", "")
    elif bt == "handicap":
        actual = result.get("handicap_result", "")
    else:
        return None
    return actual == rk if actual else None


def append_recommend_comments_to_json(parlay_candidates, parlay_summary=None):
    """将推荐及命中情况追加到对应日期 JSON（若文件存在）。"""
    if not parlay_candidates:
        return
    by_date = {}
    for rec in parlay_candidates:
        date_full = rec.get("match_date_full")
        if not date_full:
            continue
        by_date.setdefault(date_full, []).append(rec)

    for date_full, items in by_date.items():
        json_paths = []
        direct = os.path.join(BASE_DIR, f"{date_full}.json")
        if os.path.exists(direct):
            json_paths.append(direct)
        else:
            # 兼容按 年/月 子目录归档的场景
            candidates = glob.glob(os.path.join(BASE_DIR, "**", f"{date_full}.json"), recursive=True)
            json_paths.extend(candidates)
        if not json_paths:
            print(f"\n未找到对应日期JSON: {date_full}.json")
            continue
        # 优先使用路径最短（通常更靠近目标目录）的文件
        json_paths = sorted(set(json_paths), key=len)
        json_path = json_paths[0]
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                continue

            idx_by_num = {}
            for i, m in enumerate(data):
                num_raw = str(m.get("num", "")).strip()
                num_digits = "".join(ch for ch in num_raw if ch.isdigit())
                if num_digits:
                    idx_by_num[num_digits.zfill(3)] = i

            changed = False
            for rec in items:
                num_key = rec.get("match_num", "")
                i = idx_by_num.get(num_key)
                if i is None:
                    continue
                hit = evaluate_hit(data[i], rec)
                hit_text = "命中" if hit is True else ("未命中" if hit is False else "待开奖")
                pick = rec.get("pick") or {}
                text = (
                    f"推荐: {rec.get('bet_name','')} {pick.get('result_key','')}({pick.get('description','')}) "
                    f"赔率={pick.get('current_odds', 0):.2f} 命中率={pick.get('hit_rate', 0):.1%} 结果={hit_text}"
                )
                # 兼容旧字段 _comment_recommend，统一迁移为 comment_recommend
                comments = data[i].get("comment_recommend", data[i].get("_comment_recommend", []))
                if not isinstance(comments, list):
                    comments = [str(comments)]
                comments = list(dict.fromkeys(comments))
                if text not in comments:
                    comments.append(text)
                data[i]["comment_recommend"] = comments
                if "_comment_recommend" in data[i]:
                    del data[i]["_comment_recommend"]
                changed = True

            # 文件末尾附加 2串1 汇总注释（含命中信息）
            if parlay_summary:
                summary_items = []
                for item in parlay_summary:
                    if not isinstance(item, dict):
                        continue
                    t = item.get("type", "常规")
                    legs = item.get("legs", [])
                    if len(legs) != 2:
                        continue
                    def leg_hit_text(leg):
                        num = leg.get("match_num", "")
                        i2 = idx_by_num.get(num)
                        if i2 is None:
                            return "待开奖"
                        rec = {"bet_type": leg.get("bet_type"), "pick": {"result_key": leg.get("result_key")}}
                        h = evaluate_hit(data[i2], rec)
                        return "命中" if h is True else ("未命中" if h is False else "待开奖")

                    l1, l2 = legs[0], legs[1]
                    h1 = leg_hit_text(l1)
                    h2 = leg_hit_text(l2)
                    combo_hit = "命中" if (h1 == "命中" and h2 == "命中") else ("未命中" if "未命中" in (h1, h2) else "待开奖")
                    summary_items.append({
                        "type": f"{t}2串1",
                        "legs": [
                            {
                                "label": l1.get("label", ""),
                                "play": l1.get("play", ""),
                                "result_key": l1.get("result_key", ""),
                                "hit": h1,
                            },
                            {
                                "label": l2.get("label", ""),
                                "play": l2.get("play", ""),
                                "result_key": l2.get("result_key", ""),
                                "hit": h2,
                            },
                        ],
                        "combo_odds": round(float(item.get("combo_odds", 0) or 0), 2),
                        "combo_hit_rate": round(float(item.get("combo_hit_rate", 0) or 0), 6),
                        "combo_result": combo_hit,
                    })

                # 去重，保持稳定顺序
                dedup_items = []
                seen = set()
                for s in summary_items:
                    k = json.dumps(s, ensure_ascii=False, sort_keys=True)
                    if k in seen:
                        continue
                    seen.add(k)
                    dedup_items.append(s)
                summary_obj = {"comment_recommend_summary": dedup_items}
                replaced = False
                if isinstance(data, list) and data:
                    tail = data[-1]
                    if isinstance(tail, dict) and "comment_recommend_summary" in tail and "num" not in tail:
                        data[-1] = summary_obj
                        replaced = True
                if not replaced:
                    data.append(summary_obj)
                changed = True

            if changed:
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print(f"\n已写入推荐注释: {os.path.basename(json_path)}")
        except Exception as e:
            print(f"\n写入推荐注释失败: {os.path.basename(json_path)} ({e})")


def run_from_file(filepath):
    """从txt文件读取多场比赛数据，批量生成推荐"""
    conn = connect_db()
    if not conn:
        return

    matches = parse_txt_file(filepath)
    if not matches:
        print(f"错误: 文件中未找到任何比赛数据")
        print(f"请确保每场比赛以 [match] 开头")
        conn.close()
        return

    print("=" * 70)
    print(f"竞彩投注推荐（从文件读取: {os.path.basename(filepath)}）")
    print(f"共 {len(matches)} 场比赛")
    print("=" * 70)

    parlay_candidates = []
    for i, match_data in enumerate(matches, 1):
        print(f"\n{'━' * 70}")
        title = match_data.get("match_label") or f"第 {i} 场比赛"
        print(f"  {title}")
        print(f"{'━' * 70}")
        best = process_match(conn, match_data)
        if best:
            best["match_index"] = i
            parlay_candidates.append(best)

    parlay_summary = []

    if len(parlay_candidates) >= 2:
        parlay_candidates.sort(
            key=lambda x: (
                x["pick"].get("confidence_score", 0),
                x["pick"].get("hit_rate", 0),
                x["pick"].get("current_odds", 0),
            ),
            reverse=True,
        )

        leg1, leg2 = pick_best_pair(parlay_candidates, pick_field="pick", same_league_penalty=0.20)
        if not leg1 or not leg2:
            leg1, leg2 = parlay_candidates[0], parlay_candidates[1]

        handicap_candidates = [x for x in parlay_candidates if x.get("handicap_pick")]
        h1 = h2 = None
        if len(handicap_candidates) >= 2:
            handicap_candidates.sort(
                key=lambda x: (
                    x["handicap_pick"]["pick"].get("confidence_score", 0),
                    x["handicap_pick"]["pick"].get("hit_rate", 0),
                    x["handicap_pick"]["pick"].get("current_odds", 0),
                ),
                reverse=True,
            )
            h1, h2 = pick_best_pair(handicap_candidates, pick_field="handicap_pick", same_league_penalty=0.20)
            if not h1 or not h2:
                h1, h2 = handicap_candidates[0], handicap_candidates[1]

        # 一致性过滤：如果同场胜平负与让球方向相悖，替换稳定性更低的一组
        if h1 and h2:
            selected_ids = {leg1.get("match_index"), leg2.get("match_index"), h1.get("match_index"), h2.get("match_index")}
            conflicted = [x for x in [leg1, leg2, h1, h2] if is_cross_play_conflict(x)]
            if conflicted:
                for item in conflicted:
                    in_main = item in (leg1, leg2)
                    in_hc = item in (h1, h2)
                    if not (in_main or in_hc):
                        continue

                    main_pick = item.get("pick") if in_main else None
                    hc_pick = (item.get("handicap_pick") or {}).get("pick") if in_hc else None
                    main_score = stability_score(main_pick)
                    hc_score = stability_score(hc_pick)

                    replace_main = in_main and (not in_hc or main_score <= hc_score)
                    replace_hc = in_hc and (not in_main or hc_score < main_score)

                    if replace_main:
                        pool = [x for x in parlay_candidates if x.get("match_index") not in selected_ids and not is_cross_play_conflict(x)]
                        if pool:
                            pool.sort(key=lambda x: (
                                x["pick"].get("confidence_score", 0),
                                x["pick"].get("hit_rate", 0),
                                x["pick"].get("current_odds", 0),
                            ), reverse=True)
                            rep = pool[0]
                            if leg1 is item:
                                selected_ids.discard(leg1.get("match_index"))
                                leg1 = rep
                            else:
                                selected_ids.discard(leg2.get("match_index"))
                                leg2 = rep
                            selected_ids.add(rep.get("match_index"))

                    if replace_hc:
                        pool = [x for x in handicap_candidates if x.get("match_index") not in selected_ids and not is_cross_play_conflict(x)]
                        if pool:
                            pool.sort(key=lambda x: (
                                x["handicap_pick"]["pick"].get("confidence_score", 0),
                                x["handicap_pick"]["pick"].get("hit_rate", 0),
                                x["handicap_pick"]["pick"].get("current_odds", 0),
                            ), reverse=True)
                            rep = pool[0]
                            if h1 is item:
                                selected_ids.discard(h1.get("match_index"))
                                h1 = rep
                            else:
                                selected_ids.discard(h2.get("match_index"))
                                h2 = rep
                            selected_ids.add(rep.get("match_index"))

        o1 = leg1["pick"].get("current_odds", 1.0)
        o2 = leg2["pick"].get("current_odds", 1.0)
        combo_odds = o1 * o2
        hit1 = leg1["pick"].get("hit_rate", 0.0)
        hit2 = leg2["pick"].get("hit_rate", 0.0)
        combo_hit = hit1 * hit2

        # 获取每场的推荐数据源
        def get_parse_level(match_item):
            bt_key = match_item.get("bet_type", "")
            return match_item.get("all_bet_types", {}).get(bt_key, bt_key)

        l1_level = get_parse_level(leg1)
        l2_level = get_parse_level(leg2)

        print(f"\n{'━' * 70}")
        print("  【2串1 推荐（最可信两场）】")
        print(f"{'━' * 70}")
        print(
            f"  1) 第{leg1['match_index']}场 {leg1['league']} {leg1['home']} vs {leg1['away']} | "
            f"{leg1['bet_name']} -> {leg1['pick']['result_key']}({leg1['pick']['description']}) "
            f"命中率={hit1:.1%} 赔率={o1:.2f} 置信度={leg1['pick']['confidence']}"
        )
        print(f"     └ 理由: {leg1['match_label'] or leg1['bet_name']}基于{l1_level}数据, "
              f"样本={leg1['pick'].get('sample', 0)}场 命中率={hit1:.1%}")
        print(
            f"  2) 第{leg2['match_index']}场 {leg2['league']} {leg2['home']} vs {leg2['away']} | "
            f"{leg2['bet_name']} -> {leg2['pick']['result_key']}({leg2['pick']['description']}) "
            f"命中率={hit2:.1%} 赔率={o2:.2f} 置信度={leg2['pick']['confidence']}"
        )
        print(f"     └ 理由: {leg2['match_label'] or leg2['bet_name']}基于{l2_level}数据, "
              f"样本={leg2['pick'].get('sample', 0)}场 命中率={hit2:.1%}")
        print(f"  组合赔率(近似): {combo_odds:.2f}")
        print(f"  组合命中率(独立近似): {combo_hit:.1%}")

        parlay_summary.append({
            "type": "常规",
            "combo_odds": combo_odds,
            "combo_hit_rate": combo_hit,
            "legs": [
                {
                    "match_num": leg1.get("match_num", ""),
                    "label": leg1.get("match_label", ""),
                    "play": leg1.get("bet_name", ""),
                    "bet_type": leg1.get("bet_type", ""),
                    "result_key": leg1["pick"]["result_key"],
                },
                {
                    "match_num": leg2.get("match_num", ""),
                    "label": leg2.get("match_label", ""),
                    "play": leg2.get("bet_name", ""),
                    "bet_type": leg2.get("bet_type", ""),
                    "result_key": leg2["pick"]["result_key"],
                },
            ],
        })


        if h1 and h2:
            p1 = h1["handicap_pick"]["pick"]
            p2 = h2["handicap_pick"]["pick"]
            ho1 = p1.get("current_odds", 1.0)
            ho2 = p2.get("current_odds", 1.0)
            hcombo_odds = ho1 * ho2
            hh1 = p1.get("hit_rate", 0.0)
            hh2 = p2.get("hit_rate", 0.0)
            hcombo_hit = hh1 * hh2

            print(f"\n{'━' * 70}")
            print("  【2串1 推荐（让球专用）】")
            print(f"{'━' * 70}")
            print(
                f"  1) 第{h1['match_index']}场 {h1['league']} {h1['home']} vs {h1['away']} | "
                f"让球胜平负 -> {p1['result_key']}({p1['description']}) "
                f"命中率={hh1:.1%} 赔率={ho1:.2f} 置信度={p1['confidence']}"
            )
            print(
                f"  2) 第{h2['match_index']}场 {h2['league']} {h2['home']} vs {h2['away']} | "
                f"让球胜平负 -> {p2['result_key']}({p2['description']}) "
                f"命中率={hh2:.1%} 赔率={ho2:.2f} 置信度={p2['confidence']}"
            )
            print(f"  组合赔率(近似): {hcombo_odds:.2f}")
            print(f"  组合命中率(独立近似): {hcombo_hit:.1%}")
            parlay_summary.append({
                "type": "让球",
                "combo_odds": hcombo_odds,
                "combo_hit_rate": hcombo_hit,
                "legs": [
                    {
                        "match_num": h1.get("match_num", ""),
                        "label": h1.get("match_label", ""),
                        "play": "让球",
                        "bet_type": "handicap",
                        "result_key": p1["result_key"],
                    },
                    {
                        "match_num": h2.get("match_num", ""),
                        "label": h2.get("match_label", ""),
                        "play": "让球",
                        "bet_type": "handicap",
                        "result_key": p2["result_key"],
                    },
                ],
            })



        hparlay_summary = {"type": "让球", "combo_odds": hcombo_odds, "combo_hit_rate": hcombo_hit}
        parlay_summary.append(hparlay_summary)
    # === 高赔组合 2串1 === 
    high_cc = []
    for pc in parlay_candidates:
        pk = pc.get("pick", {})
        if not pk: continue
        sc = pk.get("hit_rate", 0) * (pk.get("current_odds", 1.0) ** 0.5) * max(0.5, pk.get("confidence_score", 0))
        high_cc.append((sc, pc))
    high_cc.sort(key=lambda x: x[0], reverse=True)
    best_hc = []
    top4 = high_cc[:max(4, len(high_cc))]
    for i in range(len(top4)):
        for j in range(i+1, len(top4)):
            _, c1 = top4[i]; _, c2 = top4[j]
            p1 = c1["pick"]; p2 = c2["pick"]
            ho = p1.get("current_odds", 1.0) * p2.get("current_odds", 1.0)
            jh = p1.get("hit_rate", 0) * p2.get("hit_rate", 0)
            lp = 0.20 if c1.get("league") == c2.get("league") else 0
            sh = jh * ho * min(p1.get("confidence_score",1), p2.get("confidence_score",1)) * (1-lp)
            best_hc.append((sh, c1, c2, ho, jh))
    best_hc.sort(key=lambda x: x[0], reverse=True)
    if best_hc and best_hc[0][3] >= 2.5:
        print(f"\n{'━' * 70}")
        print("  【2串1 推荐（高赔组合）】")
        print(f"{'━' * 70}")
        def gl(m): return m.get("all_bet_types",{}).get(m.get("bet_type",""),"")
        seen_legs = {(leg1.get("match_index"), leg1.get("bet_type")), (leg2.get("match_index"), leg2.get("bet_type"))}
        shown = 0
        for sh, c1, c2, hco, hjh in best_hc:
            if shown >= 2: break
            k1 = (c1.get("match_index"), c1.get("bet_type"))
            k2 = (c2.get("match_index"), c2.get("bet_type"))
            if k1 in seen_legs and k2 in seen_legs: continue
            if hco < 2.5: continue
            p1,p2 = c1["pick"], c2["pick"]
            ho1,ho2 = p1.get("current_odds",1.0), p2.get("current_odds",1.0)
            l1l,l2l = gl(c1), gl(c2)
            shown += 1
            print(f"  {shown}) 第{c1['match_index']}场 {c1['league']} {c1['home']} vs {c1['away']} | {c1['bet_name']} {p1['result_key']}({p1['description']}) 命中率={p1['hit_rate']:.0%} 赔率={ho1:.2f}")
            print(f"     └ 理由: {l1l} {p1.get('sample',0)}场样本")
            print(f"     + 第{c2['match_index']}场 {c2['league']} {c2['home']} vs {c2['away']} | {c2['bet_name']} {p2['result_key']}({p2['description']}) 命中率={p2['hit_rate']:.0%} 赔率={ho2:.2f}")
            print(f"     └ 理由: {l2l} {p2.get('sample',0)}场样本")
            print(f"  组合赔率: {hco:.2f}  联合命中率: {hjh:.1%}  可信度: {p1.get('confidence','低')}+{p2.get('confidence','低')}")

    print(f"\n{'━' * 70}")
    print(f"  全部 {len(matches)} 场比赛推荐完成")
    print(f"{'━' * 70}")

    append_recommend_comments_to_json(parlay_candidates, parlay_summary=parlay_summary)

    conn.close()

def interactive():
    """交互式推荐"""
    conn = connect_db()
    if not conn:
        return
    print("=" * 70)
    print("竞彩投注推荐")
    print("=" * 70)
    print("输入比赛信息获取推荐（输入 q 退出）")
    print("没开的玩法直接留空跳过即可")
    print()
    while True:
        print("-" * 40)
        league = input("联赛 (如 英超): ").strip()
        if league.lower() == "q":
            break
        home = input("主队: ").strip()
        if home.lower() == "q":
            break
        away = input("客队: ").strip()
        if away.lower() == "q":
            break
        handicap = input("让球 (如 -1, +1, -2): ").strip()
        if handicap.lower() == "q":
            break
        # 收集各玩法赔率
        bet_odds = {}
        for bt, info in BET_TYPES.items():
            print(f"\n  --- {info['name']}赔率（留空跳过整个玩法，输入 q 退出）---")
            bt_odds = {}
            all_empty = True
            for rk in info["results"]:
                desc = (info.get("result_map") or {}).get(rk, rk)
                val = input(f"    {rk}({desc}): ").strip()
                if val.lower() == "q":
                    print("退出。")
                    conn.close()
                    return
                odds = safe_float(val)
                if odds and odds > 0:
                    bt_odds[rk] = odds
                    all_empty = False
            if not all_empty:
                bet_odds[bt] = bt_odds
        if not bet_odds:
            print("未输入任何赔率，跳过。")
            continue
        # 输出推荐
        print("\n" + "=" * 70)
        print("竞彩全玩法投注推荐")
        print("=" * 70)
        match_data = {
            "league": league,
            "home": home,
            "away": away,
            "handicap": handicap,
            "bet_odds": bet_odds,
        }
        process_match(conn, match_data)
        print("=" * 70)
        print()
    conn.close()
def print_txt_format_help():
    """打印txt文件格式说明"""
    print("=" * 70)
    print("txt输入文件格式说明")
    print("=" * 70)
    print("""
每场比赛以 [match] 开头，包含以下字段：
  基本信息（必填）：
    league=英超          联赛名称
    home=曼城            主队名称
    away=伯恩利          客队名称
    handicap=-2          让球（负数=主场让球，正数=主场受让）
  胜平负赔率（没开就不写）：
    wdl_3=1.85           主胜赔率
    wdl_1=3.50           平局赔率
    wdl_0=4.20           客胜赔率
  让球胜平负赔率：
    handicap_3=1.85      赢盘赔率
    handicap_1=3.50      走盘赔率
    handicap_0=4.20      输盘赔率
示例文件内容：
──────────────────────────────────────
# 今天要看的比赛
[match]
league=英超
home=曼城
away=伯恩利
handicap=-2
wdl_3=1.85
wdl_1=3.50
wdl_0=4.20
handicap_3=1.85
handicap_1=3.50
handicap_0=4.20
[match]
league=西甲
home=皇马
away=巴萨
handicap=-1
wdl_3=2.10
wdl_1=3.40
wdl_0=3.80
handicap_3=1.95
handicap_1=3.60
handicap_0=3.90
──────────────────────────────────────
没开的玩法直接不写即可，推荐器会自动跳过。
# 开头的行是注释，会被忽略。
""")
if __name__ == "__main__":
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "--help" or arg == "-h":
            print_txt_format_help()
        elif arg == "--file" or arg == "-f":
            if len(sys.argv) < 3:
                print("用法: python3 recommend.py --file input.txt")
                print("      python3 recommend.py -f input.txt")
            else:
                filepath = sys.argv[2]
                if not os.path.exists(filepath):
                    print(f"错误: 文件不存在 ({filepath})")
                else:
                    run_from_file(filepath)
        else:
            # 直接传文件路径
            if os.path.exists(arg):
                run_from_file(arg)
            else:
                print(f"错误: 文件不存在 ({arg})")
                print("用法: python3 recommend.py --file input.txt")
    else:
        # 默认读取 input.txt
        if os.path.exists("input.txt"):
            run_from_file("input.txt")
        else:
            print("未找到 input.txt，进入交互模式...")
            interactive()
