import os
import json
from collections import defaultdict

# ====== 配置 ======
YEAR = 2025
BASE_DIR = f"竞彩分析/{YEAR}年"
OUTPUT_FILE = f"竞彩分析汇总/{YEAR}年.json"

# ====== 全年汇总结构 ======
summary_data = {
    "time": str(YEAR),
    "data": []
}

# { order: { type: { result_str: { count, day, cash } } } }
order_summary = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {
    "count": 0,
    "day": set(),
    "cash": []
})))

# ====== 遍历所有月份文件夹 ======
for month_folder in os.listdir(BASE_DIR):
    month_path = os.path.join(BASE_DIR, month_folder)
    if not os.path.isdir(month_path):
        continue

    # 获取月份数字（例："3月" → "03"）
    month_num = "".join([c for c in month_folder if c.isdigit()])
    month_num = month_num.zfill(2)

    # ====== 遍历月内所有文件 ======
    for file_name in os.listdir(month_path):
        if not file_name.endswith(".json"):
            continue

        file_path = os.path.join(month_path, file_name)
        with open(file_path, "r", encoding="utf-8") as f:
            try:
                day_data = json.load(f)
            except Exception as e:
                print(f"❌ 读取失败: {file_path} - {e}")
                continue

        # 取日部分，如 "2025-03-08.json" → "08"
        day_num = os.path.splitext(file_name)[0].split("-")[-1]
        day_str = f"{month_num}-{day_num}"  # "MM-DD"

        for content_item in day_data.get("content", []):
            order = content_item["type"].get("order", "")
            results = content_item.get("result", {})

            # 只统计 F 开头的
            if not order.startswith("F"):
                continue

            for key in ["score", "handicap", "halfFull", "size"]:
                value = results.get(key, "")
                if not value or "_" not in value:
                    continue

                try:
                    parts = value.split("_")
                    cash_value = parts[-1]
                    result_key = "_".join(parts[:-1])
                except Exception:
                    continue

                entry = order_summary[order][key][result_key]
                entry["count"] += 1
                entry["day"].add(day_str)
                entry["cash"].append(cash_value)

# ====== 格式化输出 ======
for order, type_dict in order_summary.items():
    order_result_list = []
    for type_key, result_dict in type_dict.items():
        result_list = []
        for result_str, data in result_dict.items():
            cash_str = "_".join(data["cash"])
            try:
                total_amount = sum(int(x) for x in data["cash"])
            except ValueError:
                total_amount = 0

            result_list.append({
                "result": result_str,
                "count": data["count"],
                "day": "_".join(sorted(data["day"], key=lambda x: (int(x.split('-')[0]), int(x.split('-')[1])))),
                "cash": cash_str,
                "total": total_amount
            })

        # 排序：先按 count 降序，再按 day 升序
        result_list.sort(key=lambda x: (-x["count"], x["day"]))

        order_result_list.append({
            "type": type_key,
            "list": result_list
        })

    summary_data["data"].append({
        "order": order,
        "result": order_result_list
    })

# ====== 输出文件 ======
os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(summary_data, f, ensure_ascii=False, indent=2)

print(f"✅ 已生成全年汇总: {OUTPUT_FILE}")
