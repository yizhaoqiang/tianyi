#!/usr/bin/env python3
"""
新球体育模式库构建器

扫描所有JSON数据，构建SQLite模式数据库。
包含全部5个玩法：胜平负、让球胜平负、比分、进球数、半全场。

用法：
  python3 update_pattern_db.py           # 增量更新
  python3 update_pattern_db.py --rebuild # 全量重建
"""

import json
import hashlib
import os
import glob
import sqlite3
from collections import defaultdict
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)  # /Users/jojo/Desktop/TianyiSport/code/新球体育
DATA_DIR = os.path.join(BASE_DIR, "竞彩数据")
DB_PATH = os.path.join(SCRIPT_DIR, "pattern_db.sqlite")

# ============ 玩法映射 ============

HALF_FULL_MAP = {
    "3-3": "主胜-主胜", "1-1": "平-平", "0-0": "客胜-客胜",
    "1-3": "平-主胜", "1-0": "平-客胜", "3-1": "主胜-平",
    "3-0": "主胜-客胜", "0-1": "客胜-平", "0-3": "客胜-主胜",
}

WDL_MAP = {"3": "主胜", "1": "平局", "0": "客胜"}

HANDICAP_RESULT_MAP = {"3": "赢盘", "1": "走盘", "0": "输盘"}

GOAL_MAP = {
    "0": "0球", "1": "1球", "2": "2球", "3": "3球",
    "4": "4球", "5": "5球", "6": "6球", "7": "7+球",
}

# ============ 赔率分桶配置（每个玩法独立区间） ============

ODDS_BUCKET_CONFIG = {
    "wdl": [(1.01, 1.3), (1.3, 1.6), (1.6, 2.0), (2.0, 2.5), (2.5, 3.5), (3.5, 5.0), (5.0, 8.0), (8.0, 99)],
    "handicap": [(1.01, 1.4), (1.4, 1.7), (1.7, 2.0), (2.0, 2.5), (2.5, 3.5), (3.5, 5.0), (5.0, 99)],
    "score": [(1.01, 3.0), (3.0, 5.0), (5.0, 8.0), (8.0, 12.0), (12.0, 20.0), (20.0, 99)],
    "goal": [(1.01, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 4.5), (4.5, 7.0), (7.0, 15.0), (15.0, 99)],
    "half_full": [(1.01, 2.0), (2.0, 3.5), (3.5, 5.5), (5.5, 8.0), (8.0, 15.0), (15.0, 99)],
}


def get_odds_bucket(bet_type, odds):
    """根据赔率值返回对应的分桶标签"""
    if not odds or odds <= 0:
        return None
    for lo, hi in ODDS_BUCKET_CONFIG.get(bet_type, []):
        if lo <= odds < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return None


# 各玩法的结果集合（用于推荐脚本）
BET_TYPES = {
    "wdl": {"name": "胜平负", "results": ["3", "1", "0"], "result_map": WDL_MAP},
    "handicap": {"name": "让球胜平负", "results": ["3", "1", "0"], "result_map": HANDICAP_RESULT_MAP},
    "score": {"name": "比分", "results": None, "result_map": None},  # 比分结果不固定，动态收集
    "goal": {"name": "进球数", "results": ["0", "1", "2", "3", "4", "5", "6", "7"], "result_map": GOAL_MAP},
    "half_full": {"name": "半全场", "results": ["3-3", "1-1", "0-0", "1-3", "1-0", "3-1", "3-0", "0-1", "0-3"], "result_map": HALF_FULL_MAP},
}


def safe_float(val, default=None):
    if val is None or val == "" or val == "-":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ============ 数据库 ============

