from bs4 import BeautifulSoup

def extract_matches_from_div(div, limit=16):
    matches = []

    for row in div.find_all("tr", attrs={"data-matchid": True}):
        tds = row.find_all("td")
        if len(tds) < 6:  # 确保索引不会越界
            continue

        fullScore = tds[3].get_text(strip=True)
        halfScore = tds[5].get_text(strip=True)  # 你需要根据 HTML 实际结构调整下标

        if fullScore != '-' and halfScore != '-':
            # 解析比分为整数
            try:
                full_home, full_away = map(int, fullScore.replace(':', '-').split('-'))
                half_home, half_away = map(int, halfScore.replace(':', '-').split('-'))
            except ValueError:
                continue  # 跳过解析失败的行

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

# 读取 HTML
with open("ddd.html", "r", encoding="utf-8") as file:
    html = file.read()

soup = BeautifulSoup(html, "html.parser")

# 提取主队历史比赛
home_div = soup.find("section", type_="home")
home_matches = extract_matches_from_div(home_div) if home_div else []

# 提取客队历史比赛
away_div = soup.find("section", type_="away")
away_matches = extract_matches_from_div(away_div) if away_div else []

# 打印结果
print("✅ 主队最近 16 场比赛：")
for match in home_matches:
    print(match)

print("\n✅ 客队最近 16 场比赛：")
for match in away_matches:
    print(match)

# import requests
# from bs4 import BeautifulSoup
#
# def extract_matches_from_div(div, limit=16):
#     matches = []
#     for row in div.find_all("tr", attrs={"data-matchid": True}):
#         tds = row.find_all("td")
#         if len(tds) < 6:
#             continue
#
#         fullScore = tds[3].get_text(strip=True)
#         halfScore = tds[5].get_text(strip=True)
#
#         if fullScore != '-' and halfScore != '-':
#             try:
#                 full_home, full_away = map(int, fullScore.replace(':', '-').split('-'))
#                 half_home, half_away = map(int, halfScore.replace(':', '-').split('-'))
#             except ValueError:
#                 continue
#
#             match = {
#                 "主队": tds[2].get_text(strip=True),
#                 "客队": tds[4].get_text(strip=True),
#                 "比分": fullScore,
#                 "半场比分": halfScore,
#                 "全场总进球": full_home + full_away,
#                 "半场总进球": half_home + half_away,
#             }
#             matches.append(match)
#
#         if len(matches) >= limit:
#             break
#
#     return matches
#
# def fetch_match_history(match_id):
#     url = f"https://www.okooo.com/soccer/match/{match_id}/history/"
#     headers = {
#         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
#         "Referer": "https://www.okooo.com/",
#     }
#
#     response = requests.get(url, headers=headers)
#     response.encoding = "gb2312"  # 页面编码是 gb2312（关键）
#
#     soup = BeautifulSoup(response.text, "html.parser")
#     home_div = soup.find("table", class_="homecomp")
#     away_div = soup.find("table", class_="awaycomp")
#
#     home_matches = extract_matches_from_div(home_div)
#     away_matches = extract_matches_from_div(away_div)
#
#     print(f"\n✅ 主队最近 16 场比赛（Match ID: {match_id}）：")
#     for match in home_matches:
#         print(match)
#
#     print(f"\n✅ 客队最近 16 场比赛（Match ID: {match_id}）：")
#     for match in away_matches:
#         print(match)
#
# # 示例调用
# fetch_match_history("1282114")
#
