import requests
import os
import json
import time
from bs4 import BeautifulSoup

year = "2025"
time_text = "2025年5月"
month = "2025-05"
start = "01"
end = "02"

startTime = month + "-" + start
endTime = month + "-" + end

folder = "data/" + year + "/" + time_text
folder_ids = folder + "/ids"

os.makedirs(folder, exist_ok=True)
os.makedirs(folder_ids, exist_ok=True)

output_file = os.path.join(folder, start + "-" + end + ".json")
output_match_ids_file = os.path.join(folder_ids, start + "-" + end + ".json")

def extract_matches_from_div(div, limit=16):
    matches = []
    for row in div.find_all("tr", attrs={"data-matchid": True}):
        tds = row.find_all("td")
        if len(tds) < 6:
            continue

        fullScore = tds[3].get_text(strip=True)
        halfScore = tds[5].get_text(strip=True)

        if fullScore != '-' and halfScore != '-':
            try:
                full_home, full_away = map(int, fullScore.replace(':', '-').split('-'))
                half_home, half_away = map(int, halfScore.replace(':', '-').split('-'))
            except ValueError:
                continue

            match = {
                "主队": tds[2].get_text(strip=True),
                "客队": tds[4].get_text(strip=True),
                "比分": fullScore,
                "半场比分": halfScore,
                "全场总进球": full_home + full_away,
                "半场总进球": half_home + half_away,
            }
            matches.append(match)

        if len(matches) >= limit:
            break

    return matches

def fetch_match_history(match_id):
    url = f"https://www.okooo.com/soccer/match/{match_id}/history/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36",
        "Referer": f"https://www.okooo.com/soccer/match/{match_id}/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": "\"Android\"",
        "upgrade-insecure-requests": "1",
    }

    # 发送请求
    session = requests.Session()
    response = session.get(url, headers=headers)
    response.encoding = "gb2312"  # 页面编码是 gb2312（关键）
    # 用 BeautifulSoup 解析 HTML
    soup = BeautifulSoup(response.text, "html.parser")

    home_div = soup.find("table", class_="homecomp")
    away_div = soup.find("table", class_="awaycomp")

    home_matches = extract_matches_from_div(home_div)
    away_matches = extract_matches_from_div(away_div)

    # print(f"\n✅ 主队最近 16 场比赛（Match ID: {match_id}）：")
    # for match in home_matches:
    #     print(match)
    #
    # print(f"\n✅ 客队最近 16 场比赛（Match ID: {match_id}）：")
    # for match in away_matches:
    #     print(match)

    return home_matches, away_matches

# === 加载 output_file 内容并更新每个对象的 history 字段 ===
def update_output_file_with_history(output_file):
    if not os.path.exists(output_file):
        print(f"❌ 找不到文件：{output_file}")
        return

    with open(output_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    for item in data:
        match_id = item.get("matchId")
        if not match_id:
            continue

        print(f"📥 正在获取 Match ID：{match_id}")
        try:
            home, away = fetch_match_history(match_id)
            item["history"] = {
                "home": home,
                "away": away
            }
        except Exception as e:
            print(f"❌ 获取失败：{match_id}，错误：{e}")
            item["history"] = {
                "home": [],
                "away": [],
                "error": str(e)
            }
        print("已完成比赛数据的添加： Match ID：{match_id}")
        time.sleep(3)  # 延迟 3 秒防止被封

    # with open(output_file, "w", encoding="utf-8") as f:
    #     json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 已将历史数据写入：{output_file}")

# update_output_file_with_history(output_file)
fetch_match_history(1280955)
