#
# 功能说明：
#   该脚本主要是为了把澳客竞彩数据抓下来
# 注意：
#   天数不要超过5天(start和end)
#

import json

import os
import requests
from bs4 import BeautifulSoup

# 年月
year = "2026"
time = "2026年5月"
month = "2026-05"
start = "30"
end = "31"

startTime = month + "-" + start
endTime = month + "-" + end

# 请求地址
url = "https://www.okooo.com/jingcai/kaijiang/?LotteryType=SportteryWDL&StartDate="+startTime+"&EndDate="+endTime

# 请求头（模拟浏览器访问）
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://www.okooo.com/",
}

folder = "data/"+year+"/"+time
folder_ids = folder + "/ids"

# 创建以日期命名的文件夹
os.makedirs(folder, exist_ok=True)
os.makedirs(folder_ids, exist_ok=True)

# 构造文件保存路径
output_file = os.path.join(folder, start+"-"+end+".json")
output_match_ids_file = os.path.join(folder_ids, start+"-"+end+".json")

# 发送请求
response = requests.get(url, headers=headers)
response.encoding = "gb2312"  # 页面编码是 gb2312（关键）

# 用 BeautifulSoup 解析 HTML
soup = BeautifulSoup(response.text, "html.parser")

# 提取比赛信息的行（class="trClass"）
rows = soup.find_all("tr", class_="trClass")

match_list = []
match_id_list = []

for row in rows:
    cols = row.find_all("td")
    if len(cols) < 12:
        continue

    # 获取主队 Match ID（matchId）
    home_link = cols[3].find("a")
    match_id = home_link["href"].split("/")[3] if home_link else None
    if match_id :
        match_id_list.append(match_id)

    match_dict = {
        "num": cols[0].text.strip(),
        "match_time": cols[2].text.strip(),
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

# 保存为 JSON 文件
with open(output_file, "w", encoding="utf-8") as f:
    json.dump(match_list, f, ensure_ascii=False, indent=2)
with open(output_match_ids_file, "w", encoding="utf-8") as f:
    json.dump(match_id_list, f, ensure_ascii=False, indent=2)

# 输出成结构化 JSON 字典
# print(json.dumps(match_list, ensure_ascii=False, indent=2))
print(match_id_list)