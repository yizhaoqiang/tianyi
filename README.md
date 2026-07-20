# 天翼体育 (TianyiSport)

竞彩数据分析工具集，支持竞彩比赛数据采集、投注模拟、统计分析等功能。

## 项目结构

```
TianyiSport/
├── code/
│   ├── titan_jc_spider.py        # 主爬虫 (titan007 数据源)
│   ├── date_jc.py                 # 澳客网数据爬虫
│   ├── match_jc.py                # 比赛数据爬虫
│   ├── match_ls.py                # 联赛赛程爬虫
│   ├── jc_buy_goal.py             # 总进球投注模拟
│   ├── jc_buy_score.py            # 比分投注模拟
│   ├── jc_handicap_buy.py         # 让球投注模拟
│   ├── jc_win_buy.py              # 胜平负投注模拟
│   ├── jc_day_asy.py              # 日分析工具
│   ├── jc_month_asy.py            # 月分析工具
│   ├── jc_year_asy.py             # 年分析工具
│   ├── data/                      # 历史比赛数据 (2012-2026)
│   ├── 新球体育/                   # 新球体育数据与分析
│   ├── 竞彩/                       # 竞彩分析工具
│   ├── 竞彩分析/                    # 竞彩分析结果
│   └── 竞彩分析汇总/                 # 竞彩分析汇总
├── requirements.txt               # Python 依赖
├── pyproject.toml                 # 项目配置
├── .python-version                # Python 版本锁定
└── .gitignore                     # Git 忽略规则
```

## 环境配置

### 前置要求

- Python >= 3.9
- pip

### 安装依赖

```bash
pip install -r requirements.txt
```

### 创建虚拟环境（推荐）

```bash
python3 -m venv venv
source venv/bin/activate    # macOS/Linux
pip install -r requirements.txt
```

## 使用方法

### 数据采集

```bash
# 抓取 titan007 竞彩数据
cd code && python3 titan_jc_spider.py

# 抓取澳客网竞彩数据（需修改脚本中的日期参数）
cd code && python3 date_jc.py
```

### 投注分析

各分析脚本已内置日期/场次参数，直接运行即可：

```bash
cd code && python3 jc_day_asy.py     # 日分析
cd code && python3 jc_month_asy.py   # 月分析
cd code && python3 jc_year_asy.py    # 年分析
cd code && python3 jc_buy_goal.py    # 总进球投注模拟
cd code && python3 jc_buy_score.py   # 比分投注模拟
```

## 注意

- 各脚本包含硬编码的日期/年份参数，使用前请根据实际需要修改
- 爬虫脚本包含请求延时，避免对目标服务器造成压力
- 数据文件较大，已通过 .gitignore 排除 .sqlite 数据库文件
