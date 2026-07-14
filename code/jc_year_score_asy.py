import os
import json
from collections import Counter

# ====== 可配置项 ======
YEAR = 2025
INPUT_DIR = f"竞彩分析汇总/{YEAR}年"  # 文件夹路径，替换为你的数据目录
TARGET_ORDER = "F4"
TARGET_TYPE = "score"
TOP_N = 6

# ====== 初始化全局统计 ======
score_counter = Counter()

# ====== 遍历文件夹中的所有 JSON 文件 ======
for root, _, files in os.walk(INPUT_DIR):
    for file in files:
        if not file.endswith(".json"):
            continue
        file_path = os.path.join(root, file)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                outer_data = json.load(f)

            # 获取内层的 data 数组
            inner_data_array = outer_data.get("data", [])

            # 获取所有匹配 TARGET_ORDER 的 result 列表
            results = []
            for item in inner_data_array:
                if item.get("order") == TARGET_ORDER:
                    results.extend(item.get("result", []))

            for result_item in results:
                if result_item.get("type") != TARGET_TYPE:
                    continue
                for item in result_item.get("list", []):
                    result_str = item.get("result", "")
                    if not result_str:
                        continue
                    # 拆分比分字符串
                    scores = result_str.split("_")
                    score_counter.update(scores)

        except Exception as e:
            print(f"⚠️ 解析文件出错: {file_path} ({e})")

for score, count in score_counter.most_common(TOP_N):
    print(f"{score}: {count} 次")