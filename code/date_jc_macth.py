
#
# 功能说明：
#   该脚本主要是为了把拉下来的竞彩数据转化成正确的天数据，方便后续数据统计
#
import json
import os
from datetime import datetime, timedelta

# ====== 可配置项 ======
YEAR = 2026
BASE_INPUT_DIR = f"data/{YEAR}"
BASE_OUTPUT_DIR = "竞彩"

# ====== 星期映射 ======
WEEKDAY_MAP = {
    "周一": 0,
    "周二": 1,
    "周三": 2,
    "周四": 3,
    "周五": 4,
    "周六": 5,
    "周日": 6,
}

# ====== 汇总数据 ======
grouped = {}

# ====== 遍历全年每个月 ======
for month in range(1, 13):
    INPUT_DIR = os.path.join(BASE_INPUT_DIR, f"{YEAR}年{month}月")
    if not os.path.exists(INPUT_DIR):
        print(f"⚠️ 跳过: {INPUT_DIR} (目录不存在)")
        continue

    print(f"\n📅 处理 {YEAR}年{month}月 数据...")

    # 用于判断每天是否已经出现过“001”的标记
    seen_first_match = set()  # e.g. {"2025-03-05": True}

    # 遍历 JSON 文件
    for filename in sorted(os.listdir(INPUT_DIR)):
        if not filename.endswith(".json"):
            continue

        file_path = os.path.join(INPUT_DIR, filename)
        print(f"📖 读取文件: {file_path}")

        # 解析 JSON
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                matches = json.load(f)
        except json.JSONDecodeError as e:
            print(f"❌ JSON 解析错误: {filename} -> {e}")
            continue

        for match in matches:
            num = match.get("num", "")
            match_time = match.get("match_time", "")

            if not num.startswith("周") or not match_time:
                continue

            # 尝试解析 match_time（补年份）
            try:
                dt = datetime.strptime(f"{YEAR}-{match_time}", "%Y-%m-%d %H:%M")
            except ValueError:
                print(f"⚠️ 无法解析时间: {match_time} (文件: {filename})")
                continue

            actual_weekday = dt.weekday()
            week_str = num[:2]
            expected_weekday = WEEKDAY_MAP.get(week_str)
            if expected_weekday is None:
                continue

            # ====== 修正星期差（最短方向）======
            if expected_weekday != actual_weekday:
                diff = (actual_weekday - expected_weekday + 7) % 7
                if diff > 3:
                    diff -= 7
                print(f"⚙️ 修正日期: num={num}, match_time={match_time}, 调整 {diff:+} 天")
                dt = dt - timedelta(days=diff)

            # ✅ 更新 match_time 为完整格式
            match["match_time"] = dt.strftime("%Y-%m-%d %H:%M")

            # ====== 检查 num 是否为当天第一场（001）======
            date_str = dt.strftime("%Y-%m-%d")
            if num[-3:] != "001":
                # 若这一天还没出现过001，则忽略其他比赛
                if date_str not in seen_first_match:
                    continue
            else:
                # 记录这一天已经出现过001
                seen_first_match.add(date_str)

            # ====== 动态输出路径 ======
            year_str = f"{dt.year}年"
            month_str = f"{dt.month}月"

            output_dir = os.path.join(BASE_OUTPUT_DIR, year_str, month_str)
            os.makedirs(output_dir, exist_ok=True)

            output_path = os.path.join(output_dir, f"{date_str}.json")
            grouped.setdefault(output_path, []).append(match)

# ====== 输出结果（追加模式）=====
print("\n🧾 开始写入汇总文件...")
for output_path, match_list in grouped.items():
    if os.path.exists(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
                if isinstance(existing_data, list):
                    match_list = existing_data + match_list
        except json.JSONDecodeError:
            print(f"⚠️ 现有文件 {output_path} 非标准 JSON，已覆盖。")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(match_list, f, ensure_ascii=False, indent=2)

    print(f"✅ 已生成/追加: {output_path} ({len(match_list)} 场比赛)")

print("\n🎉 全年数据处理完成！")
