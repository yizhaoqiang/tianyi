import os
import json
from collections import defaultdict

# ====== 配置 ======
YEAR = 2025
BASE_DIR = f"竞彩分析/{YEAR}年"
OUTPUT_BASE_DIR = f"竞彩分析汇总/{YEAR}年"

# ====== 遍历所有月份文件夹 ======
for month_folder in os.listdir(BASE_DIR):
    month_path = os.path.join(BASE_DIR, month_folder)
    if not os.path.isdir(month_path):
        continue

    # 获取月份数字（例："3月" → "03"）
    month_num = "".join([c for c in month_folder if c.isdigit()])
    month_num = month_num.zfill(2)
    month_key = f"{YEAR}-{month_num}"

    print(f"📦 开始统计 {month_key} ...")

    # ====== 汇总结果结构 ======
    summary_data = {
        "time": month_key,
        "data": []
    }

    # { order: { type: { result_str: { count, day, cash } } } }
    order_summary = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {
        "count": 0,
        "day": set(),
        "cash": []
    })))

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

        day = os.path.splitext(file_name)[0].split("-")[-1]  # 取日部分，如 "2025-03-08.json" → "08"

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
                    result_key = "_".join(parts[:-1])  # 如 1-0_1-1
                except Exception:
                    continue

                entry = order_summary[order][key][result_key]
                entry["count"] += 1
                entry["day"].add(day)
                entry["cash"].append(cash_value)

    # ====== 格式化输出 ======
    for order, type_dict in order_summary.items():
        order_result_list = []  # 每个 order 独立列表
        for type_key, result_dict in type_dict.items():
            result_list = []
            for result_str, data in result_dict.items():
                # 拼接 cash 字符串
                cash_str = "_".join(data["cash"])
                # 计算 total 金额
                try:
                    total_amount = sum(int(x) for x in data["cash"])
                except ValueError:
                    total_amount = 0

                result_list.append({
                    "result": result_str,
                    "count": data["count"],
                    "day": "_".join(sorted(data["day"], key=lambda x: int(x))),
                    "cash": cash_str,
                    "total": total_amount
                })

            # 按 count 降序排序
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
    os.makedirs(OUTPUT_BASE_DIR, exist_ok=True)
    output_file = os.path.join(OUTPUT_BASE_DIR, f"{YEAR}年{int(month_num)}月.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, ensure_ascii=False, indent=2)

    print(f"✅ 已生成: {output_file}")
