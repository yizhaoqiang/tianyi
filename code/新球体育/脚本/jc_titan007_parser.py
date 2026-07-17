#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jc_titan007_parser.py
获取竞彩足球比赛信息和赔率数据

功能：
1. 获取当前在售的竞彩比赛（从 bf_jc.txt）
2. 获取比赛的胜平负赔率和让球赔率（从 odds_jc.txt）
3. 只显示当天的比赛
4. 直接 print 输出，不保存文件

使用方法：
  python3 jc_titan007_parser.py
"""

import re
import time
import ssl
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Dict, List


# ── 分隔符（与 JS 保持一致）──────────────────────────────
DOMAIN     = "$"    # 联赛块 vs 赛事块
DATA_TYPE  = "!"    # 记录之间的分隔
SPLIT_REC  = "^"    # 字段之间的分隔
SPLIT_COL  = ","    # 名称多语言版本分隔（简,繁,英）

# ── 比赛状态映射 ─────────────────────────────────────────
STATE_MAP = {
    "-14": "待定",  "-13": "推迟",  "-12": "中断",  "-11": "腰斩",
    "-4":  "取消",  "-1":  "完",
    "0":   "未开场",
    "1":   "上半场",  "3":   "下半场",
    "4":   "加时",    "5":   "点球",
    "13":  "完",
    "15":  "上",     "16":  "中",     "17":  "下",
    "18":  "加",     "19":  "点",
}

CST = timezone(timedelta(hours=8))

# 禁用 SSL 证书验证（解决 self-signed certificate 问题）
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

# 绕过系统代理（HTTP_PROXY/HTTPS_PROXY），直连 titan007
_no_proxy_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl_context),
)


# ── 工具函数 ─────────────────────────────────────────────
def parse_time(s: str):
    """解析 '2026,6,8,00,00,00' 为 datetime（北京时间 UTC+8）
    注意：月份是 0-based，需要 +1 转换为 1-based
    """
    if not s or not s.strip():
        return None
    try:
        p = [int(x.strip()) for x in s.split(",")]
        while len(p) < 6:
            p.append(0)
        # 月份 0-based → 1-based
        month = p[1] + 1
        return datetime(p[0], month, p[2], p[3], p[4], p[5], tzinfo=CST)
    except Exception:
        return None


def split_names(s: str) -> list:
    """把 '阿根廷,阿根廷,阿根廷' 拆成 [简, 繁, 英]"""
    if not s:
        return ["", "", ""]
    parts = s.split(SPLIT_COL)
    while len(parts) < 3:
        parts.append(parts[0] if parts else "")
    return parts


# ── 数据模型 ─────────────────────────────────────────────
class League:
    def __init__(self, raw: str):
        """raw 是一条联赛记录，字段用 ^ 分隔"""
        f = raw.split(SPLIT_REC)
        self.sclass_id   = int(f[0]) if f[0].strip() else 0
        self.color       = f[1] if len(f) > 1 else ""
        self.sub_sclass_id = f[2] if len(f) > 2 else ""
        # 字段3：联赛名称（简,繁,英）
        names = split_names(f[3] if len(f) > 3 else "")
        self.name_simp  = names[0]
        self.name_trad  = names[1]
        self.name_eng   = names[2]
        # 字段4：子联赛名称
        sub = split_names(f[4] if len(f) > 4 else "")
        self.sub_simp   = sub[0]
        self.sub_trad   = sub[1]
        self.url         = f[5] if len(f) > 5 else ""

    def name(self, lang=0) -> str:
        return [self.name_simp, self.name_trad, self.name_eng][lang] or self.name_simp

    def __repr__(self):
        return f"<League {self.sclass_id}: {self.name_simp}>"


class Match:
    def __init__(self, raw: str):
        """raw 是一条赛事记录，字段用 ^ 分隔"""
        f = raw.split(SPLIT_REC)
        self.sid           = int(f[0]) if f[0].strip() else 0
        self.match_time    = parse_time(f[1] if len(f) > 1 else "")
        self.start_time    = parse_time(f[2] if len(f) > 2 else "")
        self.state_code    = f[3] if len(f) > 3 else ""
        self.match_id      = f[4] if len(f) > 4 else ""   # "周二095"
        self.sclass_id     = int(f[5]) if len(f) > 5 and f[5].strip() else 0
        self.sub_sclass_id = f[6] if len(f) > 6 else ""
        # 主队
        self.home_id       = f[7] if len(f) > 7 else ""
        h_names            = split_names(f[8] if len(f) > 8 else "")
        self.home_simp     = h_names[0]
        self.home_trad     = h_names[1]
        self.home_eng      = h_names[2]
        # 客队
        self.guest_id      = f[9] if len(f) > 9 else ""
        g_names            = split_names(f[10] if len(f) > 10 else "")
        self.guest_simp    = g_names[0]
        self.guest_trad     = g_names[1]
        self.guest_eng     = g_names[2]
        # 比分
        self.home_score    = f[11] if len(f) > 11 else ""
        self.guest_score   = f[12] if len(f) > 12 else ""
        self.home_score_h  = f[13] if len(f) > 13 else ""
        self.guest_score_h = f[14] if len(f) > 14 else ""
        # 红黄牌
        self.home_red      = f[15] if len(f) > 15 else "0"
        self.guest_red     = f[16] if len(f) > 16 else "0"
        self.home_yellow   = f[17] if len(f) > 17 else "0"
        self.guest_yellow  = f[18] if len(f) > 18 else "0"
        # 排名
        self.home_order    = f[19] if len(f) > 19 else ""
        self.guest_order   = f[20] if len(f) > 20 else ""
        # 日期 & 让球
        self.range_date    = parse_time(f[21] if len(f) > 21 else "")
        self.first_goal    = f[22] if len(f) > 22 else ""
        self.is_turned     = f[23] if len(f) > 23 else "0"
        
        # 赔率数据（将从 odds_jc.txt 填充）
        self.odds_w3 = 0.0
        self.odds_w1 = 0.0
        self.odds_w0 = 0.0
        self.odds_handicap = ""
        self.odds_h3 = 0.0
        self.odds_h1 = 0.0
        self.odds_h0 = 0.0

    def set_odds(self, odds: Dict):
        """设置赔率数据"""
        self.odds_w3 = odds.get("w3", 0.0)
        self.odds_w1 = odds.get("w1", 0.0)
        self.odds_w0 = odds.get("w0", 0.0)
        self.odds_handicap = odds.get("handicap", "")
        self.odds_h3 = odds.get("h3", 0.0)
        self.odds_h1 = odds.get("h1", 0.0)
        self.odds_h0 = odds.get("h0", 0.0)

    @property
    def state_label(self) -> str:
        return STATE_MAP.get(self.state_code, self.state_code)

    @property
    def score_display(self) -> str:
        s = self.state_code
        if s in ("-1", "13", "1", "3", "4", "5", "15", "16", "17", "18", "19"):
            return f"{self.home_score}-{self.guest_score}"
        return "-"

    def home_name(self, lang=0) -> str:
        return [self.home_simp, self.home_trad, self.home_eng][lang] or self.home_simp

    def guest_name(self, lang=0) -> str:
        return [self.guest_simp, self.guest_trad, self.guest_eng][lang] or self.guest_simp

    def __repr__(self):
        return (f"<Match {self.sid} {self.match_id} "
                f"{self.home_name()}-{self.guest_name()} "
                f"[{self.score_display}] {self.state_label}>")


# ── 过滤函数 ─────────────────────────────────────────────
def get_weekday_match_prefix() -> str:
    """
    获取当前星期几对应的比赛编号前缀
    返回："周二", "周三", "周四" 等
    """
    weekday_map = {
        0: "周一",
        1: "周二",
        2: "周三",
        3: "周四",
        4: "周五",
        5: "周六",
        6: "周日",
    }
    today = datetime.now(CST).weekday()  # 0=周一, 1=周二, ...
    return weekday_map.get(today, "")


def filter_today_matches(matches: List[Match]) -> List[Match]:
    """
    过滤出当天的比赛
    根据比赛编号的前缀（如"周二"）与当前星期几匹配
    """
    prefix = get_weekday_match_prefix()
    if not prefix:
        return matches
    
    filtered = [m for m in matches if m.match_id.startswith(prefix)]
    return filtered


# ── 核心解析 ─────────────────────────────────────────────
def fetch(url: str) -> str:
    """获取接口数据，强制 UTF-8 解码"""
    # 如果 URL 已经包含 ?，则追加 &ts，否则追加 ?ts
    sep = "&" if "?" in url else "?"
    url_with_ts = f"{url}{sep}{int(time.time() * 1000)}"
    req = urllib.request.Request(
        url_with_ts,
        headers={
            "Referer": "https://jc.titan007.com/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
    )
    try:
        with _no_proxy_opener.open(req, timeout=15) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        raise RuntimeError(f"请求失败: {e}") from e


def fetch_odds_jc() -> Dict[str, Dict]:
    """
    获取并解析 odds_jc.txt 接口的数据
    接口：https://jc.titan007.com/xml/odds_jc.txt?timestamp
    格式：sid^w3^w1^w0^handicap^h3^h1^h0!sid^w3^w1^w0^handicap^h3^h1^h0!...
    返回：{sid: {"w3": w3, "w1": w1, "w0": w0, "handicap": handicap, "h3": h3, "h1": h1, "h0": h0}}
    """
    url = f"https://jc.titan007.com/xml/odds_jc.txt?{int(time.time() * 1000)}"
    req = urllib.request.Request(
        url,
        headers={
            "Referer": "https://jc.titan007.com/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
    )
    try:
        with _no_proxy_opener.open(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        raise RuntimeError(f"获取赔率失败: {e}") from e
    
    odds_map = {}
    for item in raw.strip().split("!"):
        item = item.strip()
        if not item:
            continue
        f = item.split("^")
        if len(f) < 8:
            continue
        sid = f[0].strip()
        if not sid:
            continue
        try:
            odds_map[sid] = {
                "w3": float(f[1]) if f[1].strip() else 0.0,
                "w1": float(f[2]) if f[2].strip() else 0.0,
                "w0": float(f[3]) if f[3].strip() else 0.0,
                "handicap": f[4],
                "h3": float(f[5]) if f[5].strip() else 0.0,
                "h1": float(f[6]) if f[6].strip() else 0.0,
                "h0": float(f[7]) if f[7].strip() else 0.0,
            }
        except ValueError:
            continue
    return odds_map


def parse_bf_jc(raw: str) -> tuple:
    """
    解析 bf_jc.txt 返回的数据
    格式：联赛字典$比赛明细
    """
    leagues = {}
    matches = []

    # 分块
    blocks = raw.split(DOMAIN)
    if len(blocks) < 2:
        print("⚠️  数据格式异常（无$分隔）")
        return leagues, matches

    league_block = blocks[0]
    match_block = blocks[1]

    # 解析联赛
    for rec in league_block.split(DATA_TYPE):
        rec = rec.strip()
        if not rec:
            continue
        try:
            lg = League(rec)
            leagues[lg.sclass_id] = lg
        except Exception as e:
            print(f"⚠️  联赛解析跳过: {rec[:40]}... 错误: {e}")

    # 解析比赛
    for rec in match_block.split(DATA_TYPE):
        rec = rec.strip()
        if not rec:
            continue
        try:
            m = Match(rec)
            matches.append(m)
        except Exception as e:
            print(f"⚠️  赛事解析跳过: {rec[:40]}... 错误: {e}")

    return leagues, matches


# ── 打印 ───────────────────────────────────────────────
def pretty_print(leagues: Dict, matches: List[Match], lang: int = 0):
    print(f"\n{'='*90}")
    print(f"  共 {len(leagues)} 个联赛，{len(matches)} 场比赛")
    print(f"{'='*90}\n")

    print("── 赛事列表 ──────────────────────────────────────────────────────────────────────")
    hdr = f"  {'ID':>8s} {'编号':10s} {'主队':16s} {'比分':8s} {'客队':16s} {'状态':6s}"
    print(hdr)
    print("  " + "-" * 88)
    for m in matches:
        lg = leagues.get(m.sclass_id)
        print(f"  {m.sid:>8d} {m.match_id:10s} "
              f"{m.home_name(lang):16s} "
              f"{m.score_display:8s} "
              f"{m.guest_name(lang):16s} "
              f"{m.state_label:6s}")
    print()

    # 显示赔率
    print("── 赔率信息 ──────────────────────────────────────────────────────────────────────")
    print(f"  {'编号':10s} {'主队':16s} {'客队':16s} {'胜':>6s} {'平':>6s} {'负':>6s} {'让球':>6s} {'让胜':>6s} {'让平':>6s} {'让负':>6s}")
    print("  " + "-" * 88)
    
    for m in matches:
        if m.odds_w3 > 0 or m.odds_w1 > 0 or m.odds_w0 > 0:
            handicap_str = m.odds_handicap if m.odds_handicap else "0"
            print(f"  {m.match_id:10s} "
                  f"{m.home_name(lang):16s} "
                  f"{m.guest_name(lang):16s} "
                  f"{m.odds_w3:>6.2f} "
                  f"{m.odds_w1:>6.2f} "
                  f"{m.odds_w0:>6.2f} "
                  f"{handicap_str:>6s} "
                  f"{m.odds_h3:>6.2f} "
                  f"{m.odds_h1:>6.2f} "
                  f"{m.odds_h0:>6.2f}")
        else:
            print(f"  {m.match_id:10s} "
                  f"{m.home_name(lang):16s} "
                  f"{m.guest_name(lang):16s} "
                  f"{'N/A':>6s} "
                  f"{'N/A':>6s} "
                  f"{'N/A':>6s} "
                  f"{'N/A':>6s} "
                  f"{'N/A':>6s} "
                  f"{'N/A':>6s} "
                  f"{'N/A':>6s}")
    print()


# ── 主程序 ─────────────────────────────────────────────
def main():
    print("正在获取竞彩足球比赛和赔率数据 ...")
    
    # 1. 获取比赛数据
    try:
        raw = fetch("https://jc.titan007.com/xml/bf_jc.txt")
        print(f"✅ 比赛数据获取成功（{len(raw)} 字符）")
    except Exception as e:
        print(f"❌ 获取比赛数据失败: {e}")
        return
    
    leagues, matches = parse_bf_jc(raw)
    
    if not matches:
        print("\n⚠️  当前无在售竞彩比赛")
        return
    
    print(f"\n✅ 找到 {len(matches)} 场比赛")
    
    # 1.5 过滤出当天的比赛
    today_matches = filter_today_matches(matches)
    if not today_matches:
        weekday_prefix = get_weekday_match_prefix()
        print(f"  当前在售的比赛：{[m.match_id for m in matches]}")
        return
    
    print(f"  过滤出今天（{get_weekday_match_prefix()}）的比赛：{len(today_matches)} 场")
    matches = today_matches
    
    # 2. 获取赔率数据
    print("\n正在获取赔率数据 ...")
    try:
        odds_map = fetch_odds_jc()
        if odds_map:
            print(f"✅ 获取成功（{len(odds_map)} 场比赛的赔率）")
            
            # 填充赔率数据
            for m in matches:
                if str(m.sid) in odds_map:
                    m.set_odds(odds_map[str(m.sid)])
        else:
            print("⚠️  未获取到赔率数据")
    except Exception as e:
        print(f"⚠️  获取赔率失败: {e}")
    
    # 3. 打印结果
    pretty_print(leagues, matches)
    
    # 提示实时接口
    print(f"\n💡 实时比分变化接口（每 3 秒刷新）：")
    print(f"   https://jc.titan007.com/xml/change_jc.txt")

    # 4. 构造 match_data 并调用 recommend.py 生成推荐
    print("\n" + "=" * 70)
    print("  开始生成投注推荐...")
    print("=" * 70)
    
    today = datetime.now(CST)
    weekday_prefix = get_weekday_match_prefix()
    
    match_data_list = []
    for m in matches:
        # 从 match_id (如 "周四097") 提取编号和标签
        match_id = m.match_id
        match_num = ""
        if match_id:
            digits = "".join(ch for ch in match_id if ch.isdigit())
            match_num = digits.zfill(3) if digits else ""
        
        # 构造赔率字典
        bet_odds = {}
        if m.odds_w3 > 0:
            bet_odds["wdl"] = {"3": m.odds_w3, "1": m.odds_w1, "0": m.odds_w0}
        if m.odds_h3 > 0:
            hc = m.odds_handicap.lstrip("+") if m.odds_handicap else "0"
            bet_odds["handicap"] = {"3": m.odds_h3, "1": m.odds_h1, "0": m.odds_h0}
        
        if not bet_odds:
            continue  # 跳过没有赔率的比赛
        
        league = leagues.get(m.sclass_id)
        league_name = league.name() if league else f"ID={m.sclass_id}"
        
        match_data = {
            "league": league_name,
            "home": m.home_name(),
            "away": m.guest_name(),
            "handicap": m.odds_handicap,
            "match_num": match_num,
            "match_label": match_id,
            "match_weekday": weekday_prefix,
            "match_date_full": today.strftime("%Y-%m-%d"),
            "bet_odds": bet_odds,
            "sid": m.sid,  # 比赛ID，用于抓取盘路数据
        }
        match_data_list.append(match_data)
    
    if match_data_list:
        import recommend
        # 先回填历史推荐文件的命中标记（比赛结果已出但未标记的）
        recommend.backfill_hit_status()
        recommend.run_from_matches(match_data_list)
    else:
        print("⚠️  没有可用的赔率数据，跳过推荐生成")


if __name__ == "__main__":
    main()
