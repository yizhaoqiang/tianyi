import os
import json
import time
import random
import requests

def fetch_and_save_matches(name, year, sid, total_rounds):
    # 创建以sid命名的文件夹
    folder = f"data/{name}/{year}/{sid}"
    os.makedirs(folder, exist_ok=True)

    # 依次请求每一轮次
    for round_num in range(1, total_rounds + 1):
        url = f"https://liansai.500.com/index.php?c=match&a=getmatch&sid={sid}&round={round_num}"
        try:
            print(f"正在请求 Round {round_num} ...")
            response = requests.get(url, timeout=10)
            response.raise_for_status()  # 如果状态码不是200则抛出异常
            data = response.json()

            # 写入json文件
            file_path = os.path.join(folder, f"第{round_num}轮.json")
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)

            print(f"Round {round_num} 数据已保存到 {file_path}")

        except Exception as e:
            print(f"Round {round_num} 请求失败：{e}")

        # 随机延时 1~3 秒
        delay = random.uniform(1, 3)
        print(f"等待 {delay:.2f} 秒后继续...")
        time.sleep(delay)

if __name__ == "__main__":
    name = '英冠'
    year = '2023-2024赛季'
    sid = 7201            # 可替换为你需要的sid
    total_rounds = 46     # 可替换为你需要的总轮次
    fetch_and_save_matches(name, year, sid, total_rounds)
