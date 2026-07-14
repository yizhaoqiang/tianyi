import json
from bs4 import BeautifulSoup

with open("sss.html", encoding="utf-8") as f:
    soup = BeautifulSoup(f, "html.parser")

rows = soup.find_all("tr", class_="trClass")

match_list = []

for row in rows:
    cols = row.find_all("td")
    if len(cols) < 12:
        continue

    # 获取主队 Match ID（matchId）
    home_link = cols[3].find("a")
    match_id = home_link["href"].split("/")[3] if home_link else None

    match_dict = {
        "num": cols[0].text.strip(),
        "time": cols[2].text.strip(),
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

# 输出成结构化 JSON 字典
print(json.dumps(match_list, ensure_ascii=False, indent=2))