#!/usr/bin/env python3
"""
新球体育投注推荐器（胜平负+让球版）
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
import urllib.request
import urllib.error
from datetime import datetime
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)  # /Users/jojo/Desktop/TianyiSport/code/新球体育
DATA_DIR = os.path.join(BASE_DIR, "竞彩数据")
TRANSFORMED_DIR = os.path.join(BASE_DIR, "联赛数据", "转化数据")
RESULT_DIR = os.path.join(BASE_DIR, "推荐结果")
DB_PATH = os.path.join(SCRIPT_DIR, "pattern_db.sqlite")
# ============ 玩法映射 ============
WDL_MAP = {"3": "主胜", "1": "平局", "0": "客胜"}
WDL_DISPLAY_KEY = {"3": "胜", "1": "平", "0": "负"}  # JSON输出用（result_key）
HANDICAP_RESULT_MAP = {"3": "赢盘", "1": "走盘", "0": "输盘"}
HALF_FULL_MAP = {
    "3-3": "主胜-主胜", "1-1": "平-平", "0-0": "客胜-客胜",
    "1-3": "平-主胜", "1-0": "平-客胜", "3-1": "主胜-平",
    "3-0": "主胜-客胜", "0-1": "客胜-平", "0-3": "客胜-主胜",
}
SHORT_HF_MAP = {
    "3-3": "胜胜", "3-1": "胜平", "3-0": "胜负",
    "1-3": "平胜", "1-1": "平平", "1-0": "平负",
    "0-3": "负胜", "0-1": "负平", "0-0": "负负",
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


def display_wdl_rk(rk, bet_type):
    """将 result_key 从 "3"/"1"/"0" 转换为展示用的 "胜"/"平"/"负"；其他玩法原样返回"""
    if bet_type in ("wdl", "handicap"):
        return WDL_DISPLAY_KEY.get(rk, rk)
    return rk


def build_reverse_parlay(parlay_type, rk1_display, rk2_display, bet_odds1=None, bet_odds2=None, hit1=None, hit2=None):
    """构建反向2串1字符串（parlay级别），含覆盖投注平均回报率。

    格式: '选项1--选项2(avg_return)'
    展示全部反向选项，赔率按覆盖投注计算：
      - N种组合各投10元，总投入 = 10×N
      - 平均回报率 = sum(所有组合赔率) / N²
    """
    _rk_map = {"胜": "3", "平": "1", "负": "0"}

    def _get_candidates(rk_display):
        if parlay_type == "胜平负":
            return {"胜": ["负"], "负": ["胜"], "平": ["胜", "负"]}.get(rk_display, [])
        return {"胜": ["平", "负"], "负": ["平", "胜"], "平": ["胜", "负"]}.get(rk_display, [])

    cand1 = _get_candidates(rk1_display)
    cand2 = _get_candidates(rk2_display)
    if not cand1 or not cand2:
        return ""

    # 展示全部反选项: "平+负--平+负"
    opt_display1 = "+".join(cand1)
    opt_display2 = "+".join(cand2)
    num_combos = len(cand1) * len(cand2)

    # 覆盖投注：计算所有子组合赔率，取平均值
    combo_odds_list = []
    for c1 in cand1:
        for c2 in cand2:
            o1 = (bet_odds1 or {}).get(_rk_map.get(c1, c1), 0)
            o2 = (bet_odds2 or {}).get(_rk_map.get(c2, c2), 0)
            if o1 > 0 and o2 > 0:
                combo_odds_list.append(o1 * o2)

    # 平均回报率 = 各投10元/组合, 总投入10×N, 命中得10×odds, 回报=odds/N
    avg_return = None
    if combo_odds_list:
        avg_return = sum(combo_odds_list) / len(combo_odds_list) / num_combos

    # 反向命中判断
    rev_hit = None
    if hit1 is False and hit2 is False:
        rev_hit = True
    elif hit1 is True or hit2 is True:
        rev_hit = False

    result = f"{opt_display1}--{opt_display2}"
    if avg_return:
        result += f"({avg_return:.2f})"
    if rev_hit is True:
        result += "(1)"
    elif rev_hit is False:
        result += "(0)"

    return result


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
    init_panlu_table(conn)
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


def query_team_yearly_stats(conn, scope, team_name, league=None, period_days=365):
    """查询某个球队在指定 scope（home/away/overall）下的近一年统计数据。
    返回: dict 或 None
    """
    if not team_name:
        return None
    if league:
        row = conn.execute("""
            SELECT * FROM team_yearly_stats
            WHERE scope=? AND league=? AND team_name=? AND period_days=?
        """, (scope, league, team_name, period_days)).fetchone()
        if row:
            return dict(row)
    row = conn.execute("""
        SELECT * FROM team_yearly_stats
        WHERE scope=? AND team_name=? AND period_days=?
    """, (scope, team_name, period_days)).fetchone()
    if row:
        return dict(row)
    return None


def get_yearly_stats_insights(conn, home, away, league):
    """提取两队逐年统计中有意义的指标，用于推荐加权和展示。
    返回: {
        "home": { 主队 overall 统计字典 },
        "away": { 客队 overall 统计字典 },
        "home_home_stats": { 主队 home 统计 },
        "away_away_stats": { 客队 away 统计 },
        "insights": [ "平局倾向: 天狼星近一年平局率38%", ... ]
    }
    """
    h_all = query_team_yearly_stats(conn, "overall", home, league=league) or {}
    a_all = query_team_yearly_stats(conn, "overall", away, league=league) or {}
    h_home = query_team_yearly_stats(conn, "home", home, league=league) or {}
    a_away = query_team_yearly_stats(conn, "away", away, league=league) or {}

    def _pct(count, total):
        if total <= 0:
            return 0.0
        return count / total

    insights = []

    # 平局倾向
    h_draw_pct = _pct(h_all.get("draws", 0), h_all.get("total_matches", 0))
    a_draw_pct = _pct(a_all.get("draws", 0), a_all.get("total_matches", 0))
    if h_draw_pct >= 0.35 and a_all.get("total_matches", 0) >= 10:
        insights.append(f"平局倾向: {home} 近一年平局率 {h_draw_pct:.0%}（{h_all.get('draws',0)}/{h_all.get('total_matches',0)}场）")
    if a_draw_pct >= 0.35 and a_all.get("total_matches", 0) >= 10:
        insights.append(f"平局倾向: {away} 近一年平局率 {a_draw_pct:.0%}（{a_all.get('draws',0)}/{a_all.get('total_matches',0)}场）")

    # 输弱队倾向
    h_lw_pct = _pct(h_all.get("losses_to_weaker", 0), h_all.get("total_matches", 0))
    a_lw_pct = _pct(a_all.get("losses_to_weaker", 0), a_all.get("total_matches", 0))
    if h_lw_pct >= 0.25 and h_all.get("total_matches", 0) >= 10:
        insights.append(f"爆冷风险: {home} 近一年 {h_lw_pct:.0%} 输给弱队（{h_all.get('losses_to_weaker',0)}次）")
    if a_lw_pct >= 0.25 and a_all.get("total_matches", 0) >= 10:
        insights.append(f"爆冷风险: {away} 近一年 {a_lw_pct:.0%} 输给弱队（{a_all.get('losses_to_weaker',0)}次）")

    # 被逆转倾向
    h_cc_pct = _pct(h_all.get("comebacks_conceded", 0), h_all.get("total_matches", 0))
    a_cc_pct = _pct(a_all.get("comebacks_conceded", 0), a_all.get("total_matches", 0))
    if h_cc_pct >= 0.20 and h_all.get("total_matches", 0) >= 10:
        insights.append(f"被逆转: {home} 近一年 {h_cc_pct:.0%} 被逆转（{h_all.get('comebacks_conceded',0)}次）")
    if a_cc_pct >= 0.20 and a_all.get("total_matches", 0) >= 10:
        insights.append(f"被逆转: {away} 近一年 {a_cc_pct:.0%} 被逆转（{a_all.get('comebacks_conceded',0)}次）")

    # 大球倾向
    h_over_pct = _pct(h_all.get("over_25", 0), h_all.get("over_25", 0) + h_all.get("under_25", 0))
    a_over_pct = _pct(a_all.get("over_25", 0), a_all.get("over_25", 0) + a_all.get("under_25", 0))
    if h_over_pct >= 0.60 and h_home.get("total_matches", 0) >= 10:
        insights.append(f"大球倾向: {home} 近一年 >2.5球 率 {h_over_pct:.0%}")
    if a_over_pct >= 0.60 and a_away.get("total_matches", 0) >= 10:
        insights.append(f"大球倾向: {away} 近一年 >2.5球 率 {a_over_pct:.0%}")
    # 小球倾向
    if h_over_pct <= 0.30 and (h_all.get("over_25", 0) + h_all.get("under_25", 0)) >= 10:
        insights.append(f"小球倾向: {home} 近一年 ≤2.5球 率 {1-h_over_pct:.0%}")
    if a_over_pct <= 0.30 and (a_all.get("over_25", 0) + a_all.get("under_25", 0)) >= 10:
        insights.append(f"小球倾向: {away} 近一年 ≤2.5球 率 {1-a_over_pct:.0%}")

    # 单双倾向
    h_odd_pct = _pct(h_all.get("odd_goals", 0), h_all.get("odd_goals", 0) + h_all.get("even_goals", 0))
    if abs(h_odd_pct - 0.5) >= 0.15 and (h_all.get("odd_goals", 0) + h_all.get("even_goals", 0)) >= 10:
        side = "单" if h_odd_pct > 0.5 else "双"
        insights.append(f"{side}数倾向: {home} 近一年 {side}数进球率 {max(h_odd_pct, 1-h_odd_pct):.0%}")

    result = {
        "home": h_all,
        "away": a_all,
        "home_home": h_home,
        "away_away": a_away,
        "insights": insights,
    }

    # 基于逐年统计的影响因子（用于加权计算）
    home_home_total = h_home.get("total_matches", 0)
    away_away_total = a_away.get("total_matches", 0)
    # 平局修正因子
    result["draw_factor"] = max(0.9, (h_draw_pct + a_draw_pct) / 2.0 + 0.5) if (h_all.get("total_matches", 0) >= 5 and a_all.get("total_matches", 0) >= 5) else 1.0
    # 大球倾向因子
    result["over_factor"] = (h_over_pct + a_over_pct) / 2.0 if (h_all.get("total_matches", 0) >= 5 and a_all.get("total_matches", 0) >= 5) else 0.5
    # 主场优势因子
    h_home_win_pct = _pct(h_home.get("wins", 0), h_home.get("total_matches", 0)) if h_home.get("total_matches", 0) >= 5 else 0
    a_away_win_pct = _pct(a_away.get("wins", 0), a_away.get("total_matches", 0)) if a_away.get("total_matches", 0) >= 5 else 0
    result["home_away_factor"] = (h_home_win_pct + (1.0 - a_away_win_pct)) / 2.0 if (home_home_total >= 5 and away_away_total >= 5) else 0.5

    return result


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

    # === 近一年平局/让平占比（替代全量历史数据） ===
    recent_draw_rates = {}
    recent_draw_total = 0
    try:
        if bet_type in ("wdl", "handicap"):
            result_col = "wdl_result" if bet_type == "wdl" else "handicap_result"
            sql = f"""
                SELECT {result_col} AS rk, COUNT(*) AS cnt
                FROM matches
                WHERE is_valid=1 AND wdl_result!='' AND {result_col}!=''
                  AND match_date >= date('now', '-365 days')
            """
            params = []
            if league:
                sql += " AND league=?"
                params.append(league)
            if handicap:
                sql += " AND handicap=?"
                params.append(handicap)
            sql += " GROUP BY rk"
            rows = conn.execute(sql, params).fetchall()
            total = sum(r[1] for r in rows) if rows else 0
            if total >= 5:
                for r in rows:
                    recent_draw_rates[r[0]] = r[1] / total
                recent_draw_total = total
    except Exception:
        pass

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

        # === 逐年统计微调（球队近一年行为模式） ===
        yi = yearly_insights
        if bet_type == "wdl" and yi:
            draw_factor = yi.get("draw_factor", 1.0)
            haf = yi.get("home_away_factor", 0.5)
            if rk == "1":
                # 平局：逐年平局倾向微调，最多 ±15%，仅当两队样本都≥5时生效
                if yi.get("home", {}).get("total_matches", 0) >= 5 and yi.get("away", {}).get("total_matches", 0) >= 5:
                    adj = min(1.15, max(0.85, draw_factor))
                    final_p = final_p * adj
                    weighted.append(("yearly_draw", adj, 0, 0.05 * abs(adj - 1.0) * 20))
            elif rk == "3":
                # 主胜方向微调：基于逐年主客场胜率
                yi_home_total = yi.get("home_home", {}).get("total_matches", 0)
                yi_away_total = yi.get("away_away", {}).get("total_matches", 0)
                if yi_home_total >= 5 and yi_away_total >= 5:
                    adj = 1.0 + (haf - 0.5) * 0.25  # ±12.5%
                    final_p = final_p * max(0.88, min(1.12, adj))
            elif rk == "0":
                yi_home_total = yi.get("home_home", {}).get("total_matches", 0)
                yi_away_total = yi.get("away_away", {}).get("total_matches", 0)
                if yi_home_total >= 5 and yi_away_total >= 5:
                    adj = 1.0 - (haf - 0.5) * 0.25  # 主胜方向的补集
                    final_p = final_p * max(0.88, min(1.12, adj))

        # === 球队近况胜率微调 ===
        if bet_type in ("wdl", "handicap"):
            if rk == "3" and home_form_info.get("reliable"):
                # 主场胜率可靠 → 调整主胜概率
                rate = home_form_info.get("rate", 50) / 100.0
                adj = 1.0 + (rate - 0.5) * 0.3  # ±15%
                final_p = final_p * max(0.85, min(1.15, adj))
                weighted.append(("team_form_home", rate, home_form_info.get("total", 0), 0.05))
            elif rk == "0" and away_form_info.get("reliable"):
                rate = away_form_info.get("rate", 50) / 100.0
                adj = 1.0 + (rate - 0.5) * 0.3
                final_p = final_p * max(0.85, min(1.15, adj))
                weighted.append(("team_form_away", rate, away_form_info.get("total", 0), 0.05))
            if rk in ("3", "0") and not home_form_info.get("reliable") and not away_form_info.get("reliable"):
                # 主客场都不够可靠 → 用总胜率微调
                home_rate = home_form_info.get("rate") or 50
                away_rate = away_form_info.get("rate") or 50
                avg_rate = (home_rate + away_rate) / 200.0  # 平均胜率/100
                adj = 1.0 + (avg_rate - 0.5) * 0.2  # ±10%
                final_p = final_p * max(0.90, min(1.10, adj))
                weighted.append(("team_form_overall", avg_rate, home_form_info.get("total", 0) + away_form_info.get("total", 0), 0.03))

        # === 平局/让平增信：基于近一年数据 ===
        if rk == "1" and bet_type in ("handicap", "wdl") and recent_draw_rates:
            rp_hit = recent_draw_rates.get("1", 0)
            rw_hit = recent_draw_rates.get("3", 0)
            rl_hit = recent_draw_rates.get("0", 0)
            rp_sample = recent_draw_total
            # 平局/让平 hit_rate 高于胜和负，且样本≥5
            if rp_hit > rw_hit and rp_hit > rl_hit and rp_sample >= 5:
                boost = 1.10
                final_p = min(1.0, final_p * boost)
                tag = "handicap_draw_dominant" if bet_type == "handicap" else "wdl_draw_dominant"
                label = "让平" if bet_type == "handicap" else "平局"
                weighted.append((tag, rp_hit, rp_sample, 0.05))
                print(f"  [{label}增信] {label}{rp_hit:.0%} > 胜{rw_hit:.0%} > 负{rl_hit:.0%} (近一年{rp_sample}场)")

        return final_p, weighted

    # ===== 逐年统计数据 =====
    yearly_insights = get_yearly_stats_insights(conn, home, away, league)

    # ===== 球队近况胜率分析（影响推荐） =====
    home_form_info = get_team_form_analysis(conn, home, "home")
    away_form_info = get_team_form_analysis(conn, away, "away")
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
    从澳客网抓取指定日期的新球体育数据（含赔率）。
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


def load_league_data_for_league(league_name):
    """从 转化数据 中加载指定联赛的最新赛季数据
    返回: {team_name: {half_full: {...}, handicap: {...}, big_small: {...}, ...}}
    或 None（找不到）
    """
    league_dir = os.path.join(TRANSFORMED_DIR, league_name)
    if not os.path.isdir(league_dir):
        return None
    # 取最新的赛季文件（文件名按字母排序最新的）
    jsons = sorted([f for f in os.listdir(league_dir) if f.endswith(".json")], reverse=True)
    if not jsons:
        return None
    latest = jsons[0]
    fp = os.path.join(league_dir, latest)
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return None

    # 构建 team_name → stats 映射
    team_stats = {}
    for tname in data.get("teams", []):
        team_stats[tname] = {}

    # 半全场
    for section in ("total", "home", "guest"):
        for row in data.get("half_full", {}).get(section, []):
            name = row.get("team_name", "")
            if name in team_stats:
                team_stats[name][f"half_full_{section}"] = row

    # 大小球
    for section in ("total", "home", "guest"):
        for row in data.get("big_small", {}).get(section, []):
            name = row.get("team_name", "")
            if name in team_stats:
                team_stats[name][f"big_small_{section}"] = row

    # 亚盘
    for section in ("total_panlu", "home", "guest"):
        for row in data.get("handicap", {}).get(section, []):
            name = row.get("team_name", "")
            if name in team_stats:
                team_stats[name][f"handicap_{section}"] = row

    # 单双
    for section in ("total", "home", "guest"):
        for row in data.get("odd_even", {}).get(section, []):
            name = row.get("team_name", "")
            if name in team_stats:
                team_stats[name][f"odd_even_{section}"] = row

    # 进球数
    for section in ("total", "home", "guest"):
        for row in data.get("goals", {}).get(section, []):
            name = row.get("team_name", "")
            if name in team_stats:
                team_stats[name][f"goals_{section}"] = row

    return {
        "league_name": league_name,
        "season": latest.replace(".json", ""),
        "team_stats": team_stats,
    }


def get_hit_from_league_data(ld, home, away, bet_type, result_key):
    """从联赛数据中提取某个玩法、某个结果的命中率
    LD数据的每个统计项已经是实际历史频率，可以直接用作命中率
    返回: (hit_rate, sample_count, source_description) 或 None
    """
    if not ld:
        return None

    team_stats = ld.get("team_stats", {})
    home_stats = team_stats.get(home, {})
    away_stats = team_stats.get(away, {})

    if bet_type == "wdl":
        # 从半全场推导胜平负：3-3+3-1+3-0 = 主胜, 1-3+1-1+1-0 = 平, 0-3+0-1+0-0 = 客胜
        hf = home_stats.get("half_full_home", {})
        af = away_stats.get("half_full_guest", {})
        # 反转客队的半全场（"guest" 已经是客队视角）
        # 主队视角的 WDL: 33+31+30 = 3, 13+11+10 = 1, 03+01+00 = 0
        total_map = {"3": ["33", "31", "30"], "1": ["13", "11", "10"], "0": ["03", "01", "00"]}
        codes = total_map.get(result_key, [])
        home_hit = sum(hf.get(c, 0) for c in codes) if hf.get("total_games", 0) > 0 else 0
        home_total = hf.get("total_games", 0)
        away_hit = sum(af.get(c, 0) for c in codes) if af.get("total_games", 0) > 0 else 0
        away_total = af.get("total_games", 0)

        total_hit = home_hit + away_hit
        total_n = home_total + away_total
        if total_n >= 10:
            return (total_hit / total_n, total_n, f"半全场统计(主{home_total}+客{away_total}场)")

    elif bet_type == "handicap":
        # 主队赢盘/走盘/输盘
        hc_h = home_stats.get("handicap_home", {})
        hc_a = away_stats.get("handicap_guest", {})
        hc_total = home_stats.get("handicap_total", {})
        rk_map = {"3": "win_pct", "1": "draw_pct", "0": "lose_pct"}
        key = rk_map.get(result_key, "win_pct")
        # 用总数据更稳定
        if hc_total.get("total_games", 0) >= 10:
            hr = hc_total.get(key, 0) / 100.0
            return (hr, hc_total["total_games"], f"亚盘{key}")

    elif bet_type == "goal":
        # 进球数分布
        g_total = home_stats.get("goals_total", {})
        if g_total.get("total_games", 0) >= 10 and result_key in g_total:
            cnt = g_total.get(result_key, 0)
            total = g_total["total_games"]
            return (cnt / total, total, f"进球数分布({result_key}球)")

    elif bet_type == "half_full":
        hf_h = home_stats.get("half_full_home", {})
        hf_a = away_stats.get("half_full_guest", {})
        h_total = hf_h.get("total_games", 0)
        a_total = hf_a.get("total_games", 0)
        hr = hf_h.get(result_key, 0) if h_total > 0 else 0
        ar = hf_a.get(result_key, 0) if a_total > 0 else 0
        total_n = h_total + a_total
        if total_n >= 10:
            return ((hr + ar) / (h_total + a_total), total_n, f"半全场(主{h_total}+客{a_total})")

    elif bet_type == "score":
        sc_h = home_stats.get("scores_home", {})
        sc_a = away_stats.get("scores_guest", {})
        hr = sc_h.get(result_key, 0)
        ar = sc_a.get(result_key, 0)
        h_total = sc_h.get("total_games", 0)
        a_total = sc_a.get("total_games", 0)
        total_n = h_total + a_total
        if total_n >= 10:
            return ((hr + ar) / total_n, total_n, f"比分(主{h_total}+客{a_total})")

    return None


def _fuzzy_match(name_a, name_b):
    """模糊队名匹配：至少2个相同字符即认为匹配"""
    if not name_a or not name_b:
        return False
    if name_a == name_b:
        return True
    # 取较短的名称做匹配基准
    short, long_ = (name_a, name_b) if len(name_a) <= len(name_b) else (name_b, name_a)
    # 统计短名称的每个字是否在长名称中出现
    match_count = sum(1 for ch in short if ch in long_)
    return match_count >= 2


# ============ 条件推导（基于WDL+Handicap） ============

def recommend_conditional_derived(conn, league, handicap, wdl_pick_key, hc_pick_key, home=None, away=None, min_sample=20):
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

        rows = None
        level_name = None
        if league and handicap:
            rows = _query_conditional(conn, result_col, odds_col,
                                       league=league, handicap=handicap,
                                       wdl_rk=wdl_pick_key, hc_rk=hc_pick_key)
            if rows and sum(r["cnt"] for r in rows) >= min_sample:
                level_name = f"{league}|handicap={handicap}"

        if not level_name and league:
            rows = _query_conditional(conn, result_col, odds_col,
                                       league=league, handicap=None,
                                       wdl_rk=wdl_pick_key, hc_rk=hc_pick_key)
            if rows and sum(r["cnt"] for r in rows) >= min_sample:
                level_name = f"{league}（不限让球）"

        if not level_name and handicap:
            rows = _query_conditional(conn, result_col, odds_col,
                                       league=None, handicap=handicap,
                                       wdl_rk=wdl_pick_key, hc_rk=hc_pick_key)
            if rows and sum(r["cnt"] for r in rows) >= min_sample:
                level_name = f"handicap={handicap}（不限联赛）"

        if not level_name:
            rows = _query_conditional(conn, result_col, odds_col,
                                       league=None, handicap=None,
                                       wdl_rk=wdl_pick_key, hc_rk=hc_pick_key)
            if rows and sum(r["cnt"] for r in rows) >= min_sample:
                level_name = "全局"

        if not rows:
            derived[bt_key] = {"name": bt_name, "level": None, "items": []}
            continue

        # === 比分专用：融合球队历史比分分布，比分比进球数更具可信 ===
        if bt_key == "score" and home and away:
            team_rows = _query_team_score_distribution(
                conn, home, away, league, handicap, wdl_pick_key, hc_rk=hc_pick_key
            )
            if team_rows:
                team_total = sum(r["cnt"] for r in team_rows)
                global_total = sum(r["cnt"] for r in rows)
                if team_total >= 10 and global_total >= 20:
                    # 球队权重 0.6, 全局权重 0.4
                    team_weight, global_weight = 0.6, 0.4
                    merged = {}
                    # 先加载全局数据
                    for r in rows:
                        merged[r["rk"]] = {
                            "score": global_weight * r["cnt"],
                            "cnt": r["cnt"],
                            "avg_odds": r["avg_odds"],
                        }
                    # 叠加球队数据（权重更高）
                    for r in team_rows:
                        rk = r["rk"]
                        team_odds = r.get("avg_odds") or 0
                        if rk in merged:
                            merged[rk]["score"] += team_weight * r["cnt"]
                            merged[rk]["cnt"] += r["cnt"]
                            if merged[rk]["avg_odds"] and team_odds:
                                merged[rk]["avg_odds"] = (merged[rk]["avg_odds"] + team_odds) / 2
                            elif team_odds:
                                merged[rk]["avg_odds"] = team_odds
                        else:
                            merged[rk] = {
                                "score": team_weight * r["cnt"],
                                "cnt": r["cnt"],
                                "avg_odds": team_odds,
                            }
                    # 按融合评分重排序
                    sorted_rows = sorted(merged.items(), key=lambda x: x[1]["score"], reverse=True)
                    rows = [
                        {"rk": rk, "cnt": info["cnt"], "avg_odds": info["avg_odds"]}
                        for rk, info in sorted_rows
                    ]
                    level_name = f"{level_name or '全局'} + 球队历史"

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


def _query_conditional(conn, result_col, odds_col, league, handicap, wdl_rk, hc_rk=None):
    """查询给定条件下的结果分布（hc_rk 可选，为 None 时不约束让球结果）"""
    where = ["is_valid = 1", "wdl_result = ?",
             f"{result_col} != ''"]
    params = [wdl_rk]

    if hc_rk is not None:
        where.append("handicap_result = ?")
        params.append(hc_rk)

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


def _query_team_score_distribution(conn, home, away, league, handicap, wdl_rk, hc_rk=None):
    """查询主客队各自历史比分分布，用于增强比分推荐的可信度。

    主队视角：主队出场且 wdl_rk 发生时的比分分布
    客队视角：客队出场且对应 wdl_rk 发生时的比分分布
    融合两队的分布，主队权重 0.6，客队权重 0.4。
    """
    def _score_dist(team, side, result_key):
        where = ["is_valid = 1", "wdl_result = ?", "score_result != ''"]
        params = [result_key]
        if side == "home":
            where.append("home = ?")
        else:
            where.append("away = ?")
        params.append(team)
        if league:
            where.append("league = ?")
            params.append(league)
        if handicap:
            where.append("handicap = ?")
            params.append(handicap)
        if hc_rk is not None:
            where.append("handicap_result = ?")
            params.append(hc_rk)
        try:
            cursor = conn.execute(f"""
                SELECT score_result AS rk, COUNT(*) AS cnt, AVG(score_odds) AS avg_odds
                FROM matches
                WHERE {' AND '.join(where)}
                GROUP BY rk ORDER BY cnt DESC LIMIT 6
            """, params)
            return [{"rk": row[0], "cnt": row[1], "avg_odds": row[2]} for row in cursor]
        except Exception:
            return []

    away_rk_map = {"3": "0", "1": "1", "0": "3"}
    away_wdl_rk = away_rk_map.get(wdl_rk, wdl_rk)

    home_dist = _score_dist(home, "home", wdl_rk)
    away_dist = _score_dist(away, "away", away_wdl_rk)

    if not home_dist and not away_dist:
        return None

    merged = {}
    for d in home_dist:
        merged[d["rk"]] = {"score": 0.6 * d["cnt"], "cnt": d["cnt"], "avg_odds": d["avg_odds"]}
    for d in away_dist:
        if d["rk"] in merged:
            merged[d["rk"]]["score"] += 0.4 * d["cnt"]
            merged[d["rk"]]["cnt"] += d["cnt"]
            if merged[d["rk"]]["avg_odds"]:
                merged[d["rk"]]["avg_odds"] = (merged[d["rk"]]["avg_odds"] + d["avg_odds"]) / 2
            else:
                merged[d["rk"]]["avg_odds"] = d["avg_odds"]
        else:
            merged[d["rk"]] = {"score": 0.4 * d["cnt"], "cnt": d["cnt"], "avg_odds": d["avg_odds"]}

    sorted_merged = sorted(merged.items(), key=lambda x: x[1]["score"], reverse=True)
    entries = [{"rk": rk, "cnt": info["cnt"], "avg_odds": info["avg_odds"]}
               for rk, info in sorted_merged]

    total_sample = sum(d["cnt"] for d in merged.values())
    return entries if total_sample >= 10 else None


def pick_coherent_pair(wdl_results, hc_results, handicap_line):
    """
    从 WDL 和 Handicap 推荐结果中选择最一致的(pair, pair)。
    一致性规则：
    - WDL 方向必须与 Handicap 方向一致（主队方向或客队方向）
    - 平局/走盘视为中性，可与任何方向搭配
    返回: {
        "wdl_pick": 最佳 WDL 结果,
        "hc_pick":  与之最一致的 Handicap 结果,
        "conflict_desc": 冲突说明,
        "cold_desc": 冷门说明,
        "coherent": 是否一致,
        "_swapped": 是否因方向不一致调整了推荐,
    }
    """
    def direction(rk):
        if rk == "3": return "home"
        if rk == "0": return "away"
        return "neutral"

    def is_coherent(w_rk, h_rk):
        wd = direction(w_rk)
        hd = direction(h_rk)
        if wd == "neutral" or hd == "neutral":
            return True
        return wd == hd

    raw_wdl_top = wdl_results[0]["result_key"] if wdl_results else None
    raw_hc_top = hc_results[0]["result_key"] if hc_results else None

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
        best["wdl_pick"] = wdl_results[0] if wdl_results else None
        best["hc_pick"] = hc_results[0] if hc_results else None
        best["coherent"] = False
        best["conflict_desc"] = "WDL与让球方向不一致，建议分开判断或避开这场比赛"

    if best["wdl_pick"] and best["hc_pick"]:
        sel_w = best["wdl_pick"]["result_key"]
        sel_h = best["hc_pick"]["result_key"]
        swapped_side = None
        if sel_w != raw_wdl_top:
            swapped_side = "WDL"
        elif sel_h != raw_hc_top:
            swapped_side = "Handicap"
        if swapped_side:
            raw_hc_info = None
            for r in hc_results:
                if r["result_key"] == raw_hc_top:
                    raw_hc_info = r
                    break
            hc_detail = ""
            if raw_hc_info:
                hc_detail = (f"（Handicap {raw_hc_top} {HANDICAP_RESULT_MAP.get(raw_hc_top,raw_hc_top)}"
                             f" 命中率={raw_hc_info['hit_rate']:.1%} 赔率={raw_hc_info['current_odds']:.2f}）")
            best["conflict_desc"] = (f"WDL {raw_wdl_top}({WDL_MAP.get(raw_wdl_top,raw_wdl_top)})"
                                     f" + Handicap {raw_hc_top}({HANDICAP_RESULT_MAP.get(raw_hc_top,raw_hc_top)})"
                                     f" 方向相悖，调整为当前方向一致的组合{hc_detail}")
            best["_swapped"] = True

    # === 检查 WDL 冷门 ===
    w_rk = best["wdl_pick"]["result_key"] if best["wdl_pick"] else ""
    w_cold = best["wdl_pick"].get("cold_percentile", 0) if best["wdl_pick"] else 0
    w_hit = best["wdl_pick"].get("hit_rate", 0) if best["wdl_pick"] else 0
    if w_cold >= 0.85 and w_hit <= 0.20:
        best["cold_desc"] = (f"WDL主推项({WDL_MAP.get(w_rk,w_rk)})冷门指数较高"
                             f"({best['wdl_pick'].get('cold_index',0):.1f}, 分位{w_cold:.0%})，存在爆冷风险")

    return best


def format_unified_recommendation(best, derived):
    """打印统一推荐结果"""
    wp = best["wdl_pick"]
    hp = best["hc_pick"]
    w_name = WDL_MAP.get(wp["result_key"], wp["result_key"]) if wp else None
    h_name = HANDICAP_RESULT_MAP.get(hp["result_key"], hp["result_key"]) if hp else None

    def direction_tag(rk, bet_type):
        if rk == "3":
            return "🔵 主队方向" if bet_type == "wdl" else "🔵 主队赢盘"
        if rk == "0":
            return "🔴 客队方向" if bet_type == "wdl" else "🔴 客队赢盘"
        return "⚪ 中性"

    w_tag = direction_tag(wp["result_key"], "wdl") if wp else ""
    h_tag = direction_tag(hp["result_key"], "handicap") if hp else ""

    lines = []
    lines.append(f"\n  ★ 统一推荐")
    lines.append(f"  ┌────────────────────────────────────────────────────────────────")
    if wp:
        lines.append(f"  │ 胜平负 → {wp['result_key']}({w_name}) {w_tag}  "
                     f"命中率={wp['hit_rate']:.1%} 赔率={wp['current_odds']:.2f}")
    if hp:
        lines.append(f"  │ 让球   → {hp['result_key']}({h_name}) {h_tag}  "
                     f"命中率={hp['hit_rate']:.1%} 赔率={hp['current_odds']:.2f}")

    for bt_key in ["score", "goal", "half_full"]:
        dd = derived.get(bt_key)
        if dd and dd.get("items"):
            top = dd["items"][0]
            lines.append(f"  │ {dd['name']} → {top['result_key']}({top['description']})  "
                         f"命中率={top['hit_rate']:.1%}")

    lines.append(f"  └────────────────────────────────────────────────────────────────")

    warnings = []
    if best.get("conflict_desc"):
        warnings.append(f"  ⚠ 注意: {best['conflict_desc']}")
    if best.get("cold_desc"):
        warnings.append(f"  ⚠ 冷门: {best['cold_desc']}")

    # 检查半全场是否有与推荐方向矛盾的高频结果
    if derived and not best.get("conflict_desc") and wp:
        hf = derived.get("half_full")
        if hf and hf.get("items") and len(hf["items"]) >= 2:
            top_hf = hf["items"][:3]
            direction_mismatch = []
            w_rk_check = wp["result_key"]
            for item in top_hf:
                full_part = item["result_key"].split("-")[1] if "-" in item["result_key"] else ""
                if w_rk_check == "3" and full_part == "0":
                    direction_mismatch.append(item)
                elif w_rk_check == "0" and full_part == "3":
                    direction_mismatch.append(item)
            if direction_mismatch:
                items_str = ", ".join(
                    f"{m['result_key']}({HALF_FULL_MAP.get(m['result_key'], m['result_key'])} "
                    f"命中率={m['hit_rate']:.1%})"
                    for m in direction_mismatch
                )
                warnings.append(f"  ⚠ 冷门: 半全场出现与方向不一致的高频结果: {items_str}，"
                                f"建议留意对手反击得分能力")

    lines.append("")
    for w in warnings:
        lines.append(w)

    return "\n".join(lines)


# ============ 球队盘路数据（titan007 panlu 页面） ============

PANLU_TABLE = """
CREATE TABLE IF NOT EXISTS team_panlu (
    match_id INTEGER PRIMARY KEY,
    league TEXT,
    match_time TEXT,
    home_team TEXT,
    away_team TEXT,
    home_team_id INTEGER,
    away_team_id INTEGER,
    home_score INTEGER,
    away_score INTEGER,
    home_half INTEGER,
    away_half INTEGER,
    handicap REAL,
    over_under REAL,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_panlu_home_id ON team_panlu(home_team_id);
CREATE INDEX IF NOT EXISTS idx_panlu_away_id ON team_panlu(away_team_id);
CREATE TABLE IF NOT EXISTS panlu_fetch_log (
    sid INTEGER PRIMARY KEY,
    fetched_at TEXT,
    status TEXT DEFAULT 'success'
);
"""

def init_panlu_table(conn):
    """初始化盘路数据表"""
    conn.executescript(PANLU_TABLE)
    conn.commit()

def fetch_and_store_panlu(conn, sid):
    """
    从 titan007 盘路页面抓取比赛历史数据，存入 team_panlu 表。
    根据 panlu_fetch_log 判定是否需要抓取，避免重复请求。
    sid: 比赛数字ID，如 2907404
    """
    if not sid:
        return

    # 检查是否已成功抓取过
    log = conn.execute(
        "SELECT status FROM panlu_fetch_log WHERE sid=?",
        (sid,)
    ).fetchone()
    if log is not None:
        if log["status"] == "success":
            return  # 已抓取成功，跳过
        # status='failed' 可重新尝试

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    url = f"https://bf.titan007.com/panlu/{sid}.htm"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=15) as resp:
            html = resp.read().decode("gbk", errors="replace")
    except Exception:
        # 记录失败状态
        try:
            conn.execute(
                "INSERT OR REPLACE INTO panlu_fetch_log (sid, fetched_at, status) VALUES (?,?,?)",
                (sid, now_str, "failed")
            )
            conn.commit()
        except Exception:
            pass
        return

    # 提取 a[] 数组内容
    # 匹配: a[数字]=[...];
    pattern = r'a\[\d+\]\s*=\s*\[([^\]]+)\];'
    matches = re.findall(pattern, html)
    if not matches:
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    inserted = 0
    for m in matches:
        # 按逗号分割，处理引号和数值
        parts = []
        in_quote = False
        current = ""
        for ch in m:
            if ch == "'":
                in_quote = not in_quote
                continue
            if ch == "," and not in_quote:
                parts.append(current.strip())
                current = ""
                continue
            current += ch
        if current.strip():
            parts.append(current.strip())

        # parts: [match_id, league, color, time, home, away, home_id, away_id,
        #          h_score, a_score, h_half, a_half, handicap, ?, ?, over_under]
        if len(parts) < 16:
            continue
        try:
            mid = int(parts[0])
            league = parts[1].strip("'")
            mtime = parts[3].strip("'")
            home_team = parts[4].strip("'")
            away_team = parts[5].strip("'")
            home_id = int(parts[6])
            away_id = int(parts[7])
            h_score = int(parts[8])
            a_score = int(parts[9])
            h_half = int(parts[10])
            a_half = int(parts[11])
            handicap = float(parts[12]) if parts[12] else None
            over_under = float(parts[15]) if parts[15] else None
        except (ValueError, IndexError):
            continue

        try:
            conn.execute("""
                INSERT OR REPLACE INTO team_panlu
                (match_id, league, match_time, home_team, away_team,
                 home_team_id, away_team_id, home_score, away_score,
                 home_half, away_half, handicap, over_under, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (mid, league, mtime, home_team, away_team,
                  home_id, away_id, h_score, a_score,
                  h_half, a_half, handicap, over_under, now_str))
            inserted += 1
        except Exception:
            continue

    if inserted:
        conn.commit()

    # 无论是否插入了新数据（可能已存在），都标记为抓取成功
    try:
        conn.execute(
            "INSERT OR REPLACE INTO panlu_fetch_log (sid, fetched_at, status) VALUES (?,?,?)",
            (sid, now_str, "success")
        )
        conn.commit()
    except Exception:
        pass

def get_team_form(conn, team_id, num_matches=8):
    """
    查询某支球队最近 N 场比赛的表现。
    返回: {
        "wins": N, "draws": N, "losses": N,
        "goals_for": N, "goals_against": N,
        "home_wins": N, "away_wins": N,
        "total": N,
        "form": "WWDLW" 等连串结果
    }
    """
    rows = conn.execute("""
        SELECT home_score, away_score, home_team_id, away_team_id,
               home_team, away_team, match_time
        FROM team_panlu
        WHERE (home_team_id=? OR away_team_id=?)
          AND home_score IS NOT NULL
        ORDER BY match_time DESC
        LIMIT ?
    """, (team_id, team_id, num_matches)).fetchall()

    result = {"wins": 0, "draws": 0, "losses": 0,
              "goals_for": 0, "goals_against": 0,
              "home_wins": 0, "away_wins": 0,
              "total": len(rows), "form": ""}

    form_chars = []
    for row in rows:
        h_score = row["home_score"]
        a_score = row["away_score"]
        is_home = row["home_team_id"] == team_id
        if is_home:
            gf, ga = h_score, a_score
        else:
            gf, ga = a_score, h_score

        result["goals_for"] += gf
        result["goals_against"] += ga

        if gf > ga:
            result["wins"] += 1
            if is_home:
                result["home_wins"] += 1
            form_chars.append("W")
        elif gf == ga:
            result["draws"] += 1
            form_chars.append("D")
        else:
            result["losses"] += 1
            if not is_home:
                result["away_wins"] += 1
            form_chars.append("L")

    result["form"] = "".join(form_chars)
    return result


def get_team_over_under(conn, team_id, num_matches=8):
    """
    查询某支球队最近 N 场比赛的大小球倾向。
    返回: {"over_rate": float, "under_rate": float, "total": int}
    """
    rows = conn.execute("""
        SELECT home_score, away_score, over_under, home_team_id
        FROM team_panlu
        WHERE (home_team_id=? OR away_team_id=?)
          AND home_score IS NOT NULL
          AND over_under IS NOT NULL AND over_under > 0
        ORDER BY match_time DESC
        LIMIT ?
    """, (team_id, team_id, num_matches)).fetchall()

    over = 0
    under = 0
    for r in rows:
        total_goals = r["home_score"] + r["away_score"]
        line = r["over_under"]
        if total_goals >= line + 0.25:  # 大于盘口 = 大球
            over += 1
        elif total_goals <= line - 0.25:  # 小于盘口 = 小球
            under += 1
        # 介于 +/-0.25 之间的视为走水，不计

    total = over + under
    return {
        "over_rate": over / total if total > 0 else 0.5,
        "under_rate": under / total if total > 0 else 0.5,
        "total": total,
    }


def get_team_recent_win_rate(conn, team_name, last_n=10, max_days=365):
    """查询球队近N场总胜率（限最近max_days天内）。

    从 matches 表查该队最近 N 场比赛（仅限一年内的数据），
    统计赢球场次占比。主场赢: wdl_result='3', 客场赢: wdl_result='0'。
    只取有意义的时间范围数据，避免多年前的比赛干扰判断。
    """
    if not team_name:
        return None
    try:
        rows = conn.execute("""
            SELECT wdl_result, home FROM matches
            WHERE (home=? OR away=?) AND is_valid=1 AND wdl_result!=''
              AND match_date >= date('now', '-' || ? || ' days')
            ORDER BY match_date DESC LIMIT ?
        """, (team_name, team_name, max_days, last_n)).fetchall()
        wins = 0
        for r in rows:
            is_home = (r[1] == team_name)
            if (is_home and r[0] == "3") or (not is_home and r[0] == "0"):
                wins += 1
        total = len(rows)
        if total == 0:
            return None
        return round(wins / total * 100)
    except Exception:
        return None


def get_team_form_analysis(conn, team_name, scope="home", last_n=10, max_days=365):
    """分析球队近况：返回胜率及可靠性判断。

    scope="home": 主队 → 主场场次≥总场次一半则用主场胜率，否则总胜率
    scope="away": 客队 → 客场场次≥总场次一半则用客场胜率，否则总胜率
    """
    if not team_name:
        return {"rate": None, "reliable": False, "source": "none"}
    try:
        rows = conn.execute("""
            SELECT wdl_result, home FROM matches
            WHERE (home=? OR away=?) AND is_valid=1 AND wdl_result!=''
              AND match_date >= date('now', '-' || ? || ' days')
            ORDER BY match_date DESC LIMIT ?
        """, (team_name, team_name, max_days, last_n)).fetchall()
        total = len(rows)
        if total == 0:
            return {"rate": None, "reliable": False, "source": "none"}

        home_games = sum(1 for r in rows if r[1] == team_name)
        away_games = total - home_games
        home_wins = sum(1 for r in rows if r[1] == team_name and r[0] == "3")
        away_wins = sum(1 for r in rows if r[1] != team_name and r[0] == "0")
        total_wins = home_wins + away_wins

        overall_rate = round(total_wins / total * 100) if total > 0 else None
        home_rate = round(home_wins / home_games * 100) if home_games > 0 else None
        away_rate = round(away_wins / away_games * 100) if away_games > 0 else None

        half = total / 2
        if scope == "home" and home_games >= half:
            return {"rate": home_rate, "reliable": True, "source": "home",
                    "home_games": home_games, "away_games": away_games, "total": total}
        elif scope == "away" and away_games >= half:
            return {"rate": away_rate, "reliable": True, "source": "away",
                    "home_games": home_games, "away_games": away_games, "total": total}
        else:
            return {"rate": overall_rate, "reliable": True, "source": "overall",
                    "home_games": home_games, "away_games": away_games, "total": total}
    except Exception:
        return {"rate": None, "reliable": False, "source": "none"}


def analyze_upset_tendency(conn, home, away, league, current_odds):
    """分析主客队爆冷倾向：大热必死 + 以弱胜强

    从 team_patterns 中提取主客队历史表现，与当前赔率对比，
    识别出"看似很强但爱输球"和"看似很弱但总能赢"的球队。

    Args:
        conn: 数据库连接
        home: 主队名
        away: 客队名
        league: 联赛名
        current_odds: dict {result_key: odds}, 当前 WDL 赔率, 如 {'3':1.5,'1':3.5,'0':6.0}

    Returns:
        dict: {adjust_away_win: float, adjust_home_win: float, notes: [str]}
    """
    home_odds = current_odds.get("3", 0)  # 主胜赔率
    away_odds = current_odds.get("0", 0)  # 客胜赔率

    result = {
        "adjust_away_win": 0,   # 客胜命中率调整量（>0 表示调高客胜）
        "adjust_home_win": 0,   # 主胜命中率调整量（>0 表示调高主胜）
        "notes": [],
    }

    # ========== 1. 主队"大热必死"检测 ==========
    # 条件：当前主胜赔率低（被看好），但主队历史主场/总体胜率低
    if home_odds > 0 and home_odds < 2.0:
        hh = query_team_patterns(conn, "home", home, "wdl", league=league)
        ho = query_team_patterns(conn, "overall", home, "wdl")
        hh_idx = {d["result_key"]: d for d in hh}
        ho_idx = {d["result_key"]: d for d in ho}

        hh3 = hh_idx.get("3")
        hh0 = hh_idx.get("0")
        ho3 = ho_idx.get("3")

        if hh3 and hh0 and hh3.get("sample_count", 0) >= 15:
            # 主场胜率<40% 且 主场输球率>25% = 主场虫
            home_weak = hh3["hit_rate"] < 0.40 and hh0["hit_rate"] > 0.25
            # 整体也印证：总体胜率<45%
            overall_weak = ho3 and ho3.get("sample_count", 0) >= 20 and ho3["hit_rate"] < 0.45

            if home_weak and overall_weak:
                # 客胜保底提升：按主队历史输球率推算
                boost = min(0.12, max(0.05, hh0["hit_rate"] * 0.25))
                result["adjust_away_win"] = boost
                result["notes"].append(
                    f"主队{home}[大热必死] 胜率{hh3['hit_rate']:.0%}但赔率{home_odds:.2f}, "
                    f"历史输球率{hh0['hit_rate']:.0%} → 客胜+{boost:.0%}"
                )

    # ========== 2. 客队"以弱胜强"检测 ==========
    # 条件：当前客胜赔率高（不被看好），但客队历史客场胜率不低
    if away_odds > 0 and away_odds > 2.5:
        aa = query_team_patterns(conn, "away", away, "wdl", league=league)
        ao = query_team_patterns(conn, "overall", away, "wdl")
        aa_idx = {d["result_key"]: d for d in aa}
        ao_idx = {d["result_key"]: d for d in ao}

        aa3 = aa_idx.get("3")
        ao3 = ao_idx.get("3")

        if aa3 and aa3.get("sample_count", 0) >= 15:
            # 历史客场胜率>28% (对于弱定位来说异常高)
            away_strong = aa3["hit_rate"] > 0.28
            # 且历史客场赢球时赔率均值>2.5 = 一直是弱定位
            always_underdog = aa3["avg_odds"] > 2.5
            # 整体也印证
            overall_strong = ao3 and ao3.get("sample_count", 0) >= 20 and ao3["hit_rate"] > 0.30

            if away_strong and always_underdog and overall_strong:
                boost = min(0.12, max(0.05, (aa3["hit_rate"] - 0.22) * 0.4))
                result["adjust_away_win"] = max(result["adjust_away_win"], boost)
                result["notes"].append(
                    f"客队{away}[以弱胜强] 客场胜率{aa3['hit_rate']:.0%}(赔率均值{aa3['avg_odds']:.2f}), "
                    f"当前赔率{away_odds:.2f} → 客胜+{boost:.0%}"
                )

    # ========== 3. 主队"以弱胜强"检测（主队升班马/黑马） ==========
    # 条件：当前主胜赔率不算太低（不被特别看好），但主场胜率不低
    if home_odds > 0 and home_odds > 2.0:
        hh = query_team_patterns(conn, "home", home, "wdl", league=league)
        hh_idx = {d["result_key"]: d for d in hh}
        hh3 = hh_idx.get("3")
        if hh3 and hh3.get("sample_count", 0) >= 15:
            if hh3["hit_rate"] > 0.35 and hh3["avg_odds"] > 2.0:
                boost = min(0.10, max(0.05, (hh3["hit_rate"] - 0.28) * 0.35))
                result["adjust_home_win"] = boost
                result["notes"].append(
                    f"主队{home}[主场黑马] 主场胜率{hh3['hit_rate']:.0%}(赔率均值{hh3['avg_odds']:.2f}), "
                    f"当前赔率{home_odds:.2f} → 主胜+{boost:.0%}"
                )

    return result


def process_match(conn, match_data):
    """处理单场比赛，生成推荐，并返回本场最稳候选用于串关"""
    league = match_data.get("league", "")
    home = match_data.get("home", "")
    away = match_data.get("away", "")
    handicap = match_data.get("handicap", "")
    bet_odds = match_data.get("bet_odds", {})

    # === 抓取盘路数据，分析球队近况 ===
    home_form = away_form = None
    hw = aw = 0
    home_id = away_id = None
    sid = match_data.get("sid")
    if sid:
        fetch_and_store_panlu(conn, sid)
        # 从盘路数据取出现频率最高的两支球队（即本场比赛的两队）
        freq_ids = conn.execute("""
            SELECT tid, COUNT(*) as cnt FROM (
                SELECT home_team_id AS tid FROM team_panlu
                UNION ALL
                SELECT away_team_id FROM team_panlu
            ) WHERE tid IS NOT NULL AND tid != 0
            GROUP BY tid ORDER BY cnt DESC LIMIT 2
        """).fetchall()
        if len(freq_ids) >= 2:
            home_id, away_id = freq_ids[0]["tid"], freq_ids[1]["tid"]
            home_form = get_team_form(conn, home_id)
            away_form = get_team_form(conn, away_id)
            print(f"  📊 {home}近8场: {home_form['form']} ({home_form['wins']}胜{home_form['draws']}平{home_form['losses']}负 进{home_form['goals_for']}失{home_form['goals_against']})")
            print(f"  📊 {away}近8场: {away_form['form']} ({away_form['wins']}胜{away_form['draws']}平{away_form['losses']}负 进{away_form['goals_for']}失{away_form['goals_against']})")
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

    # === 根据球队近况调整命中率（放大历史数据权重） ===
    if home_form and away_form and "wdl" in all_results:
        hw, aw = home_form["wins"], away_form["wins"]
        hl, al = home_form["losses"], away_form["losses"]
        h_gd = home_form["goals_for"] - home_form["goals_against"]
        a_gd = away_form["goals_for"] - away_form["goals_against"]
        for r in all_results["wdl"].get("results", []):
            rk = r["result_key"]
            if rk == "3":  # 主胜
                adj = (hw - aw) * 0.05 + (h_gd - a_gd) * 0.02
                r["hit_rate"] = min(1.0, max(0.05, r["hit_rate"] + adj))
            elif rk == "0":  # 客胜
                adj = (aw - hw) * 0.05 + (a_gd - h_gd) * 0.02
                r["hit_rate"] = min(1.0, max(0.05, r["hit_rate"] + adj))
        # 重新排序
        wdl_results = all_results["wdl"]["results"]
        if wdl_results:
            old_top = wdl_results[0]["result_key"]
            wdl_results.sort(key=lambda x: x["hit_rate"], reverse=True)
            all_results["wdl"]["results"] = wdl_results
            new_top = wdl_results[0]["result_key"]
            if old_top != new_top:
                print(f"  💡 球队近况调整: 主队{home_form['wins']}胜{home_form['losses']}负 "
                      f"客队{away_form['wins']}胜{away_form['losses']}负 → "
                      f"推荐从{old_top}调整为{wdl_results[0]['result_key']}")
            # 更新 all_best（WDL 顺序可能变化）
            if old_top != new_top:
                all_best = [(bt, name, all_results.get(bt, {}).get("results", [None])[0])
                           for bt, name, _ in all_best]

    # === 爆冷倾向检测：大热必死 / 以弱胜强 ===
    if "wdl" in all_results and all_results["wdl"].get("results"):
        wdl_odds_from_input = bet_odds.get("wdl", {})
        if wdl_odds_from_input:
            upset = analyze_upset_tendency(conn, home, away, league, wdl_odds_from_input)
            if upset["adjust_away_win"] > 0 or upset["adjust_home_win"] > 0:
                wdl_results = all_results["wdl"]["results"]
                old_top = wdl_results[0]["result_key"]
                for r in wdl_results:
                    if r["result_key"] == "0":  # 客胜
                        r["hit_rate"] = min(1.0, r["hit_rate"] + upset["adjust_away_win"])
                    elif r["result_key"] == "3":  # 主胜
                        r["hit_rate"] = min(1.0, r["hit_rate"] + upset["adjust_home_win"])
                wdl_results.sort(key=lambda x: x["hit_rate"], reverse=True)
                all_results["wdl"]["results"] = wdl_results
                new_top = wdl_results[0]["result_key"]
                for note in upset["notes"]:
                    print(f"  ⚡ {note}")
                # 无论 top pick 是否变化，都更新 all_best 确保数据一致性
                all_best = [(bt, name, all_results.get(bt, {}).get("results", [None])[0])
                           for bt, name, _ in all_best]
                if old_top != new_top:
                    print(f"  → 推荐从{old_top}调整为{new_top}")

    # === 跨玩法一致性检查：WDL vs Handicap ===
    adj_handicap, adj_desc, is_inconsistent = check_wdl_handicap_consistency(
        {bt: all_results[bt]["results"] for bt in all_results if bt in ("wdl", "handicap")},
        handicap
    )
    if is_inconsistent and adj_handicap:
        # 仅记录方向不一致提醒，不替换实际推荐
        pass

    # === 加载联赛数据（转化数据）补充分析 ===
    league_data = load_league_data_for_league(league)
    ld_findings = []
    if league_data:
        for bt, input_odds in bet_odds.items():
            bet_info = BET_TYPES[bt]
            for r in all_results[bt].get("results", []):
                rk = r["result_key"]
                ld_hit = get_hit_from_league_data(league_data, home, away, bt, rk)
                if ld_hit is not None:
                    ld_rate, ld_n, ld_src = ld_hit
                    # 联赛数据命中率替换策略桶命中率（更高优先级）
                    # 但如果 LD 样本量够大（>=50），完全信任 LD；否则与策略桶 50/50
                    if ld_n >= 50:
                        r["hit_rate"] = ld_rate
                        r["league_data"] = True
                    elif ld_n >= 10:
                        r["hit_rate"] = 0.5 * r["hit_rate"] + 0.5 * ld_rate
                        r["league_data"] = True
                    ld_findings.append((bt, rk, ld_rate, ld_n, ld_src))
        # 重新排序
        for bt in all_results:
            all_results[bt]["results"].sort(
                key=lambda x: x["hit_rate"] * x["current_odds"], reverse=True
            )
        # 更新 all_best
        all_best = []
        for bt, input_odds in bet_odds.items():
            bet_info = BET_TYPES[bt]
            results = all_results[bt]["results"]
            if results:
                all_best.append((bt, bet_info["name"], results[0]))
                if bt == "handicap":
                    handicap_best = (bt, bet_info["name"], results[0])

    # === 实时命中检查：从竞彩数据查赛果 ===
    match_date = match_data.get("match_date_full", "")
    actual_match = None
    if match_date:
        try:
            dt = datetime.strptime(match_date, "%Y-%m-%d")
            jc_file = os.path.join(DATA_DIR, f"{dt.year}年", f"{dt.month}月", f"{match_date}.json")
            if os.path.exists(jc_file):
                with open(jc_file, "r", encoding="utf-8") as f:
                    day_data = json.load(f)
                # 优先按竞彩编号匹配
                match_num = match_data.get("match_num", "")
                for am in day_data:
                    if match_num and am.get("num", "").endswith(match_num):
                        actual_match = am
                        break
                # 编号匹配不到，模糊队名匹配
                if not actual_match:
                    for am in day_data:
                        if (am.get("league", "") == league
                                and _fuzzy_match(am.get("home", ""), home)
                                and _fuzzy_match(am.get("away", ""), away)):
                            actual_match = am
                            break
        except (ValueError, OSError):
            pass

    if actual_match:
        result = actual_match.get("result", {})
        result_map = {
            "wdl": result.get("win_draw_lose_result", ""),
            "handicap": result.get("handicap_result", ""),
        }
        print(f"\n  【赛后验证】已查找到实际赛果")
        for bt, input_odds in bet_odds.items():
            actual_val = result_map.get(bt, "")
            if not actual_val:
                continue
            info = BET_TYPES.get(bt, {})
            rlk = info.get("result_map") or {}
            desc = rlk.get(actual_val, actual_val)
            print(f"    实际{info.get('name', bt)}: {actual_val}({desc})")
            for r in all_results.get(bt, {}).get("results", []):
                rk = r["result_key"]
                r["checked"] = True
                r["hit"] = (actual_val == rk)

    # === 打印所有玩法 ===
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

    # === 基于胜平负推荐推导比分/进球数/半全场（不依赖让球方向） ===
    derived = {}
    wdl_pick = coherent_wdl or (wdl_results[0] if wdl_results else None)
    if wdl_pick:
        derived = recommend_conditional_derived(
            conn, league, handicap,
            wdl_pick["result_key"], None,
            home=home, away=away
        )

    # === 根据球队大小球倾向调整进球数/比分 ===
    if home_form is not None and derived:
        h_ou = get_team_over_under(conn, home_id)
        a_ou = get_team_over_under(conn, away_id)
        if h_ou["total"] >= 5 and a_ou["total"] >= 5:
            # 综合双方大小球倾向
            avg_over = (h_ou["over_rate"] + a_ou["over_rate"]) / 2
            avg_under = (h_ou["under_rate"] + a_ou["under_rate"]) / 2
            over_bias = avg_over - avg_under  # 正=偏大球，负=偏小球
            print(f"  📊 大小球: 主队大球率{h_ou['over_rate']:.0%} 客队大球率{a_ou['over_rate']:.0%} "
                  f"偏差={over_bias:+.0%}")

            # 调整进球数命中率
            goal_items = derived.get("goal", {}).get("items", [])
            if goal_items:
                for item in goal_items:
                    try:
                        g = int(item["result_key"])
                    except ValueError:
                        continue
                    # 高进球数(4+)受大球偏差正向影响，低进球数(0-2)受负向影响
                    if g >= 4:
                        item["hit_rate"] = min(1.0, item["hit_rate"] * (1 + over_bias))
                    elif g <= 2:
                        item["hit_rate"] = max(0.01, item["hit_rate"] * (1 - over_bias))
                # 重排序
                goal_items.sort(key=lambda x: x["hit_rate"], reverse=True)
                derived["goal"]["items"] = goal_items

            # 调整比分命中率（高比分受大球偏差正向影响）
            score_items = derived.get("score", {}).get("items", [])
            if score_items:
                for item in score_items:
                    sk = item.get("result_key", "")
                    try:
                        parts = sk.split(":")
                        total = int(parts[0]) + int(parts[1])
                    except (ValueError, IndexError):
                        continue
                    if total >= 4:
                        item["hit_rate"] = min(1.0, item["hit_rate"] * (1 + over_bias))
                    elif total <= 1:
                        item["hit_rate"] = max(0.01, item["hit_rate"] * (1 - over_bias))
                score_items.sort(key=lambda x: x["hit_rate"], reverse=True)
                derived["score"]["items"] = score_items

    # === 统一推荐输出 ===
    print(format_unified_recommendation(coherent, derived))

    # 联赛数据参考
    if ld_findings:
        print(f"  【联赛数据参考（{league_data['season']}）】")
        shown_bt = set()
        for bt, rk, ld_rate, ld_n, ld_src in ld_findings:
            bt_name = BET_TYPES.get(bt, {}).get("name", bt)
            if bt_name not in shown_bt:
                print(f"  {bt_name}:")
                shown_bt.add(bt_name)
            print(f"    {rk}: 命中率={ld_rate:.1%} (样本{ld_n}场, {ld_src})")

    # === 球队逐年统计展示 ===
    yearly_insights = get_yearly_stats_insights(conn, home, away, league)
    yi_insights = yearly_insights.get("insights", [])
    if yi_insights:
        print(f"\n  【近一年球队趋势】")
        for ins in yi_insights:
            print(f"  · {ins}")
        # 逐年核心数据一览
        h_stats = yearly_insights.get("home", {})
        a_stats = yearly_insights.get("away", {})
        h_total = h_stats.get("total_matches", 0)
        a_total = a_stats.get("total_matches", 0)
        if h_total >= 5 or a_total >= 5:
            def _short_line(name, s):
                t = s.get("total_matches", 0)
                if t < 3:
                    return ""
                return (f"  {name:>10} 近一年{t}场: "
                        f"胜{s.get('wins',0)}平{s.get('draws',0)}负{s.get('losses',0)} | "
                        f"输弱队{s.get('losses_to_weaker',0)} 被逆转{s.get('comebacks_conceded',0)} | "
                        f"大{s.get('over_25',0)}小{s.get('under_25',0)} 单{s.get('odd_goals',0)}双{s.get('even_goals',0)}")
            hl = _short_line(home, h_stats)
            al = _short_line(away, a_stats)
            if hl:
                print(f"  【近一年球队核心数据】")
                print(hl)
            if al:
                if not hl:
                    print(f"  【近一年球队核心数据】")
                print(al)
            if h_stats.get("total_matches", 0) >= 3 or a_stats.get("total_matches", 0) >= 3:
                # 比分分布 Top3
                for team_name, s in [(home, h_stats), (away, a_stats)]:
                    sd_raw = s.get("score_dist", "{}")
                    try:
                        sd = json.loads(sd_raw) if isinstance(sd_raw, str) else sd_raw
                    except Exception:
                        sd = {}
                    if sd and s.get("total_matches", 0) >= 5:
                        top_scores = sorted(sd.items(), key=lambda x: x[1], reverse=True)[:3]
                        score_str = ", ".join(f"{sc}={cnt}次" for sc, cnt in top_scores)
                        print(f"  {team_name:>10} 常见比分: {score_str}")
                    hf_raw = s.get("half_full_dist", "{}")
                    try:
                        hf = json.loads(hf_raw) if isinstance(hf_raw, str) else hf_raw
                    except Exception:
                        hf = {}
                    if hf and s.get("total_matches", 0) >= 5:
                        top_hf = sorted(hf.items(), key=lambda x: x[1], reverse=True)[:3]
                        hf_str = ", ".join(f"{k}({HALF_FULL_MAP.get(k,k)})={v}次" for k, v in top_hf)
                        print(f"  {'':>10} 常见半全场: {hf_str}")

    # === 单场一致方向2串1 ===
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
            level_label = (
                f"条件: WDL={WDL_MAP.get(coherent_wdl['result_key'],coherent_wdl['result_key'])}"
                f" + 让球={HANDICAP_RESULT_MAP.get(coherent_hc['result_key'],coherent_hc['result_key'])}"
                f" ({data['level']})")
            print(f"\n  【{data['name']}】基于{level_label}")
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
        "handicap": match_data.get("handicap", ""),
        "bet_type": top_bt,
        "bet_name": top_name,
        "pick": top_pick,
        "match_label": match_data.get("match_label", ""),
        "match_date_full": match_data.get("match_date_full", ""),
        "match_num": match_data.get("match_num", ""),
        "bet_odds": bet_odds,  # 原始赔率数据，供回填team时使用
        "same_match_parlays": same_match_parlays,
        "all_wdl_results": wdl_results[:3] if wdl_results else [],
        "all_hc_results": hc_results[:3] if hc_results else [],
        "all_bet_types": {bt: data.get("best_level", "") for bt, data in all_results.items()},
        "derived_bets": derived,  # 比分/进球数/半全场
        "form_score": (hw - aw) if home_form else 0,  # 近况评分，正=主队强，负=客队强
    }

    # 收集所有玩法的最佳推荐
    all_bet_picks = {}
    for bt, input_odds in bet_odds.items():
        bet_info = BET_TYPES[bt]
        results = all_results.get(bt, {}).get("results", [])
        all_bet_picks[bt] = [{
            "result_key": r["result_key"],
            "description": r.get("description", ""),
            "hit_rate": r["hit_rate"],
            "odds": r["current_odds"],
            "confidence": r.get("confidence", ""),
            "confidence_score": r.get("confidence_score", 0),
            "ev": r.get("ev", None),
            "sample": r.get("sample", 0),
            "checked": r.get("checked"),
            "hit": r.get("hit"),
        } for r in results[:1]]
    ret["all_bet_picks"] = all_bet_picks

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
        direct = os.path.join(DATA_DIR, f"{date_full}.json")
        if os.path.exists(direct):
            json_paths.append(direct)
        else:
            # 兼容按 年/月 子目录归档的场景
            candidates = glob.glob(os.path.join(DATA_DIR, "**", f"{date_full}.json"), recursive=True)
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
                        "combo_hit_rate": round(float(item.get("combo_hit_rate", 0) or 0), 2),
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


