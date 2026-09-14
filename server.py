# -*- coding: utf-8 -*-
"""
StockMasters - A股/港股多大师投资分析系统
数据源: akshare (东方财富datacenter + 同花顺 + 百度估值, 国内直连)
架构: Python标准库HTTP服务 + 6位大师规则评分引擎 + 原生HTML前端
"""
import os
os.environ["TQDM_DISABLE"] = "1"

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path

import akshare as ak
import pandas as pd
import requests as _req

# ---- 给所有 requests 请求注入 User-Agent (akshare 内部也走 requests, 全局生效) ----
_orig_req = _req.Session.request
def _patched_req(self, *a, **kw):
    kw.setdefault("headers", {})
    if isinstance(kw["headers"], dict):
        kw["headers"].setdefault("User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    return _orig_req(self, *a, **kw)
_req.Session.request = _patched_req

BASE_DIR = Path(__file__).parent
PORT = 8020
CACHE_TTL = 300  # 5分钟数据缓存

_cache = {}       # code -> (ts, result)
_cache_lock = threading.Lock()
_name_cache = {}  # code -> name (持久)
_sv_cache = {}    # code -> DataFrame (stock_value_em 结果缓存)
_hk_cache = {}    # code -> {pe_df, pb_df, fin_df} (港股数据缓存)
_b_spot_cache = {}  # B股全量快照缓存 {key: (ts, df)}


def detect_market(code):
    """识别市场: 'A' / 'B' / 'HK'.
    A股 6位(600/000/300/688等开头), B股 6位(200深B/900沪B开头), 港股 1~5位."""
    code = re.sub(r"\D", "", str(code))
    if len(code) == 6:
        if code.startswith(("200", "900")):
            return "B", code
        return "A", code
    # 1~5位数字视为港股, 补零到5位
    if 1 <= len(code) <= 5:
        return "HK", code.lstrip("0").rjust(5, "0") if code != "0" * 5 else "00000"
    return "A", code.zfill(6)


def with_retry(func, *args, retries=3, base_delay=2, **kwargs):
    """带退避重试的 akshare 调用包装"""
    last_err = None
    for i in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_err = e
            if i < retries - 1:
                time.sleep(base_delay * (i + 1))
    raise last_err


def _get_sv(code):
    """获取并缓存 stock_value_em 数据 (datacenter.eastmoney.com, 内网稳定可达)"""
    if code not in _sv_cache:
        try:
            _sv_cache[code] = with_retry(ak.stock_value_em, symbol=code, retries=2)
        except Exception:
            try:
                _sv_cache[code] = ak.stock_value_em(symbol=code)
            except Exception:
                raise RuntimeError(
                    f"未找到代码 {code} 的数据。"
                    f"支持: 沪深A股(600/000/300/688开头)、B股(200/900开头)、港股(1-5位数字, 如00700)。"
                    f"请确认代码是否正确或已退市。")
    return _sv_cache[code]


def _get_hk_data(code):
    """获取并缓存港股数据 (百度估值PE/PB + 东财港股财务)"""
    if code in _hk_cache:
        return _hk_cache[code]
    data = {}
    # 百度 PE(TTM) 近3年
    data["pe"] = with_retry(ak.stock_hk_valuation_baidu, symbol=code,
                           indicator="市盈率(TTM)", period="近三年", retries=3)
    # 百度 PB 近一年
    time.sleep(0.5)
    data["pb"] = with_retry(ak.stock_hk_valuation_baidu, symbol=code,
                           indicator="市净率", period="近一年", retries=3)
    # 东财港股财务指标
    time.sleep(0.5)
    data["fin"] = with_retry(ak.stock_financial_hk_analysis_indicator_em,
                             symbol=code, retries=3)
    _hk_cache[code] = data
    return data


# ---- B股数据通道 (新浪K线 + 东财快照 + 同花顺财务) ----

def _get_b_daily(code):
    """新浪B股日线, 缓存. code: 6位 (200xxx深B / 900xxx沪B)"""
    key = "B" + code
    if key not in _sv_cache:
        prefix = "sz" if code.startswith("2") else "sh"
        _sv_cache[key] = with_retry(ak.stock_zh_b_daily,
                                    symbol=f"{prefix}{code}", retries=3)
    return _sv_cache[key]


def _get_b_spot():
    """东财B股全量快照(含PE/PB/市值), 缓存5分钟; 被墙时抛异常由调用方降级"""
    key = "__b_spot__"
    ent = _b_spot_cache.get(key)
    now = time.time()
    if ent and now - ent[0] < 300:
        return ent[1]
    df = with_retry(ak.stock_zh_b_spot_em, retries=1)
    _b_spot_cache[key] = (now, df)
    return df


def _get_b_history(code):
    """B股: 新浪日线 -> 价格/趋势"""
    df = _get_b_daily(code)
    if df is None or len(df) == 0:
        raise RuntimeError(f"未找到B股代码 {code} 的行情数据, 请确认代码正确")
    closes = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(closes) == 0:
        raise RuntimeError("B股价格数据为空")
    last = float(closes.iloc[-1])
    chg = round((last / float(closes.iloc[-2]) - 1) * 100, 2) if len(closes) >= 2 else None
    ma20 = float(closes.tail(20).mean()) if len(closes) >= 20 else last
    ma60 = float(closes.tail(60).mean()) if len(closes) >= 60 else last
    win = min(244, len(closes))
    high_52w = float(closes.tail(win).max())
    low_52w = float(closes.tail(win).min())
    yoy_idx = max(0, len(closes) - min(244, len(closes)))
    yoy = (last / float(closes.iloc[yoy_idx]) - 1) * 100 if len(closes) > 10 else None
    date = str(df["date"].iloc[-1])[:10]
    return {
        "price": round(last, 2), "change_pct": chg, "date": date,
        "ma20": round(ma20, 2), "ma60": round(ma60, 2),
        "trend_20": round((last / ma20 - 1) * 100, 2),
        "trend_60": round((last / ma60 - 1) * 100, 2),
        "high_52w": round(high_52w, 2), "low_52w": round(low_52w, 2),
        "off_high_pct": round((last / high_52w - 1) * 100, 2) if high_52w else None,
        "yoy_pct": round(yoy, 2) if yoy is not None else None,
    }


def _get_b_valuation(code):
    """B股估值: 优先东财快照(PE/PB/市值); 被墙时用新浪spot取名称 + 财务反算PE/PB + 价格分位"""
    val = {"pe_ttm": None, "pb": None, "peg": None,
           "pe_percentile_3y": None, "price_percentile_3y": None, "mcap_yi": None}
    # 1. 尝试东财快照 (含PE/PB/市值/名称)
    try:
        df = _get_b_spot()
        row = df[df["代码"].astype(str) == code]
        if len(row) > 0:
            r = row.iloc[0]
            val["pe_ttm"] = sf(r.get("市盈率-动态"))
            val["pb"] = sf(r.get("市净率"))
            mcap = sf(r.get("总市值"))
            if mcap:
                val["mcap_yi"] = round(mcap / 1e8, 1)
            name = str(r.get("名称", "")).strip()
            if name:
                _name_cache[code] = name
    except Exception:
        pass
    # 2. 东财不可用: 新浪spot取名称 + 日线价格 + 财务反算PE/PB
    if val["pe_ttm"] is None or val["pb"] is None:
        try:
            price = None
            d = _get_b_daily(code)
            closes = pd.to_numeric(d["close"], errors="coerce").dropna()
            if len(closes) > 0:
                price = float(closes.iloc[-1])
            spot = with_retry(ak.stock_zh_b_spot, retries=2)
            srow = spot[spot["代码"].astype(str).str.contains(code)]
            if len(srow) > 0:
                name = str(srow.iloc[0].get("名称", "")).strip()
                if name:
                    _name_cache[code] = name
            # 财务反算 PE(年化EPS) / PB(每股净资产)
            if price:
                try:
                    fin = with_retry(ak.stock_financial_abstract_ths, symbol=code, retries=2)
                    if fin is not None and len(fin) > 0:
                        latest = fin.iloc[-1]
                        eps = sf(latest.get("基本每股收益"))
                        bvps = sf(latest.get("每股净资产"))
                        period = str(latest.get("报告期", ""))
                        if eps and eps > 0:
                            month = period[5:7] if len(period) >= 7 else ""
                            factor = {"12": 1, "09": 4/3, "06": 2, "03": 4}.get(month, 1)
                            val["pe_ttm"] = round(price / (eps * factor), 2) if val["pe_ttm"] is None else val["pe_ttm"]
                        if bvps and bvps > 0 and val["pb"] is None:
                            val["pb"] = round(price / bvps, 2)
                except Exception:
                    pass
        except Exception:
            pass
    # 3. 价格近3年分位 (B股无PE历史, 作安全边际代理)
    try:
        d = _get_b_daily(code)
        closes = pd.to_numeric(d["close"], errors="coerce").dropna()
        if len(closes) > 100:
            recent = closes.tail(750)
            val["price_percentile_3y"] = round(float((recent < float(closes.iloc[-1])).mean() * 100), 1)
    except Exception:
        pass
    return val


# ============ 工具函数 ============

def sf(x, default=None):
    """安全转 float"""
    try:
        if x is None or x is False or x == "":
            return default
        v = float(str(x).replace(",", "").replace("%", "").strip())
        return v if v == v else default  # NaN check
    except (ValueError, TypeError):
        return default


def parse_cn_amount(s):
    """解析中文金额: '5.05亿' -> 505000000"""
    v = sf(s)
    if v is not None:
        return v
    if not isinstance(s, str):
        return None
    s = s.strip()
    m = re.match(r"^(-?[\d.]+)\s*亿", s)
    if m:
        return float(m.group(1)) * 1e8
    m = re.match(r"^(-?[\d.]+)\s*万", s)
    if m:
        return float(m.group(1)) * 1e4
    return None


def band(value, bands, missing=5.0):
    """分段评分. bands: [(上限, 分数), ...] 升序排列"""
    if value is None:
        return missing, None
    for upper, score in bands:
        if value <= upper:
            return score, value
    return bands[-1][1], value


# ============ 数据获取 ============

def get_history(code, market="A"):
    """获取价格与趋势数据. A股走东财datacenter, 港股走百度估值反推, B股走新浪日K."""
    if market == "HK":
        return _get_hk_history(code)
    if market == "B":
        return _get_b_history(code)
    try:
        df = _get_sv(code)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"行情数据获取失败(网络波动, 请稍后重试): {str(e)[:100]}")
    if df is None or len(df) == 0:
        raise RuntimeError("未获取到行情/估值数据, 请检查股票代码是否正确")
    closes = pd.to_numeric(df["当日收盘价"], errors="coerce").dropna()
    if len(closes) == 0:
        raise RuntimeError("价格数据为空")
    last_close = float(closes.iloc[-1])
    last_row = df.iloc[-1]
    chg = sf(last_row.get("当日涨跌幅"))
    ma20 = float(closes.tail(20).mean()) if len(closes) >= 20 else last_close
    ma60 = float(closes.tail(60).mean()) if len(closes) >= 60 else last_close
    win = min(244, len(closes))
    high_52w = float(closes.tail(win).max())
    low_52w = float(closes.tail(win).min())
    yoy_idx = max(0, len(closes) - min(244, len(closes)))
    yoy = (last_close / float(closes.iloc[yoy_idx]) - 1) * 100 if len(closes) > 10 else None
    date = str(last_row.get("数据日期", ""))
    return {
        "price": round(last_close, 2), "change_pct": chg, "date": date,
        "ma20": round(ma20, 2), "ma60": round(ma60, 2),
        "trend_20": round((last_close / ma20 - 1) * 100, 2),
        "trend_60": round((last_close / ma60 - 1) * 100, 2),
        "high_52w": round(high_52w, 2), "low_52w": round(low_52w, 2),
        "off_high_pct": round((last_close / high_52w - 1) * 100, 2) if high_52w else None,
        "yoy_pct": round(yoy, 2) if yoy is not None else None,
    }


