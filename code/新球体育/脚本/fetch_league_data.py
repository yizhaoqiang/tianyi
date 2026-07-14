#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
联赛数据下载脚本

从 titan007 下载指定联赛/赛季的比赛数据、大小球数据、亚盘让球数据。

用法：
  1. 修改下方三个变量（联赛名、赛季、文件类型）
  2. 运行：python3 fetch_league_data.py

示例变量值：
  LEAGUE_NAME = "德甲"
  SEASON = "2024-2025"
  FILE_TYPES = ["s8.js", "bs8.js", "l8.js"]

输出目录：新球体育/联赛数据/初始数据/<联赛名>/<赛季>/
  联赛数据.txt
  大小球数据.txt
  亚盘让球数据.txt
"""

import os
import time
import random

try:
    import requests
except ImportError:
    print("错误: 缺少 requests 模块，请安装: pip3 install requests")
    raise

OUTPUT_DIR = "/Users/yzq/Desktop/sports_db/code/新球体育/联赛数据/初始数据"

# ═══════════════════════════════════════
#  ↓↓↓ 在这里修改参数 ↓↓↓
# ═══════════════════════════════════════
LEAGUE_NAME = "澳超"                          # 联赛名称（用于文件夹命名）
START_YEAR = 2012                              # 起始年（"range"制：2012-2013 → 填2012 / "single"制：2012年 → 填2012）
END_YEAR = 2026                                # 结束年（不含，"range"制到2025-2026，"single"制到2026）
SEASON_FORMAT = "range"                       # 赛季格式："range"=跨年制(2012-2013)，"single"=单年制(2012)
FILE_TYPES = ["s273_462.js", "bs273.js", "l273.js"]     # 文件类型列表
# ═══════════════════════════════════════

# 文件名 → (URL子路径, 输出文件名) 映射规则
# FILE_TYPES 改成对应的文件名后自动匹配
FILENAME_RULES = [
    # (文件名前缀, URL路径, 输出文件名)
    ("bs", "bigSmall",    "大小球数据"),
    ("l",  "letGoal",     "亚盘让球数据"),
    ("s",  "matchResult", "联赛数据"),
]


def get_type_info(filename):
    """根据文件名自动推断URL路径和输出文件名"""
    for prefix, url_path, out_name in FILENAME_RULES:
        if filename.startswith(prefix):
            return (url_path, out_name)
    return None

# 缓存清除参数（固定写死）
CACHE_BUSTER = "0.7172134173199805"
VERSION_TS = "2026061113"


def fetch_url(url, retries=3):
    """GET 请求，带重试"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://zq.titan007.com/",
    }
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            r.encoding = "utf-8"
            if r.status_code == 200:
                return r.text
            print(f"  ⚠ HTTP {r.status_code}, 重试 {attempt + 1}/{retries}")
        except requests.exceptions.Timeout:
            print(f"  ⚠ 请求超时, 重试 {attempt + 1}/{retries}")
        except requests.exceptions.RequestException as e:
            print(f"  ⚠ 请求异常: {e}, 重试 {attempt + 1}/{retries}")
        time.sleep(2 * (attempt + 1))
    return None


def build_url(file_type, season):
    """根据文件类型和赛季构建完整 URL"""
    info = get_type_info(file_type)
    if info is None:
        return None
    path_part = info[0]

    # 不同文件类型用不同的缓存参数
    if file_type == "s8.js":
        cache_param = f"version={VERSION_TS}"
    else:
        cache_param = f"flesh={CACHE_BUSTER}"

    url = f"https://zq.titan007.com/jsData/{path_part}/{season}/{file_type}?{cache_param}"
    return url


def download_league_data(league_name, season, file_types):
    """下载指定联赛、赛季的指定数据文件"""
    # 输出目录: 新球体育/<联赛名>/<赛季>/
    output_dir = os.path.join(OUTPUT_DIR, league_name, season)
    os.makedirs(output_dir, exist_ok=True)

    print(f"{'=' * 60}")
    print(f"  联赛: {league_name}")
    print(f"  赛季: {season}")
    print(f"  输出: {output_dir}")
    print(f"  文件: {', '.join(file_types)}")
    print(f"{'=' * 60}")

    results = []
    for ft in file_types:
        info = get_type_info(ft)
        if info is None:
            print(f"\n  ⚠ 跳过未知文件类型: {ft}（文件名需以 s/bs/l 开头）")
            continue

        out_name = info[1]
        url = build_url(ft, season)
        if not url:
            continue

        output_path = os.path.join(output_dir, f"{out_name}.txt")

        print(f"\n  [{out_name}]")
        print(f"  请求: {url[:80]}...")

        content = fetch_url(url)

        if content is not None:
            # 写文件
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)
            size = len(content)
            print(f"  ✓ 已保存 ({size:,} bytes) → {out_name}.txt")
            results.append((ft, output_path, size))
        else:
            print(f"  ✗ 请求失败（重试耗尽）")

        # 请求间隔（避免被限流）
        time.sleep(random.uniform(1.0, 2.5))

    # 汇总
    print(f"\n{'=' * 60}")
    if results:
        print(f"  完成! 成功 {len(results)}/{len(file_types)} 个文件")
        for ft, path, size in results:
            print(f"    {get_type_info(ft)[1]}: {os.path.basename(path)} ({size:,} bytes)")
    else:
        print("  未下载到任何数据")
    print(f"{'=' * 60}")

    return results


def main():
    """逐赛季循环下载，每个赛季完成后间隔一段时间"""
    # 根据 SEASON_FORMAT 生成赛季列表
    seasons = []
    if SEASON_FORMAT == "single":
        for year in range(START_YEAR, END_YEAR + 1):
            seasons.append(str(year))
        fmt_desc = f"{START_YEAR} ~ {END_YEAR}"
    else:
        for year in range(START_YEAR, END_YEAR):
            seasons.append(f"{year}-{year + 1}")
        fmt_desc = f"{START_YEAR}-{START_YEAR + 1} ~ {END_YEAR - 1}-{END_YEAR}"

    print(f"{'=' * 60}")
    print(f"  联赛: {LEAGUE_NAME}")
    print(f"  赛季格式: {'单年制' if SEASON_FORMAT == 'single' else '跨年制'} ({fmt_desc})")
    print(f"  共 {len(seasons)} 个赛季")
    print(f"  文件类型: {FILE_TYPES}")
    print(f"{'=' * 60}")

    total = len(seasons)
    for i, season in enumerate(seasons, 1):
        print(f"\n{'─' * 60}")
        print(f"  [{i}/{total}] 开始处理赛季: {season}")
        print(f"{'─' * 60}")

        download_league_data(LEAGUE_NAME, season, FILE_TYPES)

        # 每个赛季之间延时，避免封IP
        if i < total:
            delay = random.uniform(3.0, 6.0)
            print(f"  ⏳ 等待 {delay:.1f}s 后进入下一赛季...")
            time.sleep(delay)

    print(f"\n{'=' * 60}")
    print(f"  全部完成! 共 {total} 个赛季")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