def init_db(db_path):
    """创建数据库表"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    # 原始比赛数据（JSON全部结果字段）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            match_id    TEXT PRIMARY KEY,
            match_date  TEXT NOT NULL,
            league      TEXT NOT NULL,
            home        TEXT NOT NULL,
            away        TEXT NOT NULL,
            handicap    TEXT NOT NULL DEFAULT '',
            half_full_result TEXT NOT NULL DEFAULT '',
            half_full_odds   REAL DEFAULT NULL,
            wdl_result       TEXT NOT NULL DEFAULT '',
            wdl_odds         REAL DEFAULT NULL,
            handicap_result  TEXT NOT NULL DEFAULT '',
            handicap_odds    REAL DEFAULT NULL,
            score_result     TEXT NOT NULL DEFAULT '',
            score_odds       REAL DEFAULT NULL,
            goal_result      TEXT NOT NULL DEFAULT '',
            goal_odds        REAL DEFAULT NULL,
            is_valid         INTEGER NOT NULL DEFAULT 1,
            source_file      TEXT NOT NULL DEFAULT ''
        )
    """)

    # 向后兼容旧库：补充 source_file 和 weight 字段
    cols = {row[1] for row in conn.execute("PRAGMA table_info(matches)")}
    if "source_file" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN source_file TEXT NOT NULL DEFAULT ''")
    if "weight" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN weight REAL NOT NULL DEFAULT 1.0")

    # 检查 patterns 表是否需要迁移（添加 odds_bucket 字段）
    needs_patterns_rebuild = False
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='patterns'").fetchone():
        pcols = {row[1] for row in conn.execute("PRAGMA table_info(patterns)")}
        if "odds_bucket" not in pcols:
            needs_patterns_rebuild = True
            conn.execute("DROP TABLE patterns")

    # 模式统计表（多层级聚合，覆盖全部5个玩法，支持赔率分桶）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS patterns (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            granularity TEXT NOT NULL,
            league      TEXT DEFAULT NULL,
            handicap    TEXT DEFAULT NULL,
            bet_type    TEXT NOT NULL,
            result_key  TEXT NOT NULL,
            odds_bucket TEXT DEFAULT NULL,
            sample_count  INTEGER NOT NULL DEFAULT 0,
            hit_count     INTEGER NOT NULL DEFAULT 0,
            avg_odds      REAL NOT NULL DEFAULT 0,
            min_odds      REAL NOT NULL DEFAULT 0,
            max_odds      REAL NOT NULL DEFAULT 0,
            hit_rate      REAL NOT NULL DEFAULT 0,
            expected_value REAL NOT NULL DEFAULT 0,
            updated_at    TEXT NOT NULL,
            UNIQUE(granularity, league, handicap, bet_type, result_key, odds_bucket)
        )
    """)

    # 球队模式统计（用于球队风格修正）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS team_patterns (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            scope         TEXT NOT NULL,          -- home / away / overall
            league        TEXT NOT NULL DEFAULT '',
            team_name     TEXT NOT NULL,
            bet_type      TEXT NOT NULL,
            result_key    TEXT NOT NULL,
            sample_count  INTEGER NOT NULL DEFAULT 0,
            hit_count     INTEGER NOT NULL DEFAULT 0,
            avg_odds      REAL NOT NULL DEFAULT 0,
            min_odds      REAL NOT NULL DEFAULT 0,
            max_odds      REAL NOT NULL DEFAULT 0,
            hit_rate      REAL NOT NULL DEFAULT 0,
            expected_value REAL NOT NULL DEFAULT 0,
            updated_at    TEXT NOT NULL,
            UNIQUE(scope, league, team_name, bet_type, result_key)
        )
    """)

    # 球队联赛画像（用于识别主联赛/偶发联赛）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS team_league_profile (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            team_name          TEXT NOT NULL,
            league             TEXT NOT NULL,
            sample_count       INTEGER NOT NULL DEFAULT 0,
            team_total_samples INTEGER NOT NULL DEFAULT 0,
            league_share       REAL NOT NULL DEFAULT 0,  -- 该联赛占球队样本比例
            is_primary_league  INTEGER NOT NULL DEFAULT 0,
            dominance_gap      REAL NOT NULL DEFAULT 0,  -- 与第二联赛占比差值
            updated_at         TEXT NOT NULL,
            UNIQUE(team_name, league)
        )
    """)

    # 球队逐年统计（近一年滚动数据，用于推荐加权）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS team_yearly_stats (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            scope              TEXT NOT NULL,          -- home / away / overall
            league             TEXT NOT NULL DEFAULT '',
            team_name          TEXT NOT NULL,
            period_days        INTEGER NOT NULL DEFAULT 365,
            total_matches      INTEGER NOT NULL DEFAULT 0,
            wins               INTEGER NOT NULL DEFAULT 0,
            draws              INTEGER NOT NULL DEFAULT 0,
            losses             INTEGER NOT NULL DEFAULT 0,
            losses_to_weaker   INTEGER NOT NULL DEFAULT 0,   -- 输给弱队
            wins_vs_stronger   INTEGER NOT NULL DEFAULT 0,   -- 赢强队
            comebacks_conceded INTEGER NOT NULL DEFAULT 0,   -- 被逆转
            comebacks_made     INTEGER NOT NULL DEFAULT 0,   -- 逆转对手
            over_25            INTEGER NOT NULL DEFAULT 0,   -- 大于2.5球
            under_25           INTEGER NOT NULL DEFAULT 0,   -- 小于等于2.5球
            odd_goals          INTEGER NOT NULL DEFAULT 0,   -- 单数总进球
            even_goals         INTEGER NOT NULL DEFAULT 0,   -- 双数总进球
            score_dist         TEXT NOT NULL DEFAULT '{}',    -- JSON: {"1:0":5,"2:1":3,...}
            half_full_dist     TEXT NOT NULL DEFAULT '{}',    -- JSON: {"3-3":8,"1-1":4,...}
            goal_dist          TEXT NOT NULL DEFAULT '{}',    -- JSON: {"0":3,"1":8,...}
            updated_at         TEXT NOT NULL,
            UNIQUE(scope, league, team_name, period_days)
        )
    """)

    # 文件校验表（增量更新用）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_checksums (
            filepath  TEXT PRIMARY KEY,
            checksum  TEXT NOT NULL,
            processed_at TEXT NOT NULL
        )
    """)

    # 元数据
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # 索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_date ON matches(match_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_league ON matches(league)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_handicap ON matches(handicap)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pattern_gran ON patterns(granularity, bet_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pattern_ob ON patterns(bet_type, odds_bucket, granularity)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pattern_ev ON patterns(expected_value DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_team_pattern_scope ON team_patterns(scope, league, team_name, bet_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_team_pattern_ev ON team_patterns(expected_value DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_team_league_primary ON team_league_profile(team_name, is_primary_league DESC, league_share DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_team_yearly_scope ON team_yearly_stats(scope, league, team_name, period_days)")

    conn.commit()
    return conn


# ============ 文件扫描 ============

def file_md5(filepath):
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_files(base_dir):
    """扫描所有日期格式的JSON文件"""
    all_jsons = glob.glob(os.path.join(base_dir, "**", "*.json"), recursive=True)
    result = []
    for fp in sorted(all_jsons):
        name = os.path.basename(fp)
        date_str = name.replace(".json", "")
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        result.append((fp, file_md5(fp)))
    return result


def find_changes(conn, all_files):
    """对比数据库，返回需要处理的文件列表"""
    existing = {}
    for row in conn.execute("SELECT filepath, checksum FROM file_checksums"):
        existing[row[0]] = row[1]

    to_process = []
    for fp, cs in all_files:
        if fp not in existing or existing[fp] != cs:
            to_process.append((fp, cs))

    return to_process


# ============ 数据加载 ============

def load_json(filepath):
    """加载JSON文件"""
    name = os.path.basename(filepath)
    date_str = name.replace(".json", "")
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise RuntimeError(f"加载失败: {filepath} ({e})")
    for m in data:
        m["_date"] = date_str
    return data


def parse_match(m, source_file):
    """解析一条比赛记录，返回数据库行"""
    r = m.get("result", {})
    def valid_result(v):
        return v not in ("", None, "延期")

    is_valid = 1 if any([
        valid_result(r.get("half_full_result")),
        valid_result(r.get("win_draw_lose_result")),
        valid_result(r.get("handicap_result")),
        valid_result(r.get("score_result")),
        valid_result(r.get("goal_result")),
    ]) else 0

    return (
        m.get("matchId", ""),
        m.get("_date", ""),
        m.get("league", ""),
        m.get("home", ""),
        m.get("away", ""),
        r.get("handicap", ""),
        r.get("half_full_result", ""),
        safe_float(r.get("half_full_odds")),
        r.get("win_draw_lose_result", ""),
        safe_float(r.get("win_draw_lose_odds")),
        r.get("handicap_result", ""),
        safe_float(r.get("handicap_odds")),
        r.get("score_result", ""),
        safe_float(r.get("score_odds")),
        r.get("goal_result", ""),
        safe_float(r.get("goal_odds")),
        is_valid,
        source_file,
        compute_weight(m.get("_date", "")),  # 竞彩数据也使用时间衰减权重
    )


# ============ 时间衰减权重 ============

def compute_weight(match_date_str, ref_date=None):
    """根据比赛距今的年数计算权重（统一用于竞彩数据和转化数据）
    权重 = max(0.05, 1.0 - 年数 × 0.1)
    当年: 1.0, 3年前: 0.7, 5年前: 0.5, 10年前: 0.05
    """
    if not match_date_str:
        return 1.0
    if ref_date is None:
        ref_date = datetime.now()
    try:
        md = datetime.strptime(match_date_str[:10], "%Y-%m-%d")
        years = (ref_date - md).days / 365.25
        return max(0.05, 1.0 - years * 0.1)
    except (ValueError, TypeError):
        return 1.0


# ============ 转化数据解析（无赔率，从比分推导） ==========

TRANSFORMED_DIR = os.path.join(BASE_DIR, "联赛数据", "转化数据")

HF_CODE_FROM_SCORE = {
    "33": "3-3", "31": "3-1", "30": "3-0",
    "13": "1-3", "11": "1-1", "10": "1-0",
    "03": "0-3", "01": "0-1", "00": "0-0",
}


def derive_results(full_score, half_score, handicap_line):
    """从比分和让球线推导所有玩法结果
    返回: {wdl_result, score_result, goal_result, half_full_result, handicap_result}
    """
    result = {"wdl_result": "", "score_result": "", "goal_result": "",
              "half_full_result": "", "handicap_result": ""}

    try:
        fh, fa = map(int, full_score.split("-"))
    except (ValueError, AttributeError):
        return result

    result["score_result"] = f"{fh}:{fa}"
    total = fh + fa
    result["goal_result"] = str(min(total, 7))

    # 胜平负
    if fh > fa:
        result["wdl_result"] = "3"
    elif fh == fa:
        result["wdl_result"] = "1"
    else:
        result["wdl_result"] = "0"

    # 半全场
    try:
        hh, ha = map(int, half_score.split("-"))
        hr = "3" if hh > ha else ("1" if hh == ha else "0")
        fr = "3" if fh > fa else ("1" if fh == fa else "0")
        result["half_full_result"] = HF_CODE_FROM_SCORE.get(hr + fr, "")
    except (ValueError, AttributeError):
        pass

    # 让球结果
    try:
        hc = float(handicap_line) if handicap_line else 0
        eff_home = fh - hc
        if eff_home > fa:
            result["handicap_result"] = "3"
        elif eff_home == fa:
            result["handicap_result"] = "1"
        else:
            result["handicap_result"] = "0"
    except (ValueError, TypeError):
        pass

    return result


def parse_transformed_match(m, league_name, source_file, ref_date=None):
    """解析转化数据中的一场比赛，返回 match 行元组（含 weight）"""
    r = derive_results(
        m.get("full_score", ""),
        m.get("half_score", ""),
        m.get("handicap", ""),
    )
    match_time = m.get("match_time", "")
    match_date_str = match_time[:10] if match_time else ""
    weight = compute_weight(match_date_str, ref_date)

    # 无赔率数据
    is_valid = 1 if any([r.get("wdl_result"), r.get("handicap_result"),
                         r.get("score_result"), r.get("goal_result"),
                         r.get("half_full_result")]) else 0

    return (
        str(m.get("match_id", "")),
        match_date_str,
        league_name,
        m.get("home_name", ""),
        m.get("away_name", ""),
        # handicap 字段用字符串形式存储
        str(m.get("handicap", "")),
        r.get("half_full_result", ""),
        None,  # half_full_odds
        r.get("wdl_result", ""),
        None,  # wdl_odds
        r.get("handicap_result", ""),
        None,  # handicap_odds
        r.get("score_result", ""),
        None,  # score_odds
        r.get("goal_result", ""),
        None,  # goal_odds
        is_valid,
        source_file,
        weight,
    )


def scan_transformed_files():
    """扫描转化数据目录，返回 [(filepath, checksum, league_name), ...]"""
    files = []
    if not os.path.isdir(TRANSFORMED_DIR):
        return files
    for league_name in sorted(os.listdir(TRANSFORMED_DIR)):
        league_dir = os.path.join(TRANSFORMED_DIR, league_name)
        if not os.path.isdir(league_dir) or league_name.startswith("."):
            continue
        for fname in sorted(os.listdir(league_dir)):
            if not fname.endswith(".json"):
                continue
            fp = os.path.join(league_dir, fname)
            files.append((fp, file_md5(fp), league_name))
    return files

def add_to_agg(agg, granularity, league, handicap, bet_type, result_key, odds, odds_bucket=None, weight=1.0):
    """通用聚合函数：将一条结果数据加入聚合结构（加权版本）"""
    key = (granularity, league, handicap, bet_type, odds_bucket)
    agg[key]["total"] += weight
    agg[key]["results"][result_key]["hit"] += weight
    if odds and odds > 0:
        agg[key]["results"][result_key]["odds"].append(odds)


def add_all_granularities(agg, bet_type, result_key, odds, league, handicap, weight=1.0):
    """将一条结果写入所有聚合维度（含赔率分桶版本），支持权重"""
    granularities = [
        ("global", None, None),
        ("league", league, None),
    ]
    if handicap:
        granularities.extend([
            ("handicap", None, handicap),
            ("league_handicap", league, handicap),
        ])

    ob = get_odds_bucket(bet_type, odds) if odds and odds > 0 else None

    for gran, lg, hc in granularities:
        add_to_agg(agg, gran, lg, hc, bet_type, result_key, odds, weight=weight)
        if ob:
            add_to_agg(agg, gran, lg, hc, bet_type, result_key, odds, odds_bucket=ob, weight=weight)


def aggregate_patterns(conn):
    """从matches表全量聚合，写入patterns表（支持时间衰减权重）"""
    cursor = conn.execute("""
        SELECT league, handicap, half_full_result, half_full_odds,
               wdl_result, wdl_odds, handicap_result, handicap_odds,
               score_result, score_odds, goal_result, goal_odds,
               weight
        FROM matches WHERE is_valid = 1
    """)

    # 聚合结构:
    agg = defaultdict(lambda: {"total": 0.0, "results": defaultdict(lambda: {"hit": 0.0, "odds": []})})

    for row in cursor:
        league, handicap, hf_result, hf_odds, \
            wdl_result, wdl_odds, hc_result, hc_odds, \
            sc_result, sc_odds, gl_result, gl_odds, weight = row
        w = weight if weight else 1.0

        # 半全场：有odds用odds条件，无odds也统计（无赔率数据）
        if hf_result and hf_result in HALF_FULL_MAP:
            add_all_granularities(agg, "half_full", hf_result, hf_odds, league, handicap, weight=w)

        # 胜平负
        if wdl_result and wdl_result in WDL_MAP:
            add_all_granularities(agg, "wdl", wdl_result, wdl_odds, league, handicap, weight=w)

        # 让球胜平负
        if hc_result and hc_result in HANDICAP_RESULT_MAP:
            add_all_granularities(agg, "handicap", hc_result, hc_odds, league, handicap, weight=w)

        # 比分
        if sc_result:
            add_all_granularities(agg, "score", sc_result, sc_odds, league, handicap, weight=w)

        # 进球数
        if gl_result and gl_result in GOAL_MAP:
            add_all_granularities(agg, "goal", gl_result, gl_odds, league, handicap, weight=w)

    # 写入patterns表
    conn.execute("DELETE FROM patterns")
    now = datetime.now().isoformat()
    records = []

    for key, data in agg.items():
        granularity, league, handicap, bet_type, odds_bucket = key
        total = data["total"]

        for result_key, rdata in data["results"].items():
            hit_count = rdata["hit"]
            odds_list = rdata["odds"]
            hit_rate = hit_count / total if total > 0 else 0
            avg_odds = sum(odds_list) / len(odds_list) if odds_list else 0
            min_odds = min(odds_list) if odds_list else 0
            max_odds = max(odds_list) if odds_list else 0
            ev = hit_rate * avg_odds - 1

            records.append((
                granularity, league, handicap, bet_type, result_key, odds_bucket,
                total, hit_count, avg_odds, min_odds, max_odds,
                hit_rate, ev, now
            ))

    conn.executemany("""
        INSERT INTO patterns (
            granularity, league, handicap, bet_type, result_key, odds_bucket,
            sample_count, hit_count, avg_odds, min_odds, max_odds,
            hit_rate, expected_value, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, records)
    conn.commit()
    return len(records)