def _get_hk_history(code):
    """港股: 用百度PE(TTM)历史 + 东财EPS_TTM 反推价格序列"""
    data = _get_hk_data(code)
    pe_df = data.get("pe")
    fin_df = data.get("fin")
    if pe_df is None or len(pe_df) == 0:
        raise RuntimeError("未获取到港股估值数据, 请检查代码是否正确 (如 00700)")
    # 从东财财务取 EPS_TTM
    eps_ttm = None
    if fin_df is not None and len(fin_df) > 0:
        eps_ttm = sf(fin_df.iloc[0].get("EPS_TTM"))
    if eps_ttm is None or eps_ttm <= 0:
        # 无法反推, 仅返回 PE 趋势作为代理
        pe_vals = pd.to_numeric(pe_df["value"], errors="coerce").dropna()
        last_pe = float(pe_vals.iloc[-1])
        return {
            "price": round(last_pe, 2), "change_pct": None,
            "date": str(pe_df.iloc[-1]["date"]),
            "ma20": round(float(pe_vals.tail(20).mean()), 2),
            "ma60": round(float(pe_vals.tail(60).mean()), 2),
            "trend_20": round((float(pe_vals.iloc[-1]) / float(pe_vals.tail(20).mean()) - 1) * 100, 2),
            "trend_60": round((float(pe_vals.iloc[-1]) / float(pe_vals.tail(60).mean()) - 1) * 100, 2),
            "high_52w": round(float(pe_vals.tail(244).max()), 2),
            "low_52w": round(float(pe_vals.tail(244).min()), 2),
            "off_high_pct": round((float(pe_vals.iloc[-1]) / float(pe_vals.tail(244).max()) - 1) * 100, 2),
            "yoy_pct": round((float(pe_vals.iloc[-1]) / float(pe_vals.iloc[0]) - 1) * 100, 2) if len(pe_vals) > 10 else None,
        }
    # 价格 = PE * EPS_TTM
    pe_df = pe_df.copy()
    pe_df["price"] = pd.to_numeric(pe_df["value"], errors="coerce") * eps_ttm
    prices = pe_df["price"].dropna()
    last_price = float(prices.iloc[-1])
    ma20 = float(prices.tail(20).mean()) if len(prices) >= 20 else last_price
    ma60 = float(prices.tail(60).mean()) if len(prices) >= 60 else last_price
    win = min(244, len(prices))
    high_52w = float(prices.tail(win).max())
    low_52w = float(prices.tail(win).min())
    yoy_idx = max(0, len(prices) - min(244, len(prices)))
    yoy = (last_price / float(prices.iloc[yoy_idx]) - 1) * 100 if len(prices) > 10 else None
    # 日涨跌幅: 最近两天价格差
    chg = None
    if len(prices) >= 2:
        chg = round((last_price / float(prices.iloc[-2]) - 1) * 100, 2)
    date = str(pe_df.iloc[-1]["date"])
    return {
        "price": round(last_price, 2), "change_pct": chg, "date": date,
        "ma20": round(ma20, 2), "ma60": round(ma60, 2),
        "trend_20": round((last_price / ma20 - 1) * 100, 2),
        "trend_60": round((last_price / ma60 - 1) * 100, 2),
        "high_52w": round(high_52w, 2), "low_52w": round(low_52w, 2),
        "off_high_pct": round((last_price / high_52w - 1) * 100, 2) if high_52w else None,
        "yoy_pct": round(yoy, 2) if yoy is not None else None,
    }


