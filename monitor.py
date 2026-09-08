#!/usr/bin/env python3
"""4013 分析团队 + 实时监控
3个子Agent:
  1. 盘内Agent — 监控开盘市场的趋势变化、放量、转折
  2. 盘外Agent — 监控未开盘市场的期货/相关性背离
  3. 监控Agent — 监控程序健康、数据源状态

核心业务逻辑:
  - 跟踪全球3时区接力(亚洲→欧洲→北美)
  - 找资金多空关系(TRS组合)
  - 利率前提: 利率异常+下降段=趋势起点, 利率异常+上升段=异常监控
  - 放量三源确认: 板块放量+相关期货放量+相关板块放量
"""
import json, time, urllib.request, sys, os
from datetime import datetime, timezone, timedelta
from pathlib import Path

HKT = timezone(timedelta(hours=8))
EDT = timezone(timedelta(hours=-4))
JST = timezone(timedelta(hours=9))
KST = timezone(timedelta(hours=9))
CET = timezone(timedelta(hours=2))

BASE_4002 = "http://192.168.25.144:4002"
BASE_3422 = "http://192.168.25.134:3422"
BASE_3400 = "http://192.168.25.134:3400"
BASE_3402 = "http://192.168.25.134:3402"
BASE_3404 = "http://192.168.25.134:3404"
BASE_5050 = "http://192.168.25.134:5050"

# ========== Trading Sessions (HKT reference) ==========
SESSIONS = {
    "CN": {"name": "A股", "sessions": [[9, 30, 11, 30], [13, 0, 15, 0]], "tz": 8},
    "HK": {"name": "港股", "sessions": [[9, 30, 12, 0], [13, 0, 16, 0]], "tz": 8},
    "JP": {"name": "日经", "sessions": [[8, 0, 10, 30], [11, 30, 14, 0]], "tz": 9},
    "KR": {"name": "KOSPI", "sessions": [[8, 0, 14, 30]], "tz": 9},
    "US": {"name": "美股", "sessions": [[21, 30, 24, 0], [0, 0, 4, 0]], "tz": -4},
    "EU": {"name": "欧股", "sessions": [[15, 0, 19, 30]], "tz": 2},
}

# ========== Product Definitions ==========
# 锚点: 半导体是核心
ANCHORS = {
    "US": {"first": "SOXX", "second": "NASDAQ:NQUSL10101015"},
    "CN": {"first": "SZSE:980017", "second": "SSE:000915"},
    "HK": {"first": "HSI:HSCASEMI", "second": "HSCI.IT"},
    "JP": {"first": "NIKKEI", "second": "dynamic"},  # 动态
    "KR": {"first": "KOSPI200IT", "second": "KOSPI200INDUS"},
    "EU": {"first": "DAX", "second": "dynamic"},
}

# 关联产品映射 (板块→商品 1:1)
SECTOR_COMMODITY = {
    "semiconductor": ["CFDGOLD", "CFDSILVER", "COPPER"],  # 贵金属↔半导体
    "industrial": ["CL", "LCO", "SC0"],  # 石油↔工业(负相关)
    "finance": ["USDX_FX", "EUR_FX", "GBP_FX", "JPY_FX"],  # 汇率↔金融
    "consumer": ["BTC_SPOT", "ETH_SPOT"],  # CRYPTO↔可选消费
    "bond": ["US_BOND", "EURO_BOND", "JP_BOND", "CN_BOND"],  # 债券
}

# DXY反向规则 (非美货币与DXY相反)
DXY_INVERSE = ["EUR_FX", "GBP_FX", "JPY_FX", "CNH_FX", "AUD_FX"]
# DXY对商品: 黄金/BTC=正相关, 石油=负相关
DXY_COMMODITY_CORR = {
    "CFDGOLD": "positive", "BTC_SPOT": "positive",
    "CL": "negative", "US_BOND": "positive",
}