def aggregate_team_patterns(conn):
    """从matches表全量聚合球队维度统计（home/away/overall，加权版本）"""
    cursor = conn.execute("""
        SELECT league, home, away, handicap,
               wdl_result, wdl_odds, handicap_result, handicap_odds,
               score_result, score_odds, goal_result, goal_odds,
               weight
        FROM matches WHERE is_valid = 1
    """)

    # key=(scope, league, team_name, bet_type)
    agg = defaultdict(lambda: {"total": 0.0, "results": defaultdict(lambda: {"hit": 0.0, "odds": []})})

    def add_team_agg(scope, league, team_name, bet_type, result_key, odds, w=1.0):
        if not team_name:
            return
        key = (scope, league or "", team_name, bet_type)
        agg[key]["total"] += w
        agg[key]["results"][result_key]["hit"] += w
        if odds and odds > 0:
            agg[key]["results"][result_key]["odds"].append(odds)

    for row in cursor:
        league, home, away, handicap, \
            wdl_result, wdl_odds, hc_result, hc_odds, \
            sc_result, sc_odds, gl_result, gl_odds, weight = row
        w = weight if weight else 1.0

        # 不统计 half_full（视角问题）
        if wdl_result and wdl_result in WDL_MAP:
            add_team_agg("home", league, home, "wdl", wdl_result, wdl_odds, w)
            add_team_agg("away", league, away, "wdl", wdl_result, wdl_odds, w)
            add_team_agg("overall", league, home, "wdl", wdl_result, wdl_odds, w)
            add_team_agg("overall", league, away, "wdl", wdl_result, wdl_odds, w)
        if hc_result and hc_result in HANDICAP_RESULT_MAP:
            add_team_agg("home", league, home, "handicap", hc_result, hc_odds, w)
            add_team_agg("away", league, away, "handicap", hc_result, hc_odds, w)
            add_team_agg("overall", league, home, "handicap", hc_result, hc_odds, w)
            add_team_agg("overall", league, away, "handicap", hc_result, hc_odds, w)
        if sc_result:
            add_team_agg("home", league, home, "score", sc_result, sc_odds, w)
            add_team_agg("away", league, away, "score", sc_result, sc_odds, w)
            add_team_agg("overall", league, home, "score", sc_result, sc_odds, w)
            add_team_agg("overall", league, away, "score", sc_result, sc_odds, w)
        if gl_result and gl_result in GOAL_MAP:
            add_team_agg("home", league, home, "goal", gl_result, gl_odds, w)
            add_team_agg("away", league, away, "goal", gl_result, gl_odds, w)
            add_team_agg("overall", league, home, "goal", gl_result, gl_odds, w)
            add_team_agg("overall", league, away, "goal", gl_result, gl_odds, w)

    conn.execute("DELETE FROM team_patterns")
    now = datetime.now().isoformat()
    records = []
    for key, data in agg.items():
        scope, league, team_name, bet_type = key
        total = data["total"]
        for result_key, rdata in data["results"].items():
            hit_count = rdata["hit"]
            odds_list = rdata["odds"]
            hit_rate = hit_count / total if total > 0 else 0
            avg_odds = sum(odds_list) / len(odds_list) if odds_list else 0
            min_odds = min(odds_list) if odds_list else 0
            max_odds = max(odds_list) if odds_list else 0
            ev = hit_rate * avg_odds - 1
            records.append((
                scope, league, team_name, bet_type, result_key,
                total, hit_count, avg_odds, min_odds, max_odds,
                hit_rate, ev, now
            ))

    conn.executemany("""
        INSERT INTO team_patterns (
            scope, league, team_name, bet_type, result_key,
            sample_count, hit_count, avg_odds, min_odds, max_odds,
            hit_rate, expected_value, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, records)
    conn.commit()
    return len(records)


def aggregate_team_league_profile(conn):
    """聚合每个球队在各联赛样本占比，标记主联赛"""
    rows = conn.execute("""
        SELECT league, home, away
        FROM matches WHERE is_valid = 1
    """).fetchall()

    team_league_count = defaultdict(lambda: defaultdict(int))
    team_total = defaultdict(int)

    for league, home, away in rows:
        if home:
            team_league_count[home][league or ""] += 1
            team_total[home] += 1
        if away:
            team_league_count[away][league or ""] += 1
            team_total[away] += 1

    conn.execute("DELETE FROM team_league_profile")
    now = datetime.now().isoformat()
    records = []

    for team, league_map in team_league_count.items():
        total = team_total.get(team, 0)
        if total <= 0:
            continue

        sorted_leagues = sorted(league_map.items(), key=lambda kv: kv[1], reverse=True)
        top_count = sorted_leagues[0][1] if sorted_leagues else 0
        second_count = sorted_leagues[1][1] if len(sorted_leagues) > 1 else 0
        top_share = top_count / total if total else 0
        second_share = second_count / total if total else 0
        gap = top_share - second_share

        for i, (league, cnt) in enumerate(sorted_leagues):
            share = cnt / total if total else 0
            is_primary = 1 if i == 0 else 0
            records.append((
                team, league, cnt, total, share, is_primary, gap if is_primary else 0, now
            ))

    conn.executemany("""
        INSERT INTO team_league_profile (
            team_name, league, sample_count, team_total_samples,
            league_share, is_primary_league, dominance_gap, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, records)
    conn.commit()
    return len(records)


# ============ 球队逐年统计（近365天） ============

def aggregate_team_yearly_stats(conn, period_days=365):
    """从 matches 表按球队聚合近一年滚动统计数据（主/客/总体）。
    统计维度：平局、输弱队、赢强队、被逆转、逆转、大小球、单双、比分/半全场/进球分布。
    """
    import json as _json

    cutoff = (datetime.now() - __import__("datetime").timedelta(days=period_days)).strftime("%Y-%m-%d")

    cursor = conn.execute("""
        SELECT league, home, away, handicap, wdl_result, half_full_result,
               score_result, goal_result
        FROM matches
        WHERE is_valid = 1 AND match_date >= ?
    """, (cutoff,))

    # 聚合结构: key=(scope, league, team_name) → stats dict
    agg = defaultdict(lambda: {
        "total": 0, "wins": 0, "draws": 0, "losses": 0,
        "losses_to_weaker": 0, "wins_vs_stronger": 0,
        "comebacks_conceded": 0, "comebacks_made": 0,
        "over_25": 0, "under_25": 0,
        "odd_goals": 0, "even_goals": 0,
        "scores": defaultdict(int),
        "half_fulls": defaultdict(int),
        "goals": defaultdict(int),
    })

    def _parse_handicap(hc_str):
        try:
            return float(hc_str)
        except (ValueError, TypeError):
            return 0.0

    def _is_favorite(hc_val, perspective):
        """team is favorite if giving handicap"""
        if perspective == "home":
            return hc_val < 0
        else:  # away
            return hc_val > 0

    def _add_stats(scope, league, team_name, wdl, hf, handicap_val,
                   perspective, total_goals, score):
        """将一场比赛的所有统计维度加到指定 team 的 scope 聚合中"""
        key = (scope, league or "", team_name)
        s = agg[key]
        s["total"] += 1

        # 胜平负
        if wdl == "3":
            s["wins"] += 1
        elif wdl == "1":
            s["draws"] += 1
        elif wdl == "0":
            s["losses"] += 1

        # 输给弱队 & 赢强队
        is_fav = _is_favorite(handicap_val, perspective)
        if is_fav and wdl == "0":
            s["losses_to_weaker"] += 1
        if not is_fav and handicap_val != 0 and wdl == "3":
            s["wins_vs_stronger"] += 1

        # 被逆转 & 逆转
        if perspective == "home":
            if hf in ("3-0", "3-1"):
                s["comebacks_conceded"] += 1
            if hf in ("0-3", "0-1"):
                s["comebacks_made"] += 1
        else:  # away — 从主队视角反推
            if hf in ("0-3", "0-1"):
                s["comebacks_conceded"] += 1
            if hf in ("3-0", "3-1"):
                s["comebacks_made"] += 1

        # 大小球（2.5 为界）
        if total_goals is not None:
            if total_goals > 2:
                s["over_25"] += 1
            else:
                s["under_25"] += 1
            # 单双
            if total_goals % 2 == 1:
                s["odd_goals"] += 1
            else:
                s["even_goals"] += 1

        # 比分分布
        if score:
            s["scores"][score] += 1
        # 半全场分布
        if hf:
            s["half_fulls"][hf] += 1
        # 进球分布
        if total_goals is not None:
            s["goals"][str(min(total_goals, 7))] += 1

    for row in cursor:
        league, home, away, handicap, wdl, hf, score, goal_str = row
        hc = _parse_handicap(handicap)
        total_goals = int(goal_str) if goal_str and goal_str.isdigit() else None

        # 主队维度
        if home:
            _add_stats("home", league, home, wdl, hf, hc, "home", total_goals, score)
            _add_stats("overall", league, home, wdl, hf, hc, "home", total_goals, score)
        # 客队维度
        if away:
            _add_stats("away", league, away, wdl, hf, hc, "away", total_goals, score)
            _add_stats("overall", league, away, wdl, hf, hc, "away", total_goals, score)

    # 写入数据库
    conn.execute("DELETE FROM team_yearly_stats")
    now = datetime.now().isoformat()
    records = []

    for key, s in agg.items():
        scope, league, team_name = key
        records.append((
            scope, league, team_name, period_days,
            s["total"], s["wins"], s["draws"], s["losses"],
            s["losses_to_weaker"], s["wins_vs_stronger"],
            s["comebacks_conceded"], s["comebacks_made"],
            s["over_25"], s["under_25"],
            s["odd_goals"], s["even_goals"],
            _json.dumps(dict(s["scores"]), ensure_ascii=False),
            _json.dumps(dict(s["half_fulls"]), ensure_ascii=False),
            _json.dumps(dict(s["goals"]), ensure_ascii=False),
            now,
        ))

    conn.executemany("""
        INSERT INTO team_yearly_stats (
            scope, league, team_name, period_days,
            total_matches, wins, draws, losses,
            losses_to_weaker, wins_vs_stronger,
            comebacks_conceded, comebacks_made,
            over_25, under_25, odd_goals, even_goals,
            score_dist, half_full_dist, goal_dist, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, records)
    conn.commit()
    return len(records)


# ============ 主流程 ============

def main(force_full=False):
    if force_full and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print("已删除旧数据库")

    conn = init_db(DB_PATH)

    # 扫描文件
    print("扫描JSON文件...")
    all_files = scan_files(DATA_DIR)
    print(f"找到 {len(all_files)} 个文件")

    if force_full:
        to_process = all_files
        deleted_files = []
    else:
        to_process = find_changes(conn, all_files)
        existing_files = {row[0] for row in conn.execute("SELECT filepath FROM file_checksums")}
        current_files = {fp for fp, _ in all_files}
        deleted_files = sorted(existing_files - current_files)
        if not to_process:
            if not deleted_files:
                print("无需更新文件，重算统计表...")
                pattern_count = aggregate_patterns(conn)
                team_pattern_count = aggregate_team_patterns(conn)
                team_league_profile_count = aggregate_team_league_profile(conn)
                team_yearly_count = aggregate_team_yearly_stats(conn)
                now = datetime.now().isoformat()
                total = conn.execute("SELECT COUNT(*) FROM matches WHERE is_valid=1").fetchone()[0]
                conn.execute("INSERT OR REPLACE INTO metadata VALUES ('last_update', ?)", (now,))
                conn.execute("INSERT OR REPLACE INTO metadata VALUES ('total_matches', ?)", (str(total),))
                conn.execute("INSERT OR REPLACE INTO metadata VALUES ('yearly_stats_cutoff', ?)", (
                    (datetime.now() - __import__("datetime").timedelta(days=365)).strftime("%Y-%m-%d"),))
                conn.commit()
                print(f"写入 {pattern_count} 条模式统计")
                print(f"写入 {team_pattern_count} 条球队模式统计")
                print(f"写入 {team_league_profile_count} 条球队联赛画像")
                print(f"写入 {team_yearly_count} 条球队逐年统计")
                print("\n完成!")
                conn.close()
                return
        print(f"需处理 {len(to_process)} 个文件")
        if deleted_files:
            print(f"检测到 {len(deleted_files)} 个已删除文件")

    # 加载并写入比赛数据
    all_matches = []
    ok_files = []
    failed_files = []
    skipped_no_id = 0

    for fp, cs in to_process:
        try:
            matches = load_json(fp)
        except Exception as e:
            failed_files.append((fp, str(e)))
            continue

        ok_files.append((fp, cs))
        for m in matches:
            row = parse_match(m, fp)
            if not row[0]:
                skipped_no_id += 1
                continue
            all_matches.append(row)

    now = datetime.now().isoformat()

    try:
        conn.execute("BEGIN")

        # 先处理删除文件
        for fp in deleted_files:
            conn.execute("DELETE FROM matches WHERE source_file = ?", (fp,))
            conn.execute("DELETE FROM file_checksums WHERE filepath = ?", (fp,))

        # 再处理成功加载的变更文件：删除旧数据并写入新数据
        for fp, _ in ok_files:
            conn.execute("DELETE FROM matches WHERE source_file = ?", (fp,))

        if all_matches:
            conn.executemany("""
                INSERT OR REPLACE INTO matches (
                    match_id, match_date, league, home, away, handicap,
                    half_full_result, half_full_odds,
                    wdl_result, wdl_odds,
                    handicap_result, handicap_odds,
                    score_result, score_odds,
                    goal_result, goal_odds,
                    is_valid, source_file, weight
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, all_matches)

        # 只更新成功加载文件的checksum
        for fp, cs in ok_files:
            conn.execute("INSERT OR REPLACE INTO file_checksums VALUES (?, ?, ?)", (fp, cs, now))

        # === 转化数据批量处理（无赔率，从比分推导） ===
        print("\n扫描转化数据...")
        tf_files = scan_transformed_files()
        print(f"找到 {len(tf_files)} 个转化数据文件")

        tf_matches = []
        for fp, cs, league_name in tf_files:
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print(f"  ⚠ 加载失败: {fp} ({e})")
                continue

            # 从 league_info 取联赛名（如果文件内没有，用文件夹名兜底）
            inner_league = data.get("league_info", "") or league_name
            match_list = data.get("matches", [])
            ref_date = datetime.now()

            for m in match_list:
                row = parse_transformed_match(m, inner_league, fp, ref_date)
                if not row[0]:
                    continue
                tf_matches.append(row)

            # 删除该文件的旧数据（如果有）
            conn.execute("DELETE FROM matches WHERE source_file = ?", (fp,))

        if tf_matches:
            conn.executemany("""
                INSERT OR REPLACE INTO matches (
                    match_id, match_date, league, home, away, handicap,
                    half_full_result, half_full_odds,
                    wdl_result, wdl_odds,
                    handicap_result, handicap_odds,
                    score_result, score_odds,
                    goal_result, goal_odds,
                    is_valid, source_file, weight
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, tf_matches)
            print(f"写入 {len(tf_matches)} 条转化数据比赛记录")

        # 聚合统计
        print("聚合模式统计（5个玩法）...")
        pattern_count = aggregate_patterns(conn)
        print(f"写入 {pattern_count} 条模式统计")
        team_pattern_count = aggregate_team_patterns(conn)
        print(f"写入 {team_pattern_count} 条球队模式统计")
        team_league_profile_count = aggregate_team_league_profile(conn)
        print(f"写入 {team_league_profile_count} 条球队联赛画像")
        team_yearly_count = aggregate_team_yearly_stats(conn)
        print(f"写入 {team_yearly_count} 条球队逐年统计（近365天）")

        # 更新元数据
        total = conn.execute("SELECT COUNT(*) FROM matches WHERE is_valid=1").fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO metadata VALUES ('last_update', ?)", (now,))
        conn.execute("INSERT OR REPLACE INTO metadata VALUES ('total_matches', ?)", (str(total),))
        conn.execute("INSERT OR REPLACE INTO metadata VALUES ('yearly_stats_cutoff', ?)", (
            (datetime.now() - __import__("datetime").timedelta(days=365)).strftime("%Y-%m-%d"),))

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    if all_matches:
        print(f"写入 {len(all_matches)} 条比赛记录")
    if skipped_no_id:
        print(f"跳过 {skipped_no_id} 条缺失matchId的记录")
    if failed_files:
        print(f"有 {len(failed_files)} 个文件加载失败，未更新checksum（下次会重试）")
        for fp, err in failed_files[:10]:
            print(f"  - {fp}: {err}")

    # 输出摘要
    print("\n" + "=" * 60)
    print("模式库更新完成")
    print("=" * 60)
    print(f"有效比赛: {total} 场")
    print(f"模式统计: {pattern_count} 条")
    print(f"球队模式统计: {team_pattern_count} 条")
    print(f"球队联赛画像: {team_league_profile_count} 条")
    print(f"球队逐年统计(近365天): {team_yearly_count} 条")

    # 各玩法全局分布
    for bt, info in BET_TYPES.items():
        print(f"\n【全局{info['name']}分布】")
        rows = conn.execute("""
            SELECT result_key, sample_count, hit_rate, avg_odds, expected_value
            FROM patterns WHERE granularity='global' AND bet_type=? ORDER BY hit_rate DESC
        """, (bt,)).fetchall()

        if not rows:
            print(f"  无数据")
            continue

        for row in rows:
            rk, sc, hr, ao, ev = row
            result_map = info.get("result_map") or {}
            desc = result_map.get(rk, rk)
            ev_str = f"+{ev:.3f}" if ev >= 0 else f"{ev:.3f}"
            print(f"  {rk}({desc}): {sc}场 命中率={hr:.1%} 平均赔率={ao:.2f} EV={ev_str}")

    # 正期望TOP10（跨玩法）
    print("\n【正期望组合 TOP10 (样本≥50, 联赛+让球维度)】")
    found = False
    for row in conn.execute("""
        SELECT league, handicap, bet_type, result_key, sample_count, hit_rate, avg_odds, expected_value
        FROM patterns WHERE granularity='league_handicap'
        AND sample_count >= 50 AND expected_value > 0
        ORDER BY expected_value DESC LIMIT 10
    """):
        found = True
        lg, hc, bt, rk, sc, hr, ao, ev = row
        bt_info = BET_TYPES.get(bt, {})
        bt_name = bt_info.get("name", bt)
        result_map = bt_info.get("result_map") or {}
        desc = result_map.get(rk, rk)
        print(f"  {lg}|handicap={hc} → {bt_name}: {rk}({desc}): 命中率={hr:.1%} 赔率={ao:.2f} EV=+{ev:.3f} (样本={sc})")
    if not found:
        print("  未找到正期望组合")

    conn.close()
    print("\n完成!")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="竞彩模式库构建器")
    parser.add_argument("--rebuild", action="store_true", help="全量重建")
    args = parser.parse_args()
    main(force_full=args.rebuild)