def get_valuation(code, market="A"):
    """估值数据. A股走东财datacenter, 港股走百度估值, B股走东财快照."""
    if market == "HK":
        return _get_hk_valuation(code)
    if market == "B":
        return _get_b_valuation(code)
    df = _get_sv(code)
    if df is None or len(df) == 0:
        return {}
    last = df.iloc[-1]
    pe = sf(last.get("PE(TTM)"))
    pb = sf(last.get("市净率"))
    peg = sf(last.get("PEG值"))
    pcf = sf(last.get("市现率"))
    ps = sf(last.get("市销率"))
    mcap = sf(last.get("总市值"))
    pe_col = pd.to_numeric(df["PE(TTM)"], errors="coerce").dropna()
    pct = None
    if pe is not None and len(pe_col) > 100:
        recent = pe_col.tail(750)
        pct = round(float((recent < pe).mean() * 100), 1)
    return {
        "pe_ttm": pe, "pb": pb, "peg": peg, "pcf": pcf, "ps": ps,
        "pe_percentile_3y": pct,
        "mcap_yi": round(mcap / 1e8, 1) if mcap else None,
    }


def _get_hk_valuation(code):
    """港股估值: 百度PE/PB + PE近3年分位"""
    data = _get_hk_data(code)
    pe_df = data.get("pe")
    pb_df = data.get("pb")
    pe = pb_val = pct = None
    if pe_df is not None and len(pe_df) > 0:
        pe = float(pe_df.iloc[-1]["value"])
        pe_vals = pd.to_numeric(pe_df["value"], errors="coerce").dropna()
        if len(pe_vals) > 100:
            pct = round(float((pe_vals < pe).mean() * 100), 1)
    if pb_df is not None and len(pb_df) > 0:
        pb_val = float(pb_df.iloc[-1]["value"])
    return {
        "pe_ttm": round(pe, 2) if pe else None,
        "pb": round(pb_val, 2) if pb_val else None,
        "peg": None,  # 港股无PEG数据源
        "pe_percentile_3y": pct,
    }


def get_financials(code, market="A"):
    """财务数据. A股/B股走同花顺(实测支持B股代码), 港股走东财港股财务指标."""
    if market == "HK":
        return _get_hk_financials(code)
    df = with_retry(ak.stock_financial_abstract_ths, symbol=code, retries=3)
    if df is None or len(df) == 0:
        return {}
    df = df.tail(4).reset_index(drop=True)  # 最近4期
    latest = df.iloc[-1] if len(df) > 0 else None
    if latest is None:
        return {}

    def col_val(row, name):
        return sf(row.get(name))

    roe = col_val(latest, "净资产收益率") or col_val(latest, "净资产收益率-摊薄")
    out = {
        "period": str(latest.get("报告期", "")),
        "roe": roe,
        "gross_margin": col_val(latest, "销售毛利率"),
        "net_margin": col_val(latest, "销售净利率"),
        "debt_ratio": col_val(latest, "资产负债率"),
        "profit_growth": col_val(latest, "净利润同比增长率"),
        "revenue_growth": col_val(latest, "营业总收入同比增长率"),
        "eps": col_val(latest, "基本每股收益"),
        "bvps": col_val(latest, "每股净资产"),
        "net_profit": parse_cn_amount(latest.get("净利润")),
    }
    # 近3期净利增速（持续性）
    growths = []
    for _, r in df.iterrows():
        g = col_val(r, "净利润同比增长率")
        if g is not None:
            growths.append(g)
    out["profit_growth_recent"] = growths[-3:]
    out["positive_growth_streak"] = sum(1 for g in reversed(growths) if g > 0) if growths else 0
    return out


def _get_hk_financials(code):
    """港股财务: 东财港股分析指标 (stock_financial_hk_analysis_indicator_em)"""
    data = _get_hk_data(code)
    df = data.get("fin")
    if df is None or len(df) == 0:
        return {}
    df = df.head(4).reset_index(drop=True)  # 最新4期
    latest = df.iloc[0] if len(df) > 0 else None
    if latest is None:
        return {}
    # 港股财务字段名与A股不同
    roe = sf(latest.get("ROE_AVG")) or sf(latest.get("ROE_YEARLY"))
    gm = sf(latest.get("GROSS_PROFIT_RATIO"))
    nm = sf(latest.get("NET_PROFIT_RATIO"))
    dr = sf(latest.get("DEBT_ASSET_RATIO"))
    pg = sf(latest.get("HOLDER_PROFIT_YOY"))  # 归母净利润同比
    rg = sf(latest.get("OPERATE_INCOME_YOY"))  # 营收同比
    eps = sf(latest.get("EPS_TTM")) or sf(latest.get("BASIC_EPS"))
    bvps = sf(latest.get("BPS"))
    out = {
        "period": str(latest.get("REPORT_DATE", ""))[:10] if latest.get("REPORT_DATE") else "",
        "roe": roe, "gross_margin": gm, "net_margin": nm,
        "debt_ratio": dr, "profit_growth": pg, "revenue_growth": rg,
        "eps": eps, "bvps": bvps,
    }
    # 近3期增速
    growths = []
    for _, r in df.iterrows():
        g = sf(r.get("HOLDER_PROFIT_YOY"))
        if g is not None:
            growths.append(g)
    out["profit_growth_recent"] = growths[-3:]
    out["positive_growth_streak"] = sum(1 for g in reversed(growths) if g > 0) if growths else 0
    # 港股名称
    name = str(latest.get("SECURITY_NAME_ABBR", "")).strip()
    if name:
        _name_cache[code] = name
    return out


def get_dividend(code, market="A"):
    """分红数据. A股走东财分红送配; B股/港股暂无可用分红接口, 由红利大师降级处理."""
    if market in ("HK", "B"):
        return {"yield_pct": None, "per_share": None, "plan": None}
    cache_file = BASE_DIR / ".dividend_cache.json"
    today = time.strftime("%Y-%m-%d")
    cache = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    ent = cache.get(code)
    if ent and ent.get("date") == today:
        return ent

    result = {"yield_pct": None, "per_share": None, "plan": None}
    # 尝试最近的年度分配日期
    for date in ["20251231", "20250630", "20241231"]:
        try:
            df = with_retry(ak.stock_fhps_em, date=date, retries=2)
            if df is None or len(df) == 0:
                continue
            row = df[df["代码"] == code]
            if len(row) == 0:
                continue
            r = row.iloc[0]
            y = sf(r.get("现金分红-股息率"))
            if y is not None and y < 1:  # 返回的是小数(0.028=2.8%)
                y = y * 100
            result["yield_pct"] = round(y, 2) if y is not None else None
            result["per_share"] = sf(r.get("现金分红-现金分红比例"))
            result["plan"] = str(r.get("最新公告日期", ""))
            name = str(r.get("名称", "")).strip()
            if name:
                _name_cache[code] = name
            break
        except Exception:
            continue
    result["date"] = today
    cache[code] = result
    try:
        cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return result