# ========== Data Fetching ==========
def fetch_json(url, timeout=10):
    """Fetch JSON from URL, return dict or None on error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}

def fetch_4002_bars(symbol, resolution="1", days=3):
    """Fetch K-line data from 4002."""
    now = int(time.time())
    frm = now - days * 86400
    url = f"{BASE_4002}/history?symbol={symbol}&resolution={resolution}&from={frm}&to={now}"
    return fetch_json(url)

def fetch_bond_hedge(days=7):
    """Fetch bond-hedge data from 3422 via proxy or direct."""
    # Try 3403 proxy first
    data = fetch_json(f"http://192.168.25.134:4013/api/bond-hedge")
    if data and not data.get("error"):
        return data
    # Fallback to 3422 direct
    return fetch_json(f"{BASE_3422}/api/hedge_trs?days={days}")

def fetch_classify():
    """Fetch 3400 classify (6-state correlation)."""
    return fetch_json(f"{BASE_3400}/api/continuation/classify")

def fetch_volume_surge(market="cn"):
    """Fetch 3402 volume surge data."""
    return fetch_json(f"{BASE_3402}/api/volume-surge?market={market}")

def fetch_futures_trends():
    """Fetch 3404 futures trends."""
    return fetch_json(f"{BASE_3404}/api/trends")

# ========== Market Session Detection ==========
def get_local_time(tz_offset):
    """Get current time in specified timezone (hours offset from UTC)."""
    utc = datetime.now(timezone.utc)
    return utc + timedelta(hours=tz_offset)

def is_market_open(market_key):
    """Check if market is currently open."""
    rule = SESSIONS.get(market_key)
    if not rule:
        return {"open": False, "status": "unknown"}
    local = get_local_time(rule["tz"])
    h, m = local.hour, local.minute
    hm = h * 60 + m
    for sH, sM, eH, eM in rule["sessions"]:
        start = sH * 60 + sM
        end = eH * 60 + eM
        if end <= start:
            end += 24 * 60
        check = hm
        if hm < start and end > 24 * 60:
            check = hm + 24 * 60
        if check >= start and check < end:
            return {"open": True, "status": "open", "local_time": local.strftime("%H:%M")}
    for sess in rule["sessions"]:
        start = sess[0] * 60 + sess[1]
        if hm >= start - 30 and hm < start:
            return {"open": False, "status": "pre", "local_time": local.strftime("%H:%M")}
    return {"open": False, "status": "closed", "local_time": local.strftime("%H:%M")}

def get_all_market_status():
    """Get status of all markets."""
    result = {}
    for key in SESSIONS:
        result[key] = is_market_open(key)
    return result

# ========== Analysis: Intraday Agent ==========
def analyze_intraday():
    """盘内Agent: 分析当前开盘市场的趋势、放量、转折。
    
    核心逻辑:
    1. 检查哪些市场开盘
    2. 获取开盘市场的K线数据
    3. 检测放量(1m volume surge)
    4. 检测趋势方向变化
    5. 检查利率前提(债券方向)
    """
    status = get_all_market_status()
    open_markets = [k for k, v in status.items() if v["open"]]
    
    result = {
        "agent": "intraday",
        "timestamp": datetime.now(HKT).isoformat(),
        "open_markets": open_markets,
        "market_status": status,
        "alerts": [],
        "trends": {},
    }
    
    # 获取债券状态(利率前提)
    bond_data = fetch_bond_hedge(days=1)
    if bond_data and not bond_data.get("error"):
        bonds = bond_data.get("bonds", {})
        for bond_key, bond_info in bonds.items():
            markers = bond_info.get("markers", [])
            if markers:
                latest = markers[-1]
                result["trends"][bond_key] = {
                    "action": latest.get("action", ""),
                    "direction": latest.get("direction", ""),
                    "bond_slope": latest.get("bond_slope", 0),
                    "fx_slope": latest.get("fx_slope", 0),
                }
                # 利率异常+下降段 = 趋势起点(4010)
                if latest.get("action") == "打開敞口" and latest.get("bond_slope", 0) > 0:
                    result["alerts"].append({
                        "type": "trend_origin_signal",
                        "bond": bond_key,
                        "msg": f"利率下降段+打开敞口: {bond_key} 利率异常+下降→趋势起点信号",
                        "severity": "high",
                    })
                # 利率异常+上升段 = 异常监控(4012)
                elif latest.get("action") == "建立對沖" and latest.get("bond_slope", 0) < 0:
                    result["alerts"].append({
                        "type": "anomaly_signal",
                        "bond": bond_key,
                        "msg": f"利率上升段+建立对冲: {bond_key} 利率异常+上升→异常监控",
                        "severity": "medium",
                    })
    
    # 检查各开盘市场的放量
    for market in open_markets:
        if market == "US":
            symbol = "SOXX"
        elif market == "CN":
            symbol = "SZSE:980017"
        elif market == "HK":
            symbol = "HSI:HSCASEMI"
        elif market == "JP":
            symbol = "NIKKEI"
        elif market == "KR":
            symbol = "KOSPI"
        else:
            continue
        
        bars = fetch_4002_bars(symbol, resolution="1", days=1)
        if bars and bars.get("s") == "ok" and bars.get("t"):
            t_arr = bars["t"]
            v_arr = bars.get("v", [])
            c_arr = bars.get("c", [])
            
            if len(v_arr) >= 30:
                # 计算30根bar的平均量
                avg_vol = sum(v_arr[-30:]) / 30
                latest_vol = v_arr[-1] if v_arr else 0
                vol_ratio = latest_vol / avg_vol if avg_vol > 0 else 0
                
                # 趋势方向(最近5根)
                if len(c_arr) >= 5:
                    trend = "up" if c_arr[-1] > c_arr[-5] else "down" if c_arr[-1] < c_arr[-5] else "flat"
                else:
                    trend = "unknown"
                
                result["trends"][market] = {
                    "symbol": symbol,
                    "vol_ratio": round(vol_ratio, 2),
                    "trend": trend,
                    "bars_count": len(t_arr),
                    "latest_close": c_arr[-1] if c_arr else 0,
                }
                
                # 放量检测
                if vol_ratio >= 1.5:
                    result["alerts"].append({
                        "type": "volume_surge",
                        "market": market,
                        "symbol": symbol,
                        "vol_ratio": round(vol_ratio, 2),
                        "trend": trend,
                        "msg": f"{SESSIONS[market]['name']} 放量 {vol_ratio:.1f}x, 趋势{trend}",
                        "severity": "high" if vol_ratio >= 3.0 else "medium",
                    })
    
    return result

# ========== Analysis: Off-hours Agent ==========
def analyze_offhours():
    """盘外Agent: 分析未开盘市场的期货相关性、DXY背离、资金行为。
    
    核心逻辑:
    1. 检查哪些市场未开盘
    2. 获取DXY/黄金/BTC/原油的期货数据
    3. 计算DXY与各商品的相关性
    4. 检测背离(DXY↑+黄金↑=背离, 应该是反向)
    5. 获取3422债券行为(敞口/对冲)
    """
    status = get_all_market_status()
    closed_markets = [k for k, v in status.items() if not v["open"]]
    
    result = {
        "agent": "offhours",
        "timestamp": datetime.now(HKT).isoformat(),
        "closed_markets": closed_markets,
        "market_status": status,
        "alerts": [],
        "correlations": {},
        "bond_behavior": {},
    }
    
    # 获取期货数据
    futures = {
        "DXY": "USDX_FX",
        "Gold": "CFDGOLD",
        "BTC": "BTC_SPOT",
        "Oil": "CL",
    }
    futures_data = {}
    for name, symbol in futures.items():
        bars = fetch_4002_bars(symbol, resolution="1", days=2)
        if bars and bars.get("s") == "ok" and bars.get("t"):
            futures_data[name] = {
                "times": bars["t"],
                "closes": bars["c"],
                "volumes": bars.get("v", []),
            }
    
    # 计算DXY与各商品的相关性
    if "DXY" in futures_data and futures_data["DXY"]["closes"]:
        dxy_closes = futures_data["DXY"]["closes"]
        dxy_len = len(dxy_closes)
        
        for commodity_name in ["Gold", "BTC", "Oil"]:
            if commodity_name in futures_data and futures_data[commodity_name]["closes"]:
                comp_closes = futures_data[commodity_name]["closes"]
                # 对齐长度
                min_len = min(dxy_len, len(comp_closes))
                if min_len < 10:
                    continue
                dxy_recent = dxy_closes[-min_len:]
                comp_recent = comp_closes[-min_len:]
                
                # 简单相关系数
                n = len(dxy_recent)
                mx = sum(dxy_recent) / n
                my = sum(comp_recent) / n
                num = sum((dxy_recent[i] - mx) * (comp_recent[i] - my) for i in range(n))
                dx = sum((dxy_recent[i] - mx) ** 2 for i in range(n))
                dy = sum((comp_recent[i] - my) ** 2 for i in range(n))
                corr = num / (dx * dy) ** 0.5 if dx > 0 and dy > 0 else 0
                
                # 趋势方向(最近5根)
                dxy_dir = dxy_recent[-1] - dxy_recent[-5] if len(dxy_recent) >= 5 else 0
                comp_dir = comp_recent[-1] - comp_recent[-5] if len(comp_recent) >= 5 else 0
                
                # 背离检测: 两者同向移动(应该反向)
                is_divergence = (dxy_dir > 0 and comp_dir > 0) or (dxy_dir < 0 and comp_dir < 0)
                
                expected_corr = DXY_COMMODITY_CORR.get(commodity_name, "unknown")
                
                result["correlations"][f"DXY×{commodity_name}"] = {
                    "correlation": round(corr, 4),
                    "dxy_direction": "up" if dxy_dir > 0 else "down" if dxy_dir < 0 else "flat",
                    "commodity_direction": "up" if comp_dir > 0 else "down" if comp_dir < 0 else "flat",
                    "is_divergence": is_divergence,
                    "expected": expected_corr,
                    "bars": min_len,
                }
                
                if is_divergence:
                    result["alerts"].append({
                        "type": "divergence",
                        "pair": f"DXY×{commodity_name}",
                        "corr": round(corr, 4),
                        "msg": f"DXY与{commodity_name}背离: DXY{'↑' if dxy_dir>0 else '↓'}, {commodity_name}{'↑' if comp_dir>0 else '↓'}",
                        "severity": "high",
                    })
    
    # 获取3422债券行为
    bond_data = fetch_bond_hedge(days=2)
    if bond_data and not bond_data.get("error"):
        bonds = bond_data.get("bonds", {})
        for bond_key, bond_info in bonds.items():
            markers = bond_info.get("markers", [])
            if markers:
                latest = markers[-1]
                result["bond_behavior"][bond_key] = {
                    "action": latest.get("action", ""),
                    "type": latest.get("type", ""),
                    "direction": latest.get("direction", ""),
                    "fx_dir": latest.get("fx_dir", ""),
                    "bond_slope": latest.get("bond_slope", 0),
                    "fx_slope": latest.get("fx_slope", 0),
                    "time": latest.get("time", 0),
                }
                
                # 资金行为分析: 债涨+汇率跌=打开敞口(资金流入)
                if latest.get("type") == "trs_off":
                    result["alerts"].append({
                        "type": "capital_flow",
                        "bond": bond_key,
                        "msg": f"{bond_key} 打开敞口: 债涨+汇率看空→资金流入信号",
                        "severity": "medium",
                    })
                # 债跌+汇率涨=建立对冲(资金流出)
                elif latest.get("type") == "hedge_on":
                    result["alerts"].append({
                        "type": "capital_flow",
                        "bond": bond_key,
                        "msg": f"{bond_key} 建立对冲: 债跌+汇率看多→资金流出信号",
                        "severity": "medium",
                    })
    
    # 跨市场趋势接力分析
    # 检查已收盘市场是否通过期货维持趋势
    for market in closed_markets:
        if market in ["CN", "HK", "JP", "KR"]:
            # 亚洲市场已收盘,检查相关期货
            if "DXY" in futures_data and futures_data["DXY"]["closes"]:
                dxy_trend = "up" if futures_data["DXY"]["closes"][-1] > futures_data["DXY"]["closes"][-5] else "down"
                result["alerts"].append({
                    "type": "relay",
                    "market": market,
                    "msg": f"{SESSIONS[market]['name']}已收盘, DXY期货趋势{dxy_trend}→通过期货接力追踪",
                    "severity": "info",
                })
    
    return result

# ========== Analysis: Monitor Agent ==========
def analyze_health():
    """监控Agent: 检查程序健康、数据源状态。"""
    result = {
        "agent": "health",
        "timestamp": datetime.now(HKT).isoformat(),
        "services": {},
        "alerts": [],
    }
    
    # 检查4002
    r = fetch_json(f"{BASE_4002}/time")
    result["services"]["4002"] = {
        "status": "ok" if r and not isinstance(r, dict) or (isinstance(r, dict) and not r.get("error")) else "error",
        "time": r if isinstance(r, (int, float)) else r.get("t") if isinstance(r, dict) else None,
    }
    if not r or (isinstance(r, dict) and r.get("error")):
        result["alerts"].append({"type": "service_down", "service": "4002", "severity": "critical"})
    
    # 检查3422
    r = fetch_json(f"{BASE_3422}/api/state")
    result["services"]["3422"] = {
        "status": "ok" if r and not r.get("error") else "error",
    }
    if not r or r.get("error"):
        result["alerts"].append({"type": "service_down", "service": "3422", "severity": "critical"})
    
    # 检查3400
    r = fetch_json(f"{BASE_3400}/api/continuation/markets")
    result["services"]["3400"] = {
        "status": "ok" if r and not r.get("error") else "error",
    }
    if not r or r.get("error"):
        result["alerts"].append({"type": "service_down", "service": "3400", "severity": "high"})
    
    # 检查3402
    r = fetch_json(f"{BASE_3402}/api/volume-surge?market=cn")
    result["services"]["3402"] = {
        "status": "ok" if r and not r.get("error") else "error",
    }
    if not r or r.get("error"):
        result["alerts"].append({"type": "service_down", "service": "3402", "severity": "high"})
    
    # 检查5050
    r = fetch_json(f"{BASE_5050}/status")
    result["services"]["5050"] = {
        "status": "ok" if r and "pools" in r else "error",
    }
    if not r or r.get("error"):
        result["alerts"].append({"type": "service_down", "service": "5050", "severity": "high"})
    
    return result

# ========== Main: Run All Agents ==========
def run_analysis():
    """运行全部3个Agent,输出分析结果。"""
    print(json.dumps({
        "type": "analysis_start",
        "timestamp": datetime.now(HKT).isoformat(),
    }, ensure_ascii=False))
    
    # Agent 1: 盘内分析
    intraday = analyze_intraday()
    print(json.dumps(intraday, ensure_ascii=False))
    
    # Agent 2: 盘外分析
    offhours = analyze_offhours()
    print(json.dumps(offhours, ensure_ascii=False))
    
    # Agent 3: 健康监控
    health = analyze_health()
    print(json.dumps(health, ensure_ascii=False))
    
    # 汇总
    all_alerts = intraday["alerts"] + offhours["alerts"] + health["alerts"]
    summary = {
        "type": "analysis_summary",
        "timestamp": datetime.now(HKT).isoformat(),
        "total_alerts": len(all_alerts),
        "high_severity": len([a for a in all_alerts if a.get("severity") == "high"]),
        "open_markets": intraday["open_markets"],
        "closed_markets": offhours["closed_markets"],
        "correlations": offhours["correlations"],
        "bond_behavior": offhours["bond_behavior"],
        "alerts": all_alerts,
    }
    print(json.dumps(summary, ensure_ascii=False))
    
    return summary

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--loop":
        # 持续监控模式
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        print(f"Starting 4013 monitor loop (interval={interval}s)")
        while True:
            try:
                run_analysis()
            except Exception as e:
                print(json.dumps({"type": "error", "msg": str(e)}, ensure_ascii=False))
            time.sleep(interval)
    else:
        run_analysis()