def run_from_matches(matches_data):
    """直接接收比赛数据列表，批量生成推荐（由 jc_titan007_parser 调用）"""
    if not matches_data:
        print("错误: 没有比赛数据")
        return
    conn = connect_db()
    if not conn:
        return

    print("=" * 70)
    print(f"新球体育投注推荐（胜平负+让球版）（自动生成）")
    print(f"共 {len(matches_data)} 场比赛")
    print("=" * 70)

    # 先回填历史推荐文件的命中标记
    print("\n检查历史推荐命中情况...")
    backfill_hit_status()
    print()

    parlay_candidates = []
    for i, md in enumerate(matches_data, 1):
        print(f"\n{'━' * 70}")
        title = md.get("match_label") or f"第 {i} 场比赛"
        print(f"  {title}")
        print(f"{'━' * 70}")
        best = process_match(conn, md)
        if best:
            best["match_index"] = i
            parlay_candidates.append(best)

    parlay_summary = _build_parlay_summary(parlay_candidates, print_results=True)

    print(f"\n{'━' * 70}")
    print(f"  全部 {len(matches_data)} 场比赛推荐完成")
    print(f"{'━' * 70}")

    save_recommendation_results(parlay_candidates, parlay_summary, conn)

    conn.close()


def _build_parlay_summary(parlay_candidates, print_results=True):
    """串关推荐构建，从 parlay_candidates 选出最佳组合"""
    parlay_summary = []
    if len(parlay_candidates) < 2:
        return parlay_summary

    parlay_candidates.sort(
        key=lambda x: (
            x["pick"].get("confidence_score", 0),
            x["pick"].get("hit_rate", 0),
            abs(x.get("form_score", 0)) * 0.02,  # 近况热度加成
            x["pick"].get("current_odds", 0),
        ),
        reverse=True,
    )

    # WDL 串关：只选有 WDL 赔率的比赛
    wdl_pool = [c for c in parlay_candidates if (c.get("bet_odds") or {}).get("wdl")]
    if print_results:
        print(f"  [WDL池] 总{len(parlay_candidates)}场, 有WDL赔率{len(wdl_pool)}场")
    if len(wdl_pool) < 2:
        wdl_pool = parlay_candidates  # fallback
    leg1, leg2 = pick_best_pair(wdl_pool, pick_field="pick", same_league_penalty=0.20)
    if not leg1 or not leg2:
        leg1, leg2 = wdl_pool[0] if len(wdl_pool) > 0 else (parlay_candidates[0] if parlay_candidates else None), \
                     wdl_pool[1] if len(wdl_pool) > 1 else (parlay_candidates[1] if len(parlay_candidates) > 1 else None)
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

        # 一致性过滤
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
                    pool = [x for x in parlay_candidates if x.get("match_index") not in selected_ids and not is_cross_play_conflict(x) and (x.get("bet_odds") or {}).get("wdl")]
                    if pool:
                        pool.sort(key=lambda x: (x["pick"].get("confidence_score", 0), x["pick"].get("hit_rate", 0), x["pick"].get("current_odds", 0)), reverse=True)
                        rep = pool[0]
                        if leg1 is item: selected_ids.discard(leg1.get("match_index")); leg1 = rep
                        else: selected_ids.discard(leg2.get("match_index")); leg2 = rep
                        selected_ids.add(rep.get("match_index"))
                if replace_hc:
                    pool = [x for x in handicap_candidates if x.get("match_index") not in selected_ids and not is_cross_play_conflict(x)]
                    if pool:
                        pool.sort(key=lambda x: (x["handicap_pick"]["pick"].get("confidence_score", 0), x["handicap_pick"]["pick"].get("hit_rate", 0), x["handicap_pick"]["pick"].get("current_odds", 0)), reverse=True)
                        rep = pool[0]
                        if h1 is item: selected_ids.discard(h1.get("match_index")); h1 = rep
                        else: selected_ids.discard(h2.get("match_index")); h2 = rep
                        selected_ids.add(rep.get("match_index"))

    o1 = leg1["pick"].get("current_odds", 1.0)
    o2 = leg2["pick"].get("current_odds", 1.0)
    combo_odds = o1 * o2
    hit1 = leg1["pick"].get("hit_rate", 0.0)
    hit2 = leg2["pick"].get("hit_rate", 0.0)
    combo_hit = hit1 * hit2

    def _get_hit(match_item, bt, rk):
        picks = match_item.get("all_bet_picks", {}).get(bt, [])
        for p in picks:
            if p.get("result_key") == rk:
                return p.get("hit")
        return None

    l1_hit = _get_hit(leg1, leg1.get("bet_type", ""), leg1["pick"]["result_key"])
    l2_hit = _get_hit(leg2, leg2.get("bet_type", ""), leg2["pick"]["result_key"])

    def _parlay_hit(legs_info):
        legs_hit = [li.get("hit") for li in legs_info]
        if all(h is True for h in legs_hit): return True
        if any(h is False for h in legs_hit): return False
        return None

    if print_results:
        print(f"\n{'━' * 70}")
        print("  【2串1 推荐（最可信两场）】")
        print(f"{'━' * 70}")
        print(f"  1) 第{leg1['match_index']}场 {leg1['league']} {leg1['home']} vs {leg1['away']} | "
              f"{leg1['bet_name']} -> {leg1['pick']['result_key']}({leg1['pick']['description']}) "
              f"命中率={hit1:.1%} 赔率={o1:.2f} 置信度={leg1['pick']['confidence']}")
        print(f"  2) 第{leg2['match_index']}场 {leg2['league']} {leg2['home']} vs {leg2['away']} | "
              f"{leg2['bet_name']} -> {leg2['pick']['result_key']}({leg2['pick']['description']}) "
              f"命中率={hit2:.1%} 赔率={o2:.2f} 置信度={leg2['pick']['confidence']}")
        print(f"  组合赔率(近似): {combo_odds:.2f}")
        print(f"  组合命中率(独立近似): {combo_hit:.1%}")

    # 只有两场都开了胜平负才生成"胜平负"串子
    if leg1.get("bet_type") == "wdl" and leg2.get("bet_type") == "wdl":
        rk1 = display_wdl_rk(leg1["pick"]["result_key"], leg1.get("bet_type", ""))
        rk2 = display_wdl_rk(leg2["pick"]["result_key"], leg2.get("bet_type", ""))
        wdl_parlay = {
            "type": "胜平负",
            "combo_odds": round(combo_odds, 2),
            "combo_hit_rate": round(combo_hit, 2),
            "hit": _parlay_hit([{"hit": l1_hit}, {"hit": l2_hit}]),
            "legs": [
                {"label": leg1.get("match_label", ""),
                 "result_key": rk1, "hit": l1_hit,
                 "team": f"{leg1.get('home', '')}-{leg1.get('away', '')}",
                 "result": leg1.get("result", None)},
                {"label": leg2.get("match_label", ""),
                 "result_key": rk2, "hit": l2_hit,
                 "team": f"{leg2.get('home', '')}-{leg2.get('away', '')}",
                 "result": leg2.get("result", None)},
            ],
        }
        bet_o1 = (leg1.get("bet_odds") or {}).get("wdl", {})
        bet_o2 = (leg2.get("bet_odds") or {}).get("wdl", {})
        rev_str = build_reverse_parlay("胜平负", rk1, rk2, bet_o1, bet_o2, l1_hit, l2_hit)
        wdl_parlay["reverse"] = rev_str
        parlay_summary.append(wdl_parlay)

    if h1 and h2:
        p1 = h1["handicap_pick"]["pick"]
        p2 = h2["handicap_pick"]["pick"]
        ho1 = p1.get("current_odds", 1.0)
        ho2 = p2.get("current_odds", 1.0)
        h1_hit = _get_hit(h1, "handicap", p1.get("result_key", ""))
        h2_hit = _get_hit(h2, "handicap", p2.get("result_key", ""))
        if print_results:
            print(f"  ---")
            print(f"  3) 第{h1['match_index']}场 {h1['league']} {h1['home']} vs {h1['away']} | 让球 -> {p1['result_key']}({p1['description']})")
            print(f"  4) 第{h2['match_index']}场 {h2['league']} {h2['home']} vs {h2['away']} | 让球 -> {p2['result_key']}({p2['description']})")
            print(f"  让球组合赔率: {ho1*ho2:.2f}")
        hc_rk1 = display_wdl_rk(p1["result_key"], "handicap")
        hc_rk2 = display_wdl_rk(p2["result_key"], "handicap")
        hc_parlay = {
            "type": "让球",
            "combo_odds": round(ho1 * ho2, 2),
            "combo_hit_rate": round(p1.get("hit_rate", 0) * p2.get("hit_rate", 0), 2),
            "hit": _parlay_hit([{"hit": h1_hit}, {"hit": h2_hit}]),
            "legs": [
                {"label": h1.get("match_label", ""),
                 "result_key": hc_rk1, "hit": h1_hit,
                 "team": f"{h1.get('home', '')}-{h1.get('away', '')}",
                 "handicap": h1.get("handicap", ""),
                 "result": h1.get("result", None)},
                {"label": h2.get("match_label", ""),
                 "result_key": hc_rk2, "hit": h2_hit,
                 "team": f"{h2.get('home', '')}-{h2.get('away', '')}",
                 "handicap": h2.get("handicap", ""),
                 "result": h2.get("result", None)},
            ],
        }
        bet_o1 = (h1.get("bet_odds") or {}).get("handicap", {})
        bet_o2 = (h2.get("bet_odds") or {}).get("handicap", {})
        rev_str = build_reverse_parlay("让球", hc_rk1, hc_rk2, bet_o1, bet_o2, h1_hit, h2_hit)
        hc_parlay["reverse"] = rev_str
        parlay_summary.append(hc_parlay)

    return parlay_summary


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
    print(f"新球体育投注推荐（胜平负+让球版）（从文件读取: {os.path.basename(filepath)}）")
    print(f"共 {len(matches)} 场比赛")
    print("=" * 70)

    # 先回填历史推荐文件的命中标记
    print("\n检查历史推荐命中情况...")
    backfill_hit_status()
    print()

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
                abs(x.get("form_score", 0)) * 0.02,
                x["pick"].get("current_odds", 0),
            ),
            reverse=True,
        )

        # WDL 串关：只选有 WDL 赔率的比赛
        wdl_pool = [c for c in parlay_candidates if (c.get("bet_odds") or {}).get("wdl")]
        if len(wdl_pool) < 2:
            wdl_pool = parlay_candidates
        leg1, leg2 = pick_best_pair(wdl_pool, pick_field="pick", same_league_penalty=0.20)
        if not leg1 or not leg2:
            leg1, leg2 = wdl_pool[0] if len(wdl_pool) > 0 else (parlay_candidates[0] if parlay_candidates else None), \
                         wdl_pool[1] if len(wdl_pool) > 1 else (parlay_candidates[1] if len(parlay_candidates) > 1 else None)

        handicap_candidates = [x for x in parlay_candidates if x.get("handicap_pick")]
        h1 = h2 = None
        if len(handicap_candidates) >= 2:
            handicap_candidates.sort(
                key=lambda x: (
                    x["handicap_pick"]["pick"].get("confidence_score", 0),
                    x["handicap_pick"]["pick"].get("hit_rate", 0),
                    abs(x.get("form_score", 0)) * 0.02,
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
                        pool = [x for x in parlay_candidates if x.get("match_index") not in selected_ids and not is_cross_play_conflict(x) and (x.get("bet_odds") or {}).get("wdl")]
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

        def _get_hit(match_item, bt, rk):
            """从候选比赛数据中查找指定玩法+结果的命中标记"""
            picks = match_item.get("all_bet_picks", {}).get(bt, [])
            for p in picks:
                if p.get("result_key") == rk:
                    return p.get("hit")
            return None

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

        def _parlay_hit(legs_info):
            """计算串关的总体命中：全部命中=true，任一未命=false，未知=null"""
            legs_hit = [li.get("hit") for li in legs_info]
            if all(h is True for h in legs_hit):
                return True
            if any(h is False for h in legs_hit):
                return False
            return None

        l1_hit = _get_hit(leg1, leg1.get("bet_type", ""), leg1["pick"]["result_key"])
        l2_hit = _get_hit(leg2, leg2.get("bet_type", ""), leg2["pick"]["result_key"])

        # 只有两场都开了胜平负才生成"胜平负"串子
        if leg1.get("bet_type") == "wdl" and leg2.get("bet_type") == "wdl":
            rk1_raw = display_wdl_rk(leg1["pick"]["result_key"], leg1.get("bet_type", ""))
            rk2_raw = display_wdl_rk(leg2["pick"]["result_key"], leg2.get("bet_type", ""))
            # 翻转WDL方向：胜↔负，平不动
            _wdl_rev = {"胜": "负", "负": "胜"}
            rk1 = _wdl_rev.get(rk1_raw, rk1_raw)
            rk2 = _wdl_rev.get(rk2_raw, rk2_raw)
            wdl_parlay = {
                "type": "胜平负",
                "combo_odds": round(combo_odds, 2),
                "combo_hit_rate": round(combo_hit, 2),
                "hit": _parlay_hit([{"hit": l1_hit}, {"hit": l2_hit}]),
                "legs": [
                    {"label": leg1.get("match_label", ""),
                     "result_key": rk1, "hit": l1_hit,
                     "team": f"{leg1.get('home', '')}-{leg1.get('away', '')}",
                     "result": leg1.get("result", None)},
                    {"label": leg2.get("match_label", ""),
                     "result_key": rk2, "hit": l2_hit,
                     "team": f"{leg2.get('home', '')}-{leg2.get('away', '')}",
                     "result": leg2.get("result", None)},
                ],
            }
            bet_o1 = (leg1.get("bet_odds") or {}).get("wdl", {})
            bet_o2 = (leg2.get("bet_odds") or {}).get("wdl", {})
            rev_str = build_reverse_parlay("胜平负", rk1, rk2, bet_o1, bet_o2, l1_hit, l2_hit)
            wdl_parlay["reverse"] = rev_str
            parlay_summary.append(wdl_parlay)


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
            h1_hit = _get_hit(h1, "handicap", p1["result_key"])
            h2_hit = _get_hit(h2, "handicap", p2["result_key"])
            hc_rk1 = display_wdl_rk(p1["result_key"], "handicap")
            hc_rk2 = display_wdl_rk(p2["result_key"], "handicap")
            # 让球串关同样翻转方向
            _wdl_rev = {"胜": "负", "负": "胜"}
            hc_rk1 = _wdl_rev.get(hc_rk1, hc_rk1)
            hc_rk2 = _wdl_rev.get(hc_rk2, hc_rk2)
            hc_parlay = {
                "type": "让球",
                "combo_odds": round(hcombo_odds, 2),
                "combo_hit_rate": round(hcombo_hit, 2),
                "hit": _parlay_hit([{"hit": h1_hit}, {"hit": h2_hit}]),
                "legs": [
                    {
                    "label": h1.get("match_label", ""),
                    "result_key": hc_rk1, "hit": h1_hit,
                    "team": f"{h1.get('home', '')}-{h1.get('away', '')}",
                    "handicap": h1.get("handicap", ""),
                    "result": h1.get("result", None),
                },
                {
                    "label": h2.get("match_label", ""),
                    "result_key": hc_rk2, "hit": h2_hit,
                    "team": f"{h2.get('home', '')}-{h2.get('away', '')}",
                    "handicap": h2.get("handicap", ""),
                    "result": h2.get("result", None),
                },
                ],
            }
            bet_o1 = (h1.get("bet_odds") or {}).get("handicap", {})
            bet_o2 = (h2.get("bet_odds") or {}).get("handicap", {})
            rev_str = build_reverse_parlay("让球", hc_rk1, hc_rk2, bet_o1, bet_o2, h1_hit, h2_hit)
            hc_parlay["reverse"] = rev_str
            parlay_summary.append(hc_parlay)



        hparlay_summary = {"type": "让球", "combo_odds": round(hcombo_odds, 2), "combo_hit_rate": round(hcombo_hit, 2)}
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

    # 保存推荐结果到文件
    save_recommendation_results(parlay_candidates, parlay_summary, conn)

    conn.close()

def format_match_result(result):
    """将竞彩数据的result格式化为"全3:1--半0:1--胜--让胜--4--双--负胜"格式"""
    if not result:
        return None
    full = result.get("full", "")
    half = result.get("half", "")
    wdl = result.get("win_draw_lose_result", "")
    handicap_r = result.get("handicap_result", "")
    goal = result.get("goal_result", "")
    half_full = result.get("half_full_result", "")

    wdl_map = {"3": "胜", "1": "平", "0": "负"}
    hc_map = {"3": "让胜", "1": "让平", "0": "让负"}
    hf_map = {
        "3-3": "胜胜", "3-1": "胜平", "3-0": "胜负",
        "1-3": "平胜", "1-1": "平平", "1-0": "平负",
        "0-3": "负胜", "0-1": "负平", "0-0": "负负",
    }

    parts = [
        f"全{full.replace('-', ':')}",
        f"半{half.replace('-', ':')}",
        wdl_map.get(wdl, wdl),
        hc_map.get(handicap_r, handicap_r) if handicap_r else "",
    ]
    # 进球数
    parts.append(goal)
    # 单/双
    try:
        total = int(goal)
        parts.append("双" if total % 2 == 0 else "单")
    except (ValueError, TypeError):
        parts.append("")
    # 半全场
    parts.append(hf_map.get(half_full, half_full) if half_full else "")

    # 过滤空部分，用 -- 连接
    return "--".join(p for p in parts if p)


def backfill_hit_status():
    """扫描推荐结果文件，为未标记命中的比赛补充命中标记（按比赛编号 match_num 匹配）"""
    if not os.path.isdir(RESULT_DIR):
        return
    today_str = datetime.now().strftime("%Y-%m-%d")
    result_files = sorted([f for f in os.listdir(RESULT_DIR) if f.endswith(".json")])

    for fname in result_files:
        fp = os.path.join(RESULT_DIR, fname)
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        matches = data.get("matches", [])
        if not matches:
            continue
        # 检查是否所有命中标记都已填充
        matches_all_done = all(
            m.get("checked") is not None and m.get("result") is not None
            for m in matches
        )
        parlays = data.get("parlays", [])
        parlays_all_done = all(sp.get("hit") is not None for sp in parlays) if parlays else True
        if matches_all_done and parlays_all_done:
            continue

        # 从文件名取日期
        date_str = fname.replace(".json", "")
        for prefix in ("正-", "反-"):
            if date_str.startswith(prefix):
                date_str = date_str[len(prefix):]
                break
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            jc_path = os.path.join(DATA_DIR, f"{dt.year}年", f"{dt.month}月", f"{date_str}.json")
        except ValueError:
            jc_path = ""

        # 加载竞彩数据，按 match_num 建立索引（提取数字部分，如 "周日204" → "204"）
        actual_by_num = {}
        if jc_path and os.path.exists(jc_path):
            try:
                with open(jc_path, "r", encoding="utf-8") as f:
                    for am in json.load(f):
                        num = am.get("num", "")
                        digits = "".join(ch for ch in num if ch.isdigit())
                        if digits:
                            actual_by_num[digits] = am
            except Exception:
                pass

        # 反查映射表
        REV_WDL = {"胜": "3", "平": "1", "负": "0"}
        REV_HC = {"让胜": "3", "让平": "1", "让负": "0"}
        REV_HF = {v: k for k, v in SHORT_HF_MAP.items()}
        REV_GOAL = {v: k for k, v in GOAL_MAP.items()}

        def _parse_segment(seg):
            """解析单个 result_key segment，返回 (bet_type, internal_rk) 或 None"""
            seg = seg.strip()
            if not seg:
                return None
            if seg.startswith("["):
                scores = seg.strip("[]").split()
                return ("score", scores[0] if scores else "")
            plain = seg.split("(")[0] if "(" in seg else seg
            if plain in REV_HC:
                return ("handicap", REV_HC[plain])
            if plain in REV_GOAL:
                return ("goal", REV_GOAL[plain])
            if plain in REV_HF:
                return ("half_full", REV_HF[plain])
            if plain in REV_WDL:
                return ("wdl", REV_WDL[plain])
            return None

        def format_match_result(result):
            """格式化赛果为 "全:半" 格式"""
            full = result.get("full", "")
            half = result.get("half", "")
            if full or half:
                return f"{full}({half})"
            return None

        changed = False
        for m in matches:
            if m.get("checked") is not None and m.get("result") is not None:
                continue

            # 按比赛编号匹配
            mn = str(m.get("match_num", ""))
            found = actual_by_num.get(mn)

            if found:
                result = found.get("result", {})
                result_map = {
                    "wdl": result.get("win_draw_lose_result", ""),
                    "handicap": result.get("handicap_result", ""),
                    "score": result.get("score_result", ""),
                    "goal": result.get("goal_result", ""),
                    "half_full": result.get("half_full_result", ""),
                }

                # 解析并比对
                old_rk = m.get("result_key", "")
                rk_segments = [s.strip() for s in old_rk.split("--")]
                updated_hits = {}
                for seg in rk_segments:
                    p = _parse_segment(seg)
                    if p:
                        bt, rk_val = p
                        actual_val = result_map.get(bt, "")
                        hit_val = actual_val == rk_val if actual_val else None
                        updated_hits[bt] = hit_val

                if m.get("checked") is None:
                    wdl_hit = updated_hits.get("wdl")
                    hc_hit = updated_hits.get("handicap")
                    if wdl_hit is not None or hc_hit is not None:
                        m["checked"] = True

                m["result"] = format_match_result(result)

                # 重建 result_key
                def _hit_suffix(hv):
                    return f"({1 if hv is True else 0})" if hv is not None else ""

                new_parts = []
                for seg in rk_segments:
                    seg = seg.strip()
                    if not seg:
                        continue
                    plain = seg.split("(")[0] if "(" in seg else seg
                    if seg.startswith("["):
                        hv = updated_hits.get("score")
                        new_parts.append(f"{seg.split(']')[0]}]{_hit_suffix(hv)}")
                    else:
                        p = _parse_segment(seg)
                        if p:
                            hv = updated_hits.get(p[0])
                            new_parts.append(f"{plain}{_hit_suffix(hv)}")
                        else:
                            new_parts.append(seg)
                m["result_key"] = "--".join(new_parts)
            else:
                try:
                    match_date = datetime.strptime(date_str, "%Y-%m-%d")
                    if match_date.date() < datetime.now().date():
                        m["checked"] = True
                        m["result"] = "无效"
                    else:
                        m["checked"] = None
                        m["result"] = None
                except ValueError:
                    m["checked"] = None
                    m["result"] = None
            changed = True

        # ===== 回填串关（parlays） =====
        parlays = data.get("parlays", [])
        if parlays:
            parlay_changed = False
            for sp in parlays:
                legs = sp.get("legs", [])
                if not legs:
                    continue
                all_done = all(
                    leg.get("hit") is not None and leg.get("result") is not None
                    for leg in legs
                )
                if all_done:
                    continue
                leg_hits = []
                for leg in legs:
                    if leg.get("hit") is not None and leg.get("result") is not None:
                        leg_hits.append(leg["hit"])
                        continue
                    label = leg.get("label", "")
                    mn = "".join(ch for ch in label if ch.isdigit())
                    found_am = actual_by_num.get(mn)
                    if found_am:
                        r = found_am.get("result", {})
                        bt = "handicap" if sp.get("type") == "让球" else "wdl"
                        rk = leg.get("result_key", "")
                        rk_val = REV_WDL.get(rk, "")
                        actual_val = r.get("handicap_result" if bt == "handicap" else "win_draw_lose_result", "")
                        hit = (actual_val == rk_val) if actual_val else None
                        leg["hit"] = hit
                        leg["result"] = r.get("full", "")
                        if r.get("handicap") and sp.get("type") == "让球":
                            leg["handicap"] = r.get("handicap", "")
                        leg_hits.append(hit)
                    else:
                        try:
                            match_date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                            if match_date_obj.date() < datetime.now().date():
                                if leg.get("result") is None:
                                    leg["result"] = "无效"
                                parlay_changed = True
                        except ValueError:
                            pass
                        leg_hits.append(None)
                if leg_hits and any(h is not None for h in leg_hits):
                    if all(h is True for h in leg_hits):
                        sp["hit"] = True
                    elif any(h is False for h in leg_hits):
                        sp["hit"] = False
                    else:
                        sp["hit"] = None
                    parlay_changed = True
            if parlay_changed:
                changed = True

        if changed:
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            hit_count = 0
            miss_count = 0
            for m in matches:
                rk = m.get("result_key", "")
                if rk and "(" in rk:
                    first_seg = rk.split("--")[0].strip()
                    if first_seg.endswith("(1)"):
                        hit_count += 1
                    elif first_seg.endswith("(0)"):
                        miss_count += 1
            verified_count = sum(1 for m in matches if m.get("checked") is True)
            print(f"  ✓ 命中回填: {os.path.basename(fp)} 已校验{verified_count}场 命中{hit_count} 未命中{miss_count}")

    print(f"  推荐结果命中检查完成 ({len(result_files)} 个文件)")


def build_simplified_result_key(pc):
    """构建 result_key 字符串，合并全部5种玩法推荐，如'胜(1)--让平(0)--3球(1)--胜负(0)--[2:0 3:0 4:0](0)'"""
    parts = []

    # WDL
    pick = pc.get("pick", {})
    wdl_rk = display_wdl_rk(pick.get("result_key", ""), "wdl")
    wdl_hit = pick.get("hit")
    if wdl_rk:
        hs = 1 if wdl_hit is True else 0 if wdl_hit is False else None
        suffix = f"({hs})" if hs is not None else ""
        parts.append(f"{wdl_rk}{suffix}")

    # Handicap
    hc_pick = (pc.get("handicap_pick") or {}).get("pick", {})
    hc_rk = display_wdl_rk(hc_pick.get("result_key", ""), "handicap")
    hc_hit = hc_pick.get("hit")
    if hc_rk:
        hs = 1 if hc_hit is True else 0 if hc_hit is False else None
        suffix = f"({hs})" if hs is not None else ""
        parts.append(f"让{hc_rk}{suffix}")

    # Goal（进球数）- 取 derived_bets 兜底
    goal_picks = pc.get("all_bet_picks", {}).get("goal", [])
    goal_items = (pc.get("derived_bets") or {}).get("goal", {}).get("items", [])
    if goal_picks:
        gp = goal_picks[0]
        goal_rk = gp.get("result_key", "")
        goal_hit = gp.get("hit")
    elif goal_items:
        gp = goal_items[0]
        goal_rk = gp.get("result_key", "")
        goal_hit = None
    else:
        goal_rk = None
    if goal_rk:
        hs = 1 if goal_hit is True else 0 if goal_hit is False else None
        suffix = f"({hs})" if hs is not None else ""
        goal_desc = GOAL_MAP.get(goal_rk, f"{goal_rk}球") if goal_rk else ""
        if goal_desc:
            parts.append(f"{goal_desc}{suffix}")

    # Half-full（半全场）- 取 derived_bets 兜底
    hf_picks = pc.get("all_bet_picks", {}).get("half_full", [])
    hf_items = (pc.get("derived_bets") or {}).get("half_full", {}).get("items", [])
    if hf_picks:
        hp = hf_picks[0]
        hf_rk = hp.get("result_key", "")
        hf_hit = hp.get("hit")
    elif hf_items:
        hp = hf_items[0]
        hf_rk = hp.get("result_key", "")
        hf_hit = None
    else:
        hf_rk = None
    if hf_rk:
        hs = 1 if hf_hit is True else 0 if hf_hit is False else None
        suffix = f"({hs})" if hs is not None else ""
        hf_desc = SHORT_HF_MAP.get(hf_rk, hf_rk)
        if hf_desc:
            parts.append(f"{hf_desc}{suffix}")

    # Score（比分）- 取 derived_bets 中的 Top3
    score_items = (pc.get("derived_bets") or {}).get("score", {}).get("items", [])[:3]
    score_picks = pc.get("all_bet_picks", {}).get("score", [])
    if score_items or score_picks:
        scores = [s.get("result_key", "") for s in score_items if s.get("result_key")]
        if not scores and score_picks:
            scores = [score_picks[0].get("result_key", "")]
        if scores:
            score_hit = score_picks[0].get("hit") if score_picks else None
            hs = 1 if score_hit is True else 0 if score_hit is False else None
            suffix = f"({hs})" if hs is not None else ""
            parts.append(f"[{' '.join(scores)}]{suffix}")

    return "--".join(parts)


def reverse_wdl_result_key(rk_str):
    """
    翻转 WDL 方向：胜↔负，半全场反向，比分主客互换。
    仅在第一段为 胜/负 时翻转，平局不动。
    """
    import re as _re

    HF_REVERSE = {
        "胜胜": "负负", "胜负": "负胜", "胜平": "负平",
        "平胜": "平负", "平平": "平平", "平负": "平胜",
        "负胜": "胜负", "负平": "胜平", "负负": "胜胜",
    }
    WDL_REVERSE = {"胜": "负", "负": "胜"}

    def _strip_suffix(t):
        """分离正文和后缀 (1)/(0)"""
        m = _re.search(r'(\([01]\))$', t)
        if m:
            return t[:m.start()], m.group(1)
        return t, ""

    def _reverse_score_block(text):
        """翻转比分块: [1:0 2:1] → [0:1 1:2]"""
        def _rev_score(m):
            parts = m.group(0).split(":")
            return f"{parts[1]}:{parts[0]}" if len(parts) == 2 else m.group(0)

        return _re.sub(r'(\d+):(\d+)', _rev_score, text)

    segs = rk_str.split("--")
    new_segs = []

    # 第一段：WDL
    first = segs[0].strip() if segs else ""
    if first:
        plain, sfx = _strip_suffix(first)
        rev = WDL_REVERSE.get(plain)
        if rev:  # 胜→负 或 负→胜
            new_segs.append(f"{rev}{sfx}")
            # 翻转之后所有段
            for seg in segs[1:]:
                seg = seg.strip()
                if not seg:
                    new_segs.append(seg)
                    continue
                plain, sfx = _strip_suffix(seg)
                # 半全场
                if plain in HF_REVERSE:
                    new_segs.append(f"{HF_REVERSE[plain]}{sfx}")
                # 比分块
                elif plain.startswith("["):
                    new_segs.append(f"{_reverse_score_block(plain)}{sfx}")
                else:
                    new_segs.append(seg)
            return "--".join(new_segs)
        else:
            # 平局，不动
            return rk_str
    return rk_str


def save_recommendation_results(parlay_candidates, parlay_summary, conn=None):
    """将推荐结果保存为 正- 和 反- 两个版本"""
    import copy as _copy

    if not parlay_candidates and not parlay_summary:
        return
    os.makedirs(RESULT_DIR, exist_ok=True)

    # 从比赛数据中取日期
    date_from_matches = None
    for pc in parlay_candidates:
        d = pc.get("match_date_full", "")
        if d:
            date_from_matches = d
            break
    date_str = date_from_matches or datetime.now().strftime("%Y-%m-%d")

    # ─── 构建正反两个版本的 match_results ───
    pos_matches = []  # 正-: 原始推荐
    neg_matches = []  # 反-: 翻转推荐

    for pc in parlay_candidates:
        pick = pc.get("pick", {})

        # 正版直接取原始 result_key
        rk_pos = build_simplified_result_key(pc)
        # 反版翻转 WDL 方向
        rk_neg = reverse_wdl_result_key(rk_pos)

        bet_odds = pc.get("bet_odds", {})
        wdl_odds = bet_odds.get("wdl", {})
        odds_str = "({:.2f} {:.2f} {:.2f})".format(
            wdl_odds.get("3", 0), wdl_odds.get("1", 0), wdl_odds.get("0", 0)
        ) if wdl_odds else ""

        # 球队近10场胜率
        home_win_rate = away_win_rate = None
        if conn:
            home_form = get_team_form_analysis(conn, pc.get("home", ""), "home")
            away_form = get_team_form_analysis(conn, pc.get("away", ""), "away")
            home_win_rate = home_form.get("rate") if home_form.get("reliable") else None
            away_win_rate = away_form.get("rate") if away_form.get("reliable") else None

        home_name = pc.get("home", "")
        away_name = pc.get("away", "")
        home_part = f"{home_name}({home_win_rate}%)" if home_win_rate is not None else home_name
        away_part = f"{away_name}({away_win_rate}%)" if away_win_rate is not None else away_name
        team_str = f"{home_part}-{away_part}{odds_str}"

        base = {
            "match_index": pc.get("match_index", ""),
            "match_num": str(pc.get("match_num", "")),
            "league": pc.get("league", ""),
            "team": team_str,
            "result": None,
            "checked": None,
        }
        pos_matches.append({**base, "result_key": rk_pos})
        neg_matches.append({**base, "result_key": rk_neg})

    # ─── 构建正反两个版本的 parlays ───
    # parlay_summary 当前已是反-版本（已翻转）
    # 正-版本：将 leg result_key 翻回原始方向
    _wdl_rev = {"胜": "负", "负": "胜"}

    def _reverse_parlay(sp):
        """对单个 parlay 做 leg result_key 的翻转"""
        sp_rev = _copy.deepcopy(sp)
        for leg in sp_rev.get("legs", []):
            leg["result_key"] = _wdl_rev.get(leg["result_key"], leg["result_key"])
        return sp_rev

    pos_parlays = [_reverse_parlay(sp) for sp in (parlay_summary or [])]
    neg_parlays = [_copy.deepcopy(sp) for sp in (parlay_summary or [])]

    # 交换 reverse 字段：正版的 reverse = 反版推荐，反版的 reverse = 正版推荐
    for pos_sp, neg_sp in zip(pos_parlays, neg_parlays):
        pos_rev = neg_sp.get("reverse", "")
        neg_rev = pos_sp.get("reverse", "")
        pos_sp["reverse"] = pos_rev
        neg_sp["reverse"] = neg_rev

    # ─── 保存正- 版本 ───
    pos_path = os.path.join(RESULT_DIR, f"正-{date_str}.json")
    pos_result = {
        "generated_at": date_str,
        "total_matches": len(pos_matches),
        "parlays": pos_parlays,
        "matches": pos_matches,
    }
    with open(pos_path, "w", encoding="utf-8") as f:
        json.dump(pos_result, f, ensure_ascii=False, indent=2)
    print(f"  ✅ 正版推荐已保存: {pos_path}")

    # ─── 保存反- 版本 ───
    neg_path = os.path.join(RESULT_DIR, f"反-{date_str}.json")
    neg_result = {
        "generated_at": date_str,
        "total_matches": len(neg_matches),
        "parlays": neg_parlays,
        "matches": neg_matches,
    }
    with open(neg_path, "w", encoding="utf-8") as f:
        json.dump(neg_result, f, ensure_ascii=False, indent=2)
    print(f"  ✅ 反版推荐已保存: {neg_path}")


def interactive():
    """交互式推荐"""
    conn = connect_db()
    if not conn:
        return
    print("=" * 70)
    print("新球体育投注推荐（胜平负+让球版）")
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
        print("新球体育投注推荐（胜平负+让球版）")
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
half_full_3-3=2.50
half_full_1-1=5.00
half_full_0-0=4.80
──────────────────────────────────────
没开的玩法直接不写即可，推荐器会自动跳过。
# 开头的行是注释，会被忽略。
""")
if __name__ == "__main__":
    # ═══════════════════════════════════════════════
    # 第一步：通过 jc_titan007_parser 获取数据并更新模式库
    # ═══════════════════════════════════════════════
    parser_ok = False
    try:
        from jc_titan007_parser import main as parser_main
        parser_main()
        parser_ok = True
    except ImportError:
        print("⚠ 未找到 jc_titan007_parser（无法自动获取竞彩数据）")
    except Exception as e:
        print(f"⚠ 获取数据失败: {e}")

    # 更新模式库（把最新赛果编入 pattern_db）
    print(f"\n{'=' * 60}")
    print("更新模式库")
    print(f"{'=' * 60}")
    try:
        from update_pattern_db import main as update_main
        update_main(force_full=False)
        print("  ✅ 模式库更新完成")
    except Exception as e:
        print(f"  ⚠ 模式库更新失败: {e}")

    # ═══════════════════════════════════════════════
    # 第二步：按比赛编号回填验证已有推荐
    # ═══════════════════════════════════════════════
    print(f"\n{'=' * 60}")
    print("回填验证推荐结果（按比赛编号匹配）")
    print(f"{'=' * 60}")
    backfill_hit_status()

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
        elif arg == "--today":
            pass  # 已由上方自动执行
        else:
            if os.path.exists(arg):
                run_from_file(arg)
            else:
                print(f"错误: 文件不存在 ({arg})")
                print("用法: python3 recommend.py --file input.txt")
    else:
        if not parser_ok:
            print()
            print("推荐器需要传入比赛数据，用法:")
            print("  python3 recommend.py --file <txt文件>     从文本文件读取")
            print("  python3 recommend.py --today              自动拉取今日竞彩数据")
            print()
            print("或者确保 jc_titan007_parser.py 在同目录下")
            print("=" * 70)