# ============ 六位大师评分引擎 ============

def fmt(v, suffix=""):
    return f"{v}{suffix}" if v is not None else "数据缺失"


def master_value(val):
    """价值大师 - 格雷厄姆式: 低估为王"""
    reasons, scores = [], []
    pe = val.get("pe_ttm")
    pb = val.get("pb")
    peg = val.get("peg")
    if pe is None:
        s = 5.0; reasons.append("PE 数据缺失, 按中性处理")
    elif pe <= 0:
        s = 1.5; reasons.append(f"PE 为负({fmt(pe)}), 公司处于亏损状态")
    else:
        s, _ = band(pe, [(8, 10), (15, 8), (25, 6), (40, 3.5)], 1.5)
        reasons.append(f"PE(TTM) {pe:.1f} 倍" + (", 估值偏低" if pe <= 15 else ", 估值中等" if pe <= 25 else ", 估值偏高"))
    scores.append(s)
    if pb is not None:
        s2, _ = band(pb, [(1, 10), (2, 8), (4, 6), (8, 3.5)], 1.5)
        reasons.append(f"市净率 {pb:.2f} 倍" + (", 破净安全" if pb <= 1 else ", 偏低" if pb <= 2 else ", 中等" if pb <= 4 else ", 偏高"))
        scores.append(s2)
    if peg is not None and peg > 0:
        s3, _ = band(peg, [(0.8, 10), (1.5, 8), (2.5, 6), (4, 3.5)], 2)
        reasons.append(f"PEG {peg:.2f}" + (", 成长性价比好" if peg <= 1.5 else ", 增长难以支撑估值" if peg > 2.5 else ", 基本匹配"))
        scores.append(s3)
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_quality(fin):
    """质量大师 - 巴菲特式: 好生意好公司"""
    reasons, scores = [], []
    roe = fin.get("roe")
    gm = fin.get("gross_margin")
    nm = fin.get("net_margin")
    dr = fin.get("debt_ratio")
    if roe is not None:
        s, _ = band(roe, [(5, 2), (10, 4.5), (15, 7), (20, 8.5)], 10)
        reasons.append(f"ROE {roe:.1f}%" + ("，盈利能力优秀" if roe >= 15 else "，尚可" if roe >= 10 else "，偏弱"))
        scores.append(s)
    if gm is not None:
        s, _ = band(gm, [(10, 2), (20, 4), (40, 6), (60, 8)], 10)
        reasons.append(f"毛利率 {gm:.1f}%")
        scores.append(s)
    if nm is not None:
        s, _ = band(nm, [(5, 2), (10, 4), (20, 6), (30, 8)], 10)
        reasons.append(f"净利率 {nm:.1f}%")
        scores.append(s)
    if dr is not None:
        s, _ = band(dr, [(30, 10), (50, 8), (65, 6), (75, 4)], 2)
        reasons.append(f"资产负债率 {dr:.1f}%" + ("，财务稳健" if dr <= 50 else "，偏高但需结合行业看" if dr <= 65 else "，杠杆压力较大"))
        scores.append(s)
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_growth(fin):
    """成长大师 - 林奇式: 增长驱动"""
    reasons, scores = [], []
    pg = fin.get("profit_growth")
    rg = fin.get("revenue_growth")
    if pg is not None:
        s, _ = band(pg, [(-10, 1), (0, 3), (5, 5), (15, 7), (30, 8.5)], 10)
        reasons.append(f"最新期净利润增速 {pg:+.1f}%")
        scores.append(s)
    if rg is not None:
        s, _ = band(rg, [(-10, 1), (0, 3), (5, 5), (15, 7), (30, 8.5)], 10)
        reasons.append(f"营收增速 {rg:+.1f}%")
        scores.append(s)
    streak = fin.get("positive_growth_streak", 0)
    if streak >= 3:
        scores.append(9); reasons.append(f"近 {streak} 期净利润持续正增长, 稳定性好")
    elif streak > 0:
        scores.append(5 + streak); reasons.append(f"近 {streak} 期正增长")
    else:
        scores.append(3); reasons.append("近期增长不稳定")
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_dividend(div, val):
    """红利大师 - A股红利视角: 现金回报"""
    reasons, scores = [], []
    y = div.get("yield_pct")
    ps = div.get("per_share")
    if y is not None:
        s, _ = band(y, [(1, 2), (2, 4.5), (3, 6.5), (4, 8), (5, 9)], 10)
        reasons.append(f"股息率 {y:.2f}%" + ("，现金回报吸引力强" if y >= 3.5 else "，中等" if y >= 2 else "，偏低"))
        scores.append(s)
    else:
        scores.append(4); reasons.append("未获取到最新分红数据")
    if ps:
        reasons.append(f"每股现金分红 {ps} 元")
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_momentum(hist):
    """动量大师 - 趋势跟踪"""
    reasons, scores = [], []
    t20 = hist.get("trend_20")
    if t20 is not None:
        s, _ = band(t20, [(-10, 2), (-5, 3.5), (0, 5.5), (5, 7.5)], 9)
        reasons.append(f"现价高于20日线 {t20:+.1f}%" + ("，短期趋势向上" if t20 > 0 else "，短期走弱"))
        scores.append(s)
    off = hist.get("off_high_pct")
    if off is not None:
        s, _ = band(-off, [(-30, 2.5), (-15, 5), (-5, 7.5)], 9)
        reasons.append(f"距52周高点 {off:+.1f}%")
        scores.append(s)
    yoy = hist.get("yoy_pct")
    if yoy is not None:
        s, _ = band(yoy, [(-20, 2.5), (-10, 4), (0, 5.5), (20, 7)], 9)
        reasons.append(f"近一年涨跌幅 {yoy:+.1f}%")
        scores.append(s)
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_margin(val):
    """安全边际大师 - 估值/价格历史分位 (B股用价格分位替代PE分位)"""
    pct = val.get("pe_percentile_3y")
    metric = "PE"
    if pct is None:
        pct = val.get("price_percentile_3y")
        metric = "价格"
    if pct is None:
        return 5.0, ["估值历史分位数据缺失, 按中性处理"]
    s, _ = band(pct, [(20, 9.5), (40, 8), (60, 6), (80, 4)], 1.5)
    desc = "处于近3年低位, 安全边际较高" if pct <= 40 else "中等水平" if pct <= 60 else "处于近3年高位, 追高风险大"
    return s, [f"当前{metric}位于近3年 {pct:.0f}% 分位, {desc}"]


