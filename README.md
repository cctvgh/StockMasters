---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'b68c3682-6ee9-44d3-9026-c0ef73cccb59'
  PropagateID: 'b68c3682-6ee9-44d3-9026-c0ef73cccb59'
  ReservedCode1: 'e9deebab-7e4a-4bbe-a0fa-e6973b7cdf15'
  ReservedCode2: 'e9deebab-7e4a-4bbe-a0fa-e6973b7cdf15'
---

# StockMasters — A股/B股/港股多大师投资分析系统

六位投资大师视角，独立打分，加权共识裁决。

输入一个股票代码，系统自动获取实时行情、财务数据和估值指标，让六位大师从各自视角独立评分，形成加权共识买/卖/持建议。

## 核心特性

- **三大市场全覆盖**：A股（沪深600/000/300/688）、B股（深B 200/沪B 900）、港股（1-5位代码）
- **六位投资大师**：价值大师（格雷厄姆）、质量大师（巴菲特）、成长大师（林奇）、红利大师、动量大师、安全边际大师
- **加权共识机制**：各大师按权重综合评分，输出买入/持有/卖出信号 + 置信度
- **国内数据源直连**：基于 akshare，数据来自东方财富/同花顺/新浪/百度估值，受限内网环境可用
- **零外部依赖前端**：纯原生 HTML/CSS/JS，白底淡蓝风格，双击即运行
- **一键启停**：BAT 脚本启动/停止，无需命令行操作

## 快速开始

### 环境要求

- Python 3.10+
- pip

### 安装

```bash
# 克隆仓库
git clone https://github.com/cctvgh/StockMasters.git
cd StockMasters

# 创建虚拟环境
python -m venv venv

# 安装依赖
venv\Scripts\python.exe -m pip install akshare -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 启动

```bash
# 方式一：双击 BAT 文件
启动大师分析.bat    # 启动服务并自动打开浏览器
停止大师分析.bat    # 停止服务

# 方式二：命令行
venv\Scripts\python.exe server.py
```

浏览器访问 http://localhost:8020 即可使用。

## 六位大师评分体系

| 大师 | 流派 | 权重 | 核心指标 |
|------|------|------|----------|
| 价值大师 | 经典价值 · 格雷厄姆 | 22% | PE、PB、PEG |
| 质量大师 | 好生意 · 巴菲特 | 20% | ROE、毛利率、净利率、资产负债率 |
| 成长大师 | 增长驱动 · 林奇 | 18% | 净利润增速、营收增速、增长持续性 |
| 红利大师 | 现金回报 | 15% | 股息率、每股分红 |
| 动量大师 | 趋势跟踪 | 15% | 20日线偏离、52周高低、年度涨跌 |
| 安全边际大师 | 估值分位 · 逆向 | 10% | PE/价格近3年历史分位 |

## 数据源说明

| 市场 | 行情数据 | 估值数据 | 财务数据 |
|------|----------|----------|----------|
| A股 | 东财 datacenter | 东财 stock_value_em | 同花顺财务摘要 |
| B股 | 新浪 stock_zh_b_daily | 新浪spot + 财务反算 | 同花顺财务摘要 |
| 港股 | 百度估值PE历史 + 东财EPS反推 | 百度估值 PE/PB | 东财港股财务指标 |

> **设计要点**：部分东财接口（push2his/push2.eastmoney.com）在企业内网可能被防火墙拦截，系统已实现多数据源降级策略，确保核心功能在受限网络下可用。

## 项目结构

```
StockMasters/
├── server.py          # 后端：数据引擎 + 评分引擎 + HTTP服务
├── index.html         # 前端：原生HTML单页应用
├── 启动大师分析.bat    # Windows一键启动
├── 停止大师分析.bat    # Windows一键停止
└── README.md
```

## 使用示例

- 输入 `600900` 分析长江电力（A股）
- 输入 `200596` 分析古井贡B（深市B股）
- 输入 `00700` 分析腾讯控股（港股）
- 页面提供快捷入口，点击即查

## 技术栈

- **后端**：Python 标准库 HTTPServer + akshare + pandas
- **前端**：原生 HTML/CSS/JS，零外部依赖
- **数据源**：akshare（东方财富、同花顺、新浪财经、百度估值）
- **架构**：规则评分引擎（非LLM），本地运行，数据不外传

## 免责声明

本工具仅供学习研究，不构成投资建议。股市有风险，入市需谨慎。

## License

MIT

> AI生成