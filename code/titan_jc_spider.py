#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
新球体育数据爬虫

从 titan007 抓取竞彩数据（2012-01-01 ~ 2026-06-01），输出到 新球体育/竞彩数据/ 目录。
目录结构：竞彩数据/YYYY年/MM月/YYYY-MM-DD.json

接口：JcResult.aspx
  - 返回结构："联赛字典$比赛明细$赛果+赔率明细"
  - 联赛字典: "25^日职联!38^英超!..."
  - 比赛明细: "id^time^num^leagueId...!..."
  - 赛果明细: "id^wdl结果^wdl赔率^让球描述^让球赔率^进球数^进球赔率^半全场^半全场赔率^比分^比分赔率"

用法：
  /usr/bin/python3 titan_jc_spider.py
"""

import json
import os
import time
import re
import random
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import requests

BASE_DIR = "/Users/yzq/Desktop/sports_db/code/新球体育/竞彩数据"
BASE_RESULT_URL = "https://jc.titan007.com/handle/JcResult.aspx"
START_DATE = "2026-07-14"
END_DATE = "2026-07-14"

SLEEP_BETWEEN_DATES_SEC = (2.0, 4.0)
MAX_RETRIES = 3
REQUEST_TIMEOUT = 15

# 中文→代码映射
WDL_MAP = {"胜": "3", "平": "1", "负": "0"}
HF_MAP = {
    "胜胜": "3-3", "平平": "1-1", "负负": "0-0",
    "平胜": "1-3", "平负": "1-0", "胜平": "3-1",
    "胜负": "3-0", "负平": "0-1", "负胜": "0-3",
}


def log(prefix, msg):
    print(f"  [{prefix}] {msg}")


def daterange(start_ymd, end_ymd):
    start = datetime.strptime(start_ymd, "%Y-%m-%d").date()
    end = datetime.strptime(end_ymd, "%Y-%m-%d").date()
    d = start
    while d <= end:
        yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def sleep_rand(rng):
    lo, hi = rng
    time.sleep(random.uniform(lo, hi))


def request_text(url):
    for i in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Referer": "https://jc.titan007.com/",
            })
            r.raise_for_status()
            return r.text.strip()
        except Exception as e:
            if i < MAX_RETRIES:
                time.sleep(0.8 * i)
            else:
                raise


def fetch_result(date_str):
    ts = int(time.time() * 1000)
    return request_text(f"{BASE_RESULT_URL}?d={date_str}&{ts}")


def fetch_odds_raw(date_str, cid=105, st=1):
    ts = int(time.time() * 1000)
    return request_text(f"https://jc.titan007.com/handle/oddsData.aspx?d={date_str}&cid={cid}&st={st}&{ts}")


def parse_league_dict(raw):
    """解析第一个$之前的联赛字典"""
    dp = raw.find("$")
    if dp <= 0:
        return {}
    league_map = {}
    for item in raw[:dp].split("!"):
        if not item:
            continue
        f = item.split("^")
        if len(f) >= 4:
            lid, lname_raw = f[0].strip(), f[3]
            lname = lname_raw.split(",")[0].strip() if lname_raw else ""
            if lid and lname:
                league_map[lid] = lname
    return league_map


def parse_match_detail(items_str):
    """解析比赛明细（$1 和 $2 之间的部分）"""
    matches = {}
    for item in items_str.split("!"):
        if not item:
            continue
        p = item.split("^")
        if len(p) < 15:
            continue
        mid = p[0].strip()
        matches[mid] = p
    return matches


def parse_result_detail(items_str):
    """解析赛果+赔率明细（最后一个$之后的部分）"""
    results = {}
    for item in items_str.split("!"):
        if not item:
            continue
        p = item.split("^")
        if len(p) < 11:
            continue
        mid = p[0].strip()
        results[mid] = p
    return results


def parse_hc_desc(desc):
    """解析让球描述 '胜(+1)' → ('+1', '3'); '负(-3)' → ('-3', '0')"""
    m = re.match(r"([胜平负])\(([+-]?\d+\.?\d*)\)", desc or "")
    if m:
        return m.group(2), WDL_MAP.get(m.group(1), "")
    return "", ""




def parse_finish_odds(raw):
    """解析 oddsData.aspx 返回（8字段格式）
    每场: id^w3^w1^w0^handicap^h3^h1^h0
    返回: {matchId: {wdl: {w/d/l}, handicap: {w/d/l}}}
    """
    result = {}
    for item in raw.split("!"):
        if not item:
            continue
        fields = item.split("^")
        if len(fields) < 8:
            continue
        mid = fields[0].strip()
        if not mid:
            continue

        def v(i):
            return fields[i].strip() if i < len(fields) and fields[i].strip() else ""

        wdl_odds = {"w": v(1), "d": v(2), "l": v(3)}
        hc_line = v(4)

        # handicap=0时，wdl赔率=让球赔率（API把wdl字段留空了）
        if not any(wdl_odds.values()) and any(v(i) for i in (5, 6, 7)):
            wdl_odds = {"w": v(5), "d": v(6), "l": v(7)}

        entry = {"wdl": wdl_odds}
        if hc_line and hc_line != "0":
            entry["handicap"] = {"line": hc_line, "w": v(5), "d": v(6), "l": v(7)}
        result[mid] = entry
    return result


def parse_time(t_str, default_month=None, default_day=None):
    """解析比赛时间 "2012,0,1,13,00,00" → "2012-01-01 13:00"（月份0-based→1-based）"""
    try:
        p = t_str.split(",")
        if len(p) >= 5:
            y = int(p[0]) if p[0].strip() else 0
            m = int(p[1]) + 1  # 0-based → 1-based
            d = int(p[2])
            h = int(p[3])
            mi = p[4]
        elif len(p) == 2:
            return f"????-??-?? {p[0]}:{p[1]}"
        else:
            return t_str

        # 月份异常时用默认值
        if m <= 0 or m > 12:
            if default_month is not None:
                m = default_month
            if default_day is not None:
                d = default_day

        return f"{y:04d}-{m:02d}-{d:02d} {h:02d}:{mi}"
    except:
        return t_str


def build_result(parts, odds):
    """构建result对象（以赛果明细为权威来源）"""
    res = {
        "half": "", "full": "",
        "win_draw_lose_result": "", "win_draw_lose_odds": "",
        "handicap": "", "handicap_result": "", "handicap_odds": "",
        "score_result": "", "score_odds": "",
        "goal_result": "", "goal_odds": "",
        "half_full_result": "", "half_full_odds": "",
    }

    # 半场比分（parts[13]-parts[14]）
    half_h = parts[13] if len(parts) > 13 else ""
    half_a = parts[14] if len(parts) > 14 else ""
    res["half"] = f"{half_h}-{half_a}"

    # 赛果明细优先（第三个$块，权威来源）
    if odds:
        wdl_cn = odds[1] if len(odds) > 1 else ""
        wdl_od = odds[2] if len(odds) > 2 else ""
        hc_desc = odds[3] if len(odds) > 3 else ""
        hc_od = odds[4] if len(odds) > 4 else ""
        goal_res = odds[5] if len(odds) > 5 else ""
        goal_od = odds[6] if len(odds) > 6 else ""
        hf_cn = odds[7] if len(odds) > 7 else ""
        hf_od = odds[8] if len(odds) > 8 else ""
        score_str = odds[9] if len(odds) > 9 else ""
        score_od = odds[10] if len(odds) > 10 else ""

        # 全场比分（从score_result取）
        if score_str:
            res["full"] = score_str.replace(":", "-")
            res["score_result"] = score_str
            # 从比分推进球数
            parts_s = score_str.split(":")
            if len(parts_s) == 2:
                try:
                    tg = int(parts_s[0]) + int(parts_s[1])
                    if not goal_res:
                        res["goal_result"] = str(min(tg, 7))
                except:
                    pass

        if wdl_cn:
            res["win_draw_lose_result"] = WDL_MAP.get(wdl_cn, "")
        if wdl_od:
            res["win_draw_lose_odds"] = wdl_od

        if hc_desc:
            hc_line, hc_rk = parse_hc_desc(hc_desc)
            res["handicap"] = hc_line
            if hc_rk:
                res["handicap_result"] = hc_rk
        if hc_od:
            res["handicap_odds"] = hc_od

        if goal_res:
            res["goal_result"] = goal_res
        if goal_od:
            res["goal_odds"] = goal_od

        if hf_cn:
            hf_code = HF_MAP.get(hf_cn, hf_cn)
            res["half_full_result"] = hf_code
        if hf_od:
            res["half_full_odds"] = hf_od

        if score_od:
            res["score_odds"] = score_od

        # 平手盘处理：handicap=0时，让球赔率=胜平负赔率
        hc_line_val = res.get("handicap", "")
        if hc_line_val == "0" or (not hc_line_val and res.get("handicap_odds") and not res.get("win_draw_lose_odds")):
            if res.get("handicap_odds") and not res.get("win_draw_lose_odds"):
                res["win_draw_lose_odds"] = res["handicap_odds"]
            res["handicap"] = None
            res["handicap_result"] = None
            res["handicap_odds"] = None

    # 无赛果明细时，从比赛明细推算（兜底）
    else:
        full_h = parts[11] if len(parts) > 11 else ""
        full_a = parts[12] if len(parts) > 12 else ""
        res["full"] = f"{full_h}-{full_a}"
        res["score_result"] = f"{full_h}:{full_a}"
        try:
            fh, fa = int(full_h), int(full_a)
            if fh > fa:
                res["win_draw_lose_result"] = "3"
            elif fh == fa:
                res["win_draw_lose_result"] = "1"
            else:
                res["win_draw_lose_result"] = "0"
            res["goal_result"] = str(min(fh + fa, 7))
            res["half_full_result"] = f"{res['win_draw_lose_result']}-{res['win_draw_lose_result']}"
        except:
            pass

    return res


def get_json_path(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return os.path.join(BASE_DIR, f"{dt.year}年", f"{dt.month}月", f"{date_str}.json")


def process_date(date_str):
    try:
        raw = fetch_result(date_str)
        sleep_rand((1.0, 2.5))
        raw_odds = fetch_odds_raw(date_str, cid=105, st=1)
    except Exception as e:
        log(date_str, f"请求失败: {e}")
        return False

    # 解析三个$
    d1 = raw.find("$")
    d2 = raw.rfind("$")
    if d1 < 0 or d2 <= d1:
        log(date_str, "数据格式异常（无$分隔）")
        return False

    league_map = parse_league_dict(raw)
    matches = parse_match_detail(raw[d1 + 1:d2])
    results = parse_result_detail(raw[d2 + 1:])

    # 解析 finish_odds（从 oddsData.aspx 返回）
    finish_odds_map = parse_finish_odds(raw_odds)

    if not matches:
        log(date_str, "当日无比赛数据")
        return False

    # 构建JSON
    json_data = []
    req_dt = datetime.strptime(date_str, "%Y-%m-%d")
    for mid, parts in matches.items():
        league_id = parts[5].strip() if len(parts) > 5 else ""
        league_name = league_map.get(league_id, f"联赛{league_id}")
        home = parts[8].split(",")[0].strip() if len(parts) > 8 else ""
        away = parts[10].split(",")[0].strip() if len(parts) > 10 else ""

        # 计算比赛时间的默认月份（先看end_time是否跨月）
        mt_month = req_dt.month
        mt_day = req_dt.day
        if len(parts) > 2 and parts[2]:
            try:
                et = parts[2].split(",")
                if len(et) >= 3:
                    em = int(et[1])
                    ed = int(et[2])
                    if 1 <= em <= 12:
                        mt_month = em
                        mt_day = ed
            except:
                pass

        obj = {
            "num": parts[4].strip() if len(parts) > 4 else "",
            "match_time": parse_time(parts[1], mt_month, mt_day) if len(parts) > 1 else "",
            "matchId": mid,
            "league": league_name,
            "home": home,
            "away": away,
            "finish_odds": finish_odds_map.get(mid, {
                "wdl": {"w": "", "d": "", "l": ""},
            }),
            "result": build_result(parts, results.get(mid)),
        }
        json_data.append(obj)

    # 写入文件
    out_path = get_json_path(date_str)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    if os.path.exists(out_path):
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if len(existing) >= len(json_data):
                log(date_str, f"已存在且数据完整 ({len(existing)}场)")
                return True
        except Exception:
            pass

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)

    log(date_str, f"写入 {len(json_data)} 场 -> {out_path}")
    return True


def main():
    print(f"新球体育爬虫 ({START_DATE} ~ {END_DATE})")
    print(f"输出: 新球体育/竞彩数据/")
    print("=" * 50)

    total = ok = empty = err = 0
    for ds in daterange(START_DATE, END_DATE):
        total += 1
        try:
            if process_date(ds):
                ok += 1
            else:
                empty += 1
        except Exception as e:
            err += 1
            log(ds, f"异常: {type(e).__name__}: {e}")

        if total % 50 == 0:
            print(f"  [进度] {total}/{ok}/{empty}/{err} 天(有/空/错)")

        sleep_rand(SLEEP_BETWEEN_DATES_SEC)

    print(f"\n完成! 总{total}天, 有数据{ok}, 无数据{empty}, 失败{err}")


if __name__ == "__main__":
    main()