def master_fisher(fin):
    """费雪大师 - 成长质量: ROE+高毛利+营收增长（Scuttlebutt 优选成长股）"""
    reasons, scores = [], []
    roe = fin.get("roe")
    gm = fin.get("gross_margin")
    rg = fin.get("revenue_growth")
    if roe is not None:
        s, _ = band(roe, [(8, 2), (12, 4), (18, 7), (25, 8.5)], 10)
        reasons.append(f"ROE {roe:.1f}%" + ("，成长质量高" if roe >= 18 else "，尚可" if roe >= 12 else "，偏弱"))
        scores.append(s)
    if gm is not None:
        s, _ = band(gm, [(15, 2), (25, 4), (40, 6), (55, 8)], 10)
        reasons.append(f"毛利率 {gm:.1f}%")
        scores.append(s)
    if rg is not None:
        s, _ = band(rg, [(-5, 2), (5, 3.5), (15, 5.5), (25, 7.5)], 10)
        reasons.append(f"营收增速 {rg:+.1f}%")
        scores.append(s)
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_munger(fin, val):
    """芒格大师 - 好生意好价格: 低负债+高ROE+低估（Lollapalooza 共振）"""
    reasons, scores = [], []
    dr = fin.get("debt_ratio")
    roe = fin.get("roe")
    pe = val.get("pe_ttm")
    pb = val.get("pb")
    if dr is not None:
        s, _ = band(dr, [(30, 9), (50, 7), (65, 5), (80, 3)], 2)
        reasons.append(f"资产负债率 {dr:.1f}%" + ("，财务保守" if dr <= 40 else "，杠杆偏高" if dr > 60 else "，适中"))
        scores.append(s)
    if roe is not None:
        s, _ = band(roe, [(8, 2.5), (12, 4.5), (18, 7), (25, 8.5)], 10)
        reasons.append(f"ROE {roe:.1f}%")
        scores.append(s)
    if pe is not None and pe > 0:
        s, _ = band(pe, [(12, 9), (20, 7), (30, 5), (45, 3)], 2)
        reasons.append(f"PE(TTM) {pe:.1f} 倍")
        scores.append(s)
    if pb is not None:
        s, _ = band(pb, [(1.5, 9), (3, 7), (5, 5), (8, 3)], 1)
        reasons.append(f"PB {pb:.2f} 倍")
        scores.append(s)
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_marks(hist):
    """马克斯大师 - 周期钟摆: 情绪极端处反着做（第二层思维）"""
    reasons, scores = [], []
    yoy = hist.get("yoy_pct")
    off = hist.get("off_high_pct")
    if yoy is not None:
        if yoy >= 60:
            s, desc = 2.0, "一年涨逾60%, 情绪过热, 钟摆近顶"
        elif yoy >= 30:
            s, desc = 3.5, f"一年涨 {yoy:.0f}%, 情绪偏热"
        elif yoy >= 10:
            s, desc = 5.5, f"温和上涨 {yoy:.0f}%, 钟摆中位"
        elif yoy >= -10:
            s, desc = 6.5, f"小幅波动 {yoy:+.0f}%, 情绪平淡"
        elif yoy >= -30:
            s, desc = 8.0, f"回调 {yoy:.0f}%, 悲观酝酿机会"
        else:
            s, desc = 9.0, f"深度回调 {yoy:.0f}%, 逆向布局时机"
        reasons.append(f"近一年 {yoy:+.1f}%, {desc}")
        scores.append(s)
    if off is not None and yoy is not None:
        dd = -off
        if dd >= 20:
            s, d = 8.5, f"距高点回撤 {dd:.0f}%, 风险释放充分"
        elif dd >= 10:
            s, d = 6.5, f"距高点回撤 {dd:.0f}%, 部分释放"
        elif dd >= 3:
            s, d = 4.5, "贴近高点, 情绪偏乐观"
        else:
            s, d = 2.5, "接近历史高位, 警惕亢奋"
        reasons.append(d)
        scores.append(s)
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_soros(hist):
    """索罗斯大师 - 反身性: 趋势加速与自我强化"""
    reasons, scores = [], []
    t20 = hist.get("trend_20")
    t60 = hist.get("trend_60")
    off = hist.get("off_high_pct")
    if t20 is not None and t60 is not None:
        acc = t20 - t60
        if acc >= 4:
            s, d = 8.0, f"短期强度领先长期 {acc:+.1f}pt, 趋势加速"
        elif acc >= 0:
            s, d = 6.5, f"短长期价差 {acc:+.1f}pt, 趋势延续"
        elif acc >= -5:
            s, d = 4.5, f"趋势减速 {acc:+.1f}pt"
        else:
            s, d = 2.5, f"趋势明显走弱 {acc:+.1f}pt"
        reasons.append(d)
        scores.append(s)
    if off is not None:
        if off >= -5:
            s, d = 8.5, "贴近52周高点, 反身性自我强化"
        elif off >= -15:
            s, d = 6.0, "处于上行通道"
        elif off >= -30:
            s, d = 4.0, "中位偏弱"
        else:
            s, d = 2.0, "远离高点, 趋势破坏"
        reasons.append(d)
        scores.append(s)
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_dalio(hist, fin):
    """达利欧大师 - 风险平价: 低波动+低杠杆的稳健配置"""
    reasons, scores = [], []
    yoy = hist.get("yoy_pct")
    dr = fin.get("debt_ratio")
    if yoy is not None:
        vol = abs(yoy)
        if vol <= 8:
            s, d = 8.0, f"年波动 {vol:.0f}%, 低波动稳健"
        elif vol <= 25:
            s, d = 6.5, f"年波动 {vol:.0f}%, 中等"
        elif vol <= 50:
            s, d = 4.0, f"年波动 {vol:.0f}%, 偏高"
        else:
            s, d = 2.0, f"年波动 {vol:.0f}%, 风险大"
        reasons.append(d)
        scores.append(s)
    if dr is not None:
        s, _ = band(dr, [(20, 9), (40, 7.5), (60, 5.5), (75, 3.5), (85, 2)], 2)
        reasons.append(f"资产负债率 {dr:.1f}%")
        scores.append(s)
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_li_lu(fin, val):
    """李录大师 - 能力圈深度价值: 便宜+质地好（护城河折价买入）"""
    reasons, scores = [], []
    pe = val.get("pe_ttm")
    pb = val.get("pb")
    roe = fin.get("roe")
    if pe is not None and pe > 0:
        s, _ = band(pe, [(12, 9), (18, 7), (28, 5), (40, 3)], 2)
        reasons.append(f"PE(TTM) {pe:.1f} 倍")
        scores.append(s)
    if pb is not None:
        s, _ = band(pb, [(1, 9), (2.5, 7), (4, 5), (6.5, 3)], 1)
        reasons.append(f"PB {pb:.2f} 倍")
        scores.append(s)
    if roe is not None:
        s, _ = band(roe, [(5, 2), (10, 4), (15, 6.5), (20, 8)], 10)
        reasons.append(f"ROE {roe:.1f}%")
        scores.append(s)
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_duan(fin):
    """段永平大师 - 本分: 商业模式清晰 + 少犯错（不做什么）"""
    reasons, scores = [], []
    dr = fin.get("debt_ratio")
    roe = fin.get("roe")
    rg = fin.get("revenue_growth")
    if dr is not None:
        s, _ = band(dr, [(30, 9), (50, 7), (65, 5), (80, 3)], 2)
        reasons.append(f"资产负债率 {dr:.1f}%" + ("，财务本分" if dr <= 40 else "，杠杆偏高" if dr > 65 else "，适中"))
        scores.append(s)
    if roe is not None:
        s, _ = band(roe, [(10, 3), (15, 5), (20, 7.5), (28, 8.5)], 10)
        reasons.append(f"ROE {roe:.1f}%")
        scores.append(s)
    if rg is not None:
        s, _ = band(rg, [(0, 3), (10, 5.5), (20, 7), (35, 8)], 10)
        reasons.append(f"营收增速 {rg:+.1f}%")
        scores.append(s)
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_zhang(fin, val):
    """张磊大师 - 长期主义: 好赛道+可持续复利（做时间的朋友）"""
    reasons, scores = [], []
    roe = fin.get("roe")
    pg = fin.get("profit_growth")
    peg = val.get("peg")
    if roe is not None:
        s, _ = band(roe, [(10, 3), (15, 5), (20, 7), (25, 8.5)], 10)
        reasons.append(f"ROE {roe:.1f}%")
        scores.append(s)
    if pg is not None:
        s, _ = band(pg, [(0, 3), (15, 5.5), (30, 7.5), (50, 8.5)], 10)
        reasons.append(f"净利润增速 {pg:+.1f}%")
        scores.append(s)
    if peg is not None and peg > 0:
        s, _ = band(peg, [(1, 8), (1.8, 7), (2.8, 5), (4, 3)], 1)
        reasons.append(f"PEG {peg:.2f}")
        scores.append(s)
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_dan_bin(fin):
    """但斌大师 - 偏爱伟大公司: 品牌护城河+高毛利"""
    reasons, scores = [], []
    gm = fin.get("gross_margin")
    nm = fin.get("net_margin")
    pg = fin.get("profit_growth")
    if gm is not None:
        s, _ = band(gm, [(20, 3), (35, 5), (50, 7), (65, 8.5)], 10)
        reasons.append(f"毛利率 {gm:.1f}%" + ("，品牌溢价强" if gm >= 50 else "，尚可" if gm >= 35 else "，偏低"))
        scores.append(s)
    if nm is not None:
        s, _ = band(nm, [(10, 3), (20, 5.5), (30, 7), (40, 8.5)], 10)
        reasons.append(f"净利率 {nm:.1f}%")
        scores.append(s)
    if pg is not None:
        s, _ = band(pg, [(0, 3), (10, 5.5), (20, 7), (35, 8.5)], 10)
        reasons.append(f"净利润增速 {pg:+.1f}%")
        scores.append(s)
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_thiel(fin):
    """蒂尔大师 - 从0到1: 垄断定价权（利润率高+负债低）"""
    reasons, scores = [], []
    nm = fin.get("net_margin")
    gm = fin.get("gross_margin")
    dr = fin.get("debt_ratio")
    if nm is not None:
        s, _ = band(nm, [(5, 2), (15, 4.5), (25, 6.5), (35, 8.5)], 10)
        reasons.append(f"净利率 {nm:.1f}%" + ("，定价权强" if nm >= 30 else "，一般" if nm >= 15 else "，偏弱"))
        scores.append(s)
    if gm is not None:
        s, _ = band(gm, [(30, 3), (45, 5), (60, 7), (75, 8.5)], 10)
        reasons.append(f"毛利率 {gm:.1f}%")
        scores.append(s)
    if dr is not None:
        s, _ = band(dr, [(40, 8), (60, 6), (75, 4), (85, 2)], 2)
        reasons.append(f"资产负债率 {dr:.1f}%")
        scores.append(s)
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_cathie(fin):
    """木头姐大师 - 破坏式创新: 高增速优先（容忍高估值）"""
    reasons, scores = [], []
    rg = fin.get("revenue_growth")
    pg = fin.get("profit_growth")
    if rg is not None:
        s, _ = band(rg, [(0, 2), (15, 4), (30, 6), (50, 8), (80, 9)], 10)
        reasons.append(f"营收增速 {rg:+.1f}%")
        scores.append(s)
    if pg is not None:
        s, _ = band(pg, [(-10, 2), (10, 4.5), (30, 6.5), (60, 8), (100, 9)], 10)
        reasons.append(f"净利润增速 {pg:+.1f}%")
        scores.append(s)
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


