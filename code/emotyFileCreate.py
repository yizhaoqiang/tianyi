import requests
import os
import json
import time
from bs4 import BeautifulSoup

year = "2024"
time_text = "2024年1月"
month = "2024-01"
start = "27"
end = "29"

startTime = month + "-" + start
endTime = month + "-" + end

folder = "data/" + year + "/" + time_text
folder_ids = folder + f"/ids/{start}-{end}"

os.makedirs(folder, exist_ok=True)
os.makedirs(folder_ids, exist_ok=True)

output_match_ids_file = os.path.join(folder_ids, start + "-" + end + ".json")

# === 加载 output_file 内容并更新每个对象的 history 字段 ===
def update_output_file_with_history(output_file):
    if not os.path.exists(output_file):
        print(f"❌ 找不到文件：{output_file}")
        return

    with open(output_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    for item in data:
        html_path = os.path.join(folder_ids, f"{item}.html")
        if not os.path.exists(html_path):
            with open(html_path, "w", encoding="utf-8") as html_file:
                html_file.write("")  # 写入空内容或 "<!-- placeholder -->"
            print(f"📝 已创建空 HTML 文件：{html_path}")
    else:
        print(f"⚠️ HTML 文件已存在，跳过创建：{html_path}")

update_output_file_with_history(output_match_ids_file)