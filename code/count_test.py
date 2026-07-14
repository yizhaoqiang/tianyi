import os
import re
from collections import Counter, defaultdict

pattern = re.compile(r'\((.*?)\)')  # 捕获括号内内容

def format_amount(amount: float) -> str:
    """格式化金额：>=100000 显示 x.x万，否则显示整数（按需调整）"""
    amount = float(amount)
    if amount < 100000:
        if amount.is_integer():
            return str(int(amount))
        return f"{amount:.2f}"
    return f"{amount / 10000:.1f}万"

def count_with_amount(folder_path, file_name):
    total_counter = Counter()
    detail = defaultdict(lambda: defaultdict(lambda: {"count": 0, "amount": 0}))

    for root, _, files in os.walk(folder_path):
        for file in files:
            # 更严格地匹配文件名（按需改成 file.endswith(...)）
            if file != file_name:
                continue

            file_path = os.path.join(root, file)
            year = os.path.basename(os.path.dirname(file_path))

            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) < 3:
                        continue

                    first_part = parts[0]
                    try:
                        amount = int(parts[1])
                    except ValueError:
                        continue
                    month_day_part = parts[2]

                    # 按 "_" 拆成每个月份段
                    occurrences = month_day_part.split("_")

                    # 统计该行的出现次数（括号内每个日期算一次）
                    times = 0
                    for occ in occurrences:
                        m = pattern.search(occ)
                        if m:
                            inner = m.group(1).strip()
                            if inner == "":
                                # 括号为空，按 0 或 1 取决于语义；这里我们忽略（0），如需改为 1 可调整
                                continue
                            # 支持中英文逗号、顿号和空白分隔
                            dates = [d for d in re.split(r'[,，、\s]+', inner) if d.strip()]
                            times += len(dates)
                        else:
                            # 没有括号的段，按 1 次计（或改为 0）
                            times += 1

                    if times == 0:
                        # 如果该行没有任何次数信息，可选择跳过或继续
                        continue

                    total_counter[first_part] += times
                    detail[first_part][year]["count"] += times

                    # 如果 amount 是“单次金额”，请把下面一行取消注释（并注释下一行）：
                    # detail[first_part][year]["amount"] += amount * times

                    # 如果 amount 已经是该行总金额（不乘次数），保持下面这一行：
                    detail[first_part][year]["amount"] += amount

    return total_counter, detail

def print_results(total_counter, detail):
    sort_list = []
    for key in total_counter:
        total_count = total_counter[key]
        total_amount = sum(info['amount'] for info in detail[key].values())
        sort_list.append((key, total_count, total_amount))

    sort_list.sort(key=lambda x: (x[1], x[2]), reverse=True)

    print("==== 统计结果 ====")
    for key, total_count, total_amount in sort_list:
        print(f"{key} {total_count} 次 {format_amount(total_amount)}")
        year_info_sorted = sorted(detail[key].items(), key=lambda x: x[0])
        year_str = ", ".join(f"{year}: {info['count']}次-{format_amount(info['amount'])}"
                             for year, info in year_info_sorted)
        print(year_str)

# 示例调用（替换成你的路径与文件名）
if __name__ == "__main__":
    data_folder = "/Users/tianyi/Desktop/JCBUY"
    file_name = "比分 - 3场.json"
    total, detail = count_with_amount(data_folder, file_name)
    print_results(total, detail)