def master_lowrisk(hist, fin):
    """低波风控大师 - 稳字当头: 低回撤+低波动+低杠杆"""
    reasons, scores = [], []
    off = hist.get("off_high_pct")
    yoy = hist.get("yoy_pct")
    dr = fin.get("debt_ratio")
    if off is not None:
        dd = -off
        if dd <= 12:
            s, d = 8.5, f"距高点仅 {dd:.0f}%, 回撤极小"
        elif dd <= 20:
            s, d = 7.0, f"回撤 {dd:.0f}%, 较稳健"
        elif dd <= 35:
            s, d = 5.0, f"回撤 {dd:.0f}%, 承压"
        elif dd <= 50:
            s, d = 3.0, f"回撤 {dd:.0f}%, 高风险"
        else:
            s, d = 1.5, f"深度回撤 {dd:.0f}%"
        reasons.append(d)
        scores.append(s)
    if yoy is not None:
        vol = abs(yoy)
        s, _ = band(vol, [(8, 8), (25, 6), (45, 4), (70, 2)], 1)
        reasons.append(f"年波动幅 {vol:.0f}%")
        scores.append(s)
    if dr is not None:
        s, _ = band(dr, [(30, 9), (50, 7), (65, 5), (80, 3)], 2)
        reasons.append(f"资产负债率 {dr:.1f}%")
        scores.append(s)
    score = round(sum(scores) / len(scores), 1) if scores else 5.0
    return score, reasons


MASTER_DEFS = [
    ("value",     "价值大师",     "经典价值 · 格雷厄姆",  0.10),
    ("quality",   "质量大师",     "好生意 · 巴菲特",      0.10),
    ("growth",    "成长大师",     "增长驱动 · 林奇",      0.08),
    ("dividend",  "红利大师",     "现金回报 · A股红利",   0.06),
    ("momentum",  "动量大师",     "趋势跟踪",            0.06),
    ("margin",    "安全边际大师", "估值分位 · 逆向",     0.06),
    ("fisher",    "费雪大师",     "成长质量 · 费雪",     0.05),
    ("munger",    "芒格大师",     "好生意好价格 · 芒格",  0.05),
    ("marks",     "马克斯大师",   "周期钟摆 · 马克斯",    0.05),
    ("soros",     "索罗斯大师",   "反身性 · 索罗斯",     0.04),
    ("dalio",     "达利欧大师",   "风险平价 · 达利欧",   0.04),
    ("li_lu",     "李录大师",     "能力圈 · 李录",       0.05),
    ("duan",      "段永平大师",   "本分 · 段永平",       0.05),
    ("zhang",     "张磊大师",     "长期主义 · 张磊",     0.05),
    ("dan_bin",   "但斌大师",     "伟大公司 · 但斌",     0.04),
    ("thiel",     "蒂尔大师",     "从0到1 · 蒂尔",       0.04),
    ("cathie",    "木头姐大师",   "颠覆创新 · 木头姐",   0.04),
    ("lowrisk",   "低波风控大师", "稳字当头 · 风控",     0.04),
]


def analyze(code):
    market, code = detect_market(code)

    with _cache_lock:
        ent = _cache.get(code)
        if ent and time.time() - ent[0] < CACHE_TTL:
            _save_history(ent[1])
            return ent[1]

    hist = get_history(code, market)
    val = get_valuation(code, market)
    fin = get_financials(code, market)
    div = get_dividend(code, market)

    masters = []
    funcs = {
        "value": lambda: master_value(val),
        "quality": lambda: master_quality(fin),
        "growth": lambda: master_growth(fin),
        "dividend": lambda: master_dividend(div, val),
        "momentum": lambda: master_momentum(hist),
        "margin": lambda: master_margin(val),
        "fisher": lambda: master_fisher(fin),
        "munger": lambda: master_munger(fin, val),
        "marks": lambda: master_marks(hist),
        "soros": lambda: master_soros(hist),
        "dalio": lambda: master_dalio(hist, fin),
        "li_lu": lambda: master_li_lu(fin, val),
        "duan": lambda: master_duan(fin),
        "zhang": lambda: master_zhang(fin, val),
        "dan_bin": lambda: master_dan_bin(fin),
        "thiel": lambda: master_thiel(fin),
        "cathie": lambda: master_cathie(fin),
        "lowrisk": lambda: master_lowrisk(hist, fin),
    }
    total, wsum = 0.0, 0.0
    dist = {"bullish": 0, "neutral": 0, "bearish": 0}
    for mid, mname, mschool, mweight in MASTER_DEFS:
        try:
            score, reasons = funcs[mid]()
        except Exception:
            score, reasons = 5.0, ["该维度数据获取异常, 按中性处理"]
        if score >= 7:
            verdict = "偏多"; dist["bullish"] += 1
        elif score >= 4:
            verdict = "中性"; dist["neutral"] += 1
        else:
            verdict = "偏空"; dist["bearish"] += 1
        masters.append({
            "id": mid, "name": mname, "school": mschool, "weight": mweight,
            "score": score, "verdict": verdict, "reasons": reasons,
        })
        total += score * mweight
        wsum += mweight

    consensus_score = round(total / wsum, 1) if wsum else 5.0
    if consensus_score >= 6.5:
        signal, sdesc = "买入倾向", "多数大师看好, 估值与基本面支撑较强"
    elif consensus_score >= 5:
        signal, sdesc = "持有偏多", "整体偏正面, 可持有观察"
    elif consensus_score >= 3.5:
        signal, sdesc = "持有", "观点分歧, 建议观望等待更好时机"
    else:
        signal, sdesc = "卖出倾向", "多数维度发出警示信号"

    # 置信度 = 1 - 分数离散度
    if len(masters) > 1:
        mean = consensus_score
        var = sum((m["score"] - mean) ** 2 for m in masters) / len(masters)
        confidence = round(max(0.3, min(0.95, 1 - (var ** 0.5) / 6)), 2)
    else:
        confidence = 0.5

    result = {
        "code": code,
        "name": _name_cache.get(code, code),
        "price": {k: hist.get(k) for k in ["price", "change_pct", "date", "ma20", "ma60", "high_52w", "low_52w", "yoy_pct"]},
        "valuation": val,
        "fundamentals": fin,
        "dividend": {"yield_pct": div.get("yield_pct"), "per_share": div.get("per_share")},
        "masters": masters,
        "consensus": {
            "score": consensus_score, "signal": signal, "description": sdesc,
            "confidence": confidence, "distribution": dist,
        },
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    with _cache_lock:
        _cache[code] = (time.time(), result)
    _save_history(result)
    return result


# ============ 历史记录 ============

HISTORY_FILE = BASE_DIR / "data" / "history.json"

def _save_history(result):
    """分析结果自动存档到 data/history.json"""
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        history = []
        if HISTORY_FILE.exists():
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        entry = {
            "code": result["code"],
            "name": result["name"],
            "score": result["consensus"]["score"],
            "signal": result["consensus"]["signal"],
            "price": result["price"].get("price"),
            "date": result["timestamp"][:10],
            "time": result["timestamp"],
            "confidence": result["consensus"]["confidence"],
            "distribution": result["consensus"]["distribution"],
            "masters": [{"id": m["id"], "name": m["name"], "score": m["score"]}
                        for m in result.get("masters", [])],
        }
        history.append(entry)
        # 保留最近 2000 条
        if len(history) > 2000:
            history = history[-2000:]
        HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def get_history_records(filters=None):
    """读取历史记录, 支持筛选"""
    if not HISTORY_FILE.exists():
        return []
    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    if filters:
        if filters.get("code"):
            history = [h for h in history if h["code"] == filters["code"]]
        if filters.get("signal"):
            history = [h for h in history if filters["signal"] in h["signal"]]
        if filters.get("date"):
            history = [h for h in history if h["date"] == filters["date"]]
    return list(reversed(history[-200:]))  # 最新200条, 倒序


# ============ 定时盘后分析 + Webhook 推送 ============

SETTINGS_FILE = BASE_DIR / "data" / "settings.json"

def load_settings():
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"cron_time": "18:00", "webhook_url": "", "watchlist": []}

def save_settings(s):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

_cron_timer = None
_cron_lock = threading.Lock()

def _send_webhook(url, results):
    """推送分析结果到飞书/企微 Webhook (参考 sequoia-x 飞书推送方式, 异动特别标注)"""
    if not url:
        return
    diff_count = sum(1 for r in results if r.get("_diff"))
    title = f"📊 StockMasters 大师共识报告 ({time.strftime('%m-%d %H:%M')})"
    if diff_count:
        title += f" · {diff_count} 只异动"
    lines = [title + "\n"]
    # 异动的股票优先展示
    results = sorted(results, key=lambda r: (0 if r.get("_diff") else 1, -(r.get("consensus", {}).get("score", 0))))
    groups = {"买入倾向": [], "持有偏多": [], "持有": [], "卖出倾向": []}
    for r in results:
        c = r.get("consensus", {})
        sig = c.get("signal", "持有")
        key = sig if sig in groups else "持有"
        groups[key].append(r)
    for sig, items in groups.items():
        if not items:
            continue
        mark = "🟢" if sig == "买入倾向" else "🟡" if sig in ("持有偏多", "持有") else "🔴"
        lines.append(f"{mark} 【{sig}】{len(items)} 只")
        for r in items:
            c = r.get("consensus", {})
            p = r.get("price", {})
            dist = c.get("distribution", {})
            flag = " ⚠️" if r.get("_diff") else ""
            lines.append(
                f"  {r.get('name','?')}({r.get('code','?')}) {c.get('score',0)}分{flag}\n"
                f"    价:{p.get('price','?')} 多{dist.get('bullish',0)}/中{dist.get('neutral',0)}/空{dist.get('bearish',0)} 置信{int(c.get('confidence',0)*100)}%"
            )
            if r.get("_diff"):
                lines.append(f"    {r['_diff']}")
    if diff_count:
        lines.append("\n⚠️ 异动 = 综合评分较上次变动 ≥1.0 分, 主因为分差最大的大师观点变化")
    payload = {"msg_type": "text", "content": {"text": "\n".join(lines)}}
    try:
        resp = _req.post(url, json=payload, timeout=15)
        resp.raise_for_status()
    except Exception:
        pass

def parse_codes(text):
    """解析自选股文本: 支持逗号/空格/换行/顿号分隔, 自动去重去非法, 返回 (codes, invalid)"""
    codes, seen = [], set()
    for token in re.split(r"[\s,，、;\n]+", text or ""):
        token = token.strip()
        m = re.match(r"^(\d{1,6})$", token)
        if m:
            code = m.group(1)
            if code not in seen:
                seen.add(code)
                codes.append(code)
    return codes


def _prev_record_for(code):
    """读取该代码最近一条历史记录 (须在 analyze 之前调用, 否则取到的是本次)"""
    try:
        if not HISTORY_FILE.exists():
            return None
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        for h in reversed(history):
            if h.get("code") == code and h.get("score") is not None:
                return h
    except Exception:
        pass
    return None


def _score_diff(prev, result):
    """对比上次与本次评分, 返回异动描述文本; 无明显异动返回 None.
    判定: 综合分差绝对值 >= 1.0 视为异动; 主因取分差最大的大师(>=0.5)."""
    if not prev:
        return None
    try:
        prev_score = float(prev.get("score"))
    except (TypeError, ValueError):
        return None
    curr_score = result["consensus"]["score"]
    delta = round(curr_score - prev_score, 1)
    if abs(delta) < 1.0:
        return None
    direction = "上调" if delta > 0 else "下调"
    desc = [f"综合分 {prev_score}→{curr_score} ({delta:+.1f})"]
    # 找出变化最大的大师作为主因
    prev_m = {m.get("id"): m.get("score") for m in prev.get("masters", []) if m.get("id")}
    best = None  # (abs差, 大师, 差值)
    for m in result.get("masters", []):
        ps = prev_m.get(m["id"])
        if ps is None:
            continue
        try:
            d = round(float(m["score"]) - float(ps), 1)
        except (TypeError, ValueError):
            continue
        if abs(d) >= 0.5 and (best is None or abs(d) > best[0]):
            best = (abs(d), m, d)
    if best:
        _, m, d = best
        reason = "；".join(str(x) for x in m.get("reasons", [])[:2])
        desc.append(f"主因: {m['name']} {d:+.1f}分 ({reason})" if reason else f"主因: {m['name']} {d:+.1f}分")
    return f"⚠️ 异动·{direction}: " + " | ".join(desc)


def _run_cron():
    """执行一次定时分析 (对比上次评分, 异动特别标注)"""
    s = load_settings()
    watchlist = s.get("watchlist", [])
    if not watchlist:
        return
    results = []
    for code in watchlist:
        try:
            prev = _prev_record_for(code)   # 先取上次记录 (analyze 会写入本次)
            r = analyze(code)
            r["_diff"] = _score_diff(prev, r)
            results.append(r)
            time.sleep(1)
        except Exception:
            pass
    if results:
        _send_webhook(s.get("webhook_url", ""), results)

def schedule_cron():
    """根据设置重新调度定时任务"""
    global _cron_timer
    with _cron_lock:
        if _cron_timer:
            _cron_timer.cancel()
            _cron_timer = None
        s = load_settings()
        cron_time = s.get("cron_time", "18:00")
        try:
            hour, minute = map(int, cron_time.split(":"))
        except Exception:
            return
        now = time.time()
        # 计算今天剩余秒数到目标时间
        import datetime
        target = datetime.datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            target = target + datetime.timedelta(days=1)
        delay = target.timestamp() - now
        def _cron_loop():
            while True:
                _run_cron()
                # 下一天同一时间
                time.sleep(86400)
        _cron_timer = threading.Timer(delay, _cron_loop)
        _cron_timer.daemon = True
        _cron_timer.start()


# ============ HTTP 服务 ============

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            fp = BASE_DIR / "index.html"
            if fp.exists():
                self._send(fp.read_text(encoding="utf-8"))
            else:
                self._send("<h3>index.html 缺失</h3>", code=500)

        elif parsed.path == "/api/analyze":
            qs = parse_qs(parsed.query)
            code = (qs.get("code") or [""])[0].strip()
            if not re.fullmatch(r"\d{1,6}", code):
                self._send(json.dumps({"error": "请输入股票代码: A股600900 / B股200596 / 港股00700"},
                                     ensure_ascii=False),
                          "application/json; charset=utf-8", 400)
                return
            try:
                self._send(json.dumps(analyze(code), ensure_ascii=False),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(json.dumps({"error": f"分析失败: {str(e)[:200]}"},
                                      ensure_ascii=False),
                           "application/json; charset=utf-8", 502)

        elif parsed.path == "/api/history":
            qs = parse_qs(parsed.query)
            filters = {}
            if qs.get("code"): filters["code"] = qs["code"][0]
            if qs.get("signal"): filters["signal"] = qs["signal"][0]
            if qs.get("date"): filters["date"] = qs["date"][0]
            self._send(json.dumps(get_history_records(filters), ensure_ascii=False),
                       "application/json; charset=utf-8")

        elif parsed.path == "/api/settings":
            self._send(json.dumps(load_settings(), ensure_ascii=False),
                       "application/json; charset=utf-8")

        elif parsed.path == "/api/cron-status":
            s = load_settings()
            self._send(json.dumps({
                "cron_time": s.get("cron_time", ""),
                "webhook_url": s.get("webhook_url", ""),
                "watchlist_count": len(s.get("watchlist", [])),
                "timer_active": _cron_timer is not None and _cron_timer.is_alive(),
            }, ensure_ascii=False), "application/json; charset=utf-8")

        elif parsed.path == "/api/cron-run":
            threading.Thread(target=_run_cron, daemon=True).start()
            self._send(json.dumps({"status": "started"}, ensure_ascii=False),
                       "application/json; charset=utf-8")

        else:
            self._send("Not Found", code=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"

        if parsed.path == "/api/settings":
            try:
                data = json.loads(body)
                save_settings(data)
                schedule_cron()
                self._send(json.dumps({"status": "ok"}, ensure_ascii=False),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(json.dumps({"error": str(e)}, ensure_ascii=False),
                           "application/json; charset=utf-8", 400)

        elif parsed.path == "/api/watchlist/import":
            try:
                data = json.loads(body)
                text = data.get("text", "")
                codes = parse_codes(text)
                if not codes:
                    self._send(json.dumps({"error": "未识别到有效股票代码"}, ensure_ascii=False),
                               "application/json; charset=utf-8", 400)
                    return
                s = load_settings()
                old = s.get("watchlist", [])
                merged = list(dict.fromkeys(old + codes))
                if len(merged) > 100:
                    self._send(json.dumps({"error": "自选股最多 100 只"}, ensure_ascii=False),
                               "application/json; charset=utf-8", 400)
                    return
                s["watchlist"] = merged
                save_settings(s)
                schedule_cron()
                self._send(json.dumps({
                    "status": "ok",
                    "imported": len(merged) - len(old),
                    "total": len(merged),
                    "watchlist": merged,
                }, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                self._send(json.dumps({"error": str(e)}, ensure_ascii=False),
                           "application/json; charset=utf-8", 400)

        elif parsed.path == "/api/batch":
            try:
                data = json.loads(body)
                codes = data.get("codes", [])
                if not codes or len(codes) > 20:
                    self._send(json.dumps({"error": "请提供1-20个股票代码"},
                                         ensure_ascii=False),
                               "application/json; charset=utf-8", 400)
                    return
                results = []
                for code in codes:
                    try:
                        r = analyze(code)
                        results.append({
                            "code": r["code"], "name": r["name"],
                            "score": r["consensus"]["score"],
                            "signal": r["consensus"]["signal"],
                            "price": r["price"].get("price"),
                            "confidence": r["consensus"]["confidence"],
                            "distribution": r["consensus"]["distribution"],
                        })
                    except Exception as e:
                        results.append({"code": code, "error": str(e)[:100]})
                    time.sleep(0.5)
                self._send(json.dumps(results, ensure_ascii=False),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(json.dumps({"error": str(e)}, ensure_ascii=False),
                           "application/json; charset=utf-8", 400)
        else:
            self._send("Not Found", code=404)


def main():
    schedule_cron()  # 启动时恢复定时任务
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"StockMasters A股/B股/港股大师分析系统")
    print(f"访问: http://localhost:{PORT}")
    print(f"数据源: 东财/同花顺/新浪/百度估值 (akshare, 国内直连)")
    print(f"Ctrl+C 停止服务")
    server.serve_forever()


if __name__ == "__main__":
    main()
