#!/usr/bin/env python3
"""
币安合约AI全自动交易系统
- 智能评分: 趋势/量价/支撑/动量/情绪 五维整数累加评分
- 自动止盈止损: tp/hold/sl 三方向比较决策
- 市场扫描: 前20名涨幅币种评分排序
"""
import json, subprocess, os, sys, time, hashlib, hmac, urllib.request
from datetime import datetime
from collections import Counter

PROXY = "socks5://127.0.0.1:7891"
CURLE = ["curl", "-s", "--connect-timeout", "5", "--max-time", "10", "--socks5-hostname", "127.0.0.1:7891"]

def _load_keys():
    with open(os.path.expanduser("~/.binance/trading")) as f:
        lines = f.read().strip().split("\n")
        api = {}
        for l in lines:
            if "=" in l:
                k,v = l.split("=",1)
                api[k.strip()] = v.strip()
        return api["api-key"], api["api-secret"]

API_KEY, SECRET = _load_keys()

def sign_request(params_str):
    return hmac.new(SECRET.encode(), params_str.encode(), hashlib.sha256).hexdigest()

def _curl_auth(method, path, data_str=None):
    """Use curl+SOCKS5 for authenticated Binance API calls (urllib doesn't support SOCKS5)."""
    ts = int(time.time()*1000)
    if data_str:
        qs = f"{data_str}&timestamp={ts}&recvWindow=50000"
    else:
        qs = f"timestamp={ts}&recvWindow=50000"
    sig = sign_request(qs)
    url = f"https://fapi.binance.com{path}?{qs}&signature={sig}"
    cmd = CURLE + ["-H", f"X-MBX-APIKEY: {API_KEY}"]
    if method == "POST":
        cmd += ["-X", "POST"]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.stdout.strip():
            return json.loads(r.stdout)
        return {"error": f"Empty response: {r.stderr[:200]}"}
    except Exception as e:
        return {"error": str(e)}

def api_get(path, params=None):
    return _curl_auth("GET", path, params)

def api_post(path, data_str):
    return _curl_auth("POST", path, data_str)

def curl_get(url):
    try:
        r = subprocess.run(CURLE + [url], capture_output=True, text=True, timeout=15)
        if r.stdout.strip(): return json.loads(r.stdout)
    except: pass
    return None

def get_account():
    data = api_get("/fapi/v2/account")
    if "error" in data: return None, None
    balances = {}
    for a in data.get("assets", []):
        wb = float(a.get("walletBalance", 0))
        up = float(a.get("crossUnPnl", 0))
        if wb > 0 or up != 0:
            balances[a["asset"]] = {"total": wb+up, "wallet": wb, "pnl": up, "available": float(a.get("availableBalance", 0))}
    positions = []
    for p in data.get("positions", []):
        if float(p.get("positionAmt", 0)) != 0:
            positions.append({"symbol": p["symbol"], "side": p["positionSide"], "amt": float(p["positionAmt"]), "entry": float(p["entryPrice"]), "leverage": int(p["leverage"]), "pnl": float(p.get("unrealizedProfit", 0))})
    return balances, positions

def get_mark_price(symbol):
    data = curl_get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}")
    return float(data["price"]) if data else None

def get_klines(symbol, interval="5m", limit=20):
    data = curl_get(f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}")
    if data:
        return [{"time": k[0]/1000, "open": float(k[1]), "high": float(k[2]), "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])} for k in data]
    return []

def analyze_position(symbol, entry_price, amt):
    """
    五维智能评分: 趋势/量价/支撑/动量/情绪
    输出整数累加评分 ⭐{tp}/{hold}/{sl}
    """
    mark = get_mark_price(symbol)
    if not mark: return ("hold", "无法获取价格", 0.5, {})
    pnl_pct = (mark - entry_price) / entry_price * 100 if amt > 0 else (entry_price - mark) / entry_price * 100
    t = curl_get(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}")
    if isinstance(t, dict):
        high_24h = float(t.get('highPrice', mark))
        low_24h = float(t.get('lowPrice', mark))
        chg_24h = float(t.get('priceChangePercent', 0))
    else:
        high_24h = low_24h = mark
        chg_24h = 0
    pullback = (high_24h - mark) / high_24h * 100 if high_24h > 0 else 0
    funding = curl_get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}")
    fr = float(funding['lastFundingRate']) * 100 if isinstance(funding, dict) and funding.get('lastFundingRate') else 0
    k5 = get_klines(symbol, "5m", 24)
    k15 = get_klines(symbol, "15m", 12)
    k1h = get_klines(symbol, "1h", 8)

    scores = {"tp": 0, "hold": 0, "sl": 0}
    rtp, rhd, rsl = [], [], []

    # Default values for trend variables (may be set inside k5/k15/k1h block)
    c5_t, c15_t, h1h_t, big_trend = 0, 0, 0, 0

    if k5 and k15 and k1h:
        c5 = [(c["close"]-c["open"])/c["open"]*100 for c in k5[-5:]]
        c5_t = (k5[-1]["close"]-k5[-5]["close"])/k5[-5]["close"]*100 if k5[-5]["close"] else 0
        c15 = [(c["close"]-c["open"])/c["open"]*100 for c in k15[-4:]]
        c15_t = (k15[-1]["close"]-k15[-4]["close"])/k15[-4]["close"]*100 if k15[-4]["close"] else 0
        h1h_t = (k1h[-1]["close"]-k1h[-4]["close"])/k1h[-4]["close"]*100 if k1h[-4]["close"] else 0

        # 大周期趋势判断
        k4h_pos = get_klines(symbol, "4h", 3)
        h4h_t = 0
        if k4h_pos and len(k4h_pos) >= 2:
            h4h_t = (k4h_pos[-1]["close"] - k4h_pos[-2]["close"]) / k4h_pos[-2]["close"] * 100 if k4h_pos[-2]["close"] else 0
        big_trend = h1h_t + h4h_t

    # 大周期持续下跌防守：1h连续阴线检测 + 15m连续下跌加速（2026-05-31升级）
    # 不管HLD/T分数多少，只要连续阴线达标就强制触发sl
    if k1h and len(k1h) >= 8:
        cons_1h_down = 0
        for c in reversed(k1h[-8:]):
            if c["close"] < c["open"]:
                cons_1h_down += 1
            else:
                break
        if cons_1h_down >= 6:
            scores["sl"] += 25; rsl.append(f"1h连{cons_1h_down}阴-趋势加速下跌")
        elif cons_1h_down >= 4:
            scores["sl"] += 12; rsl.append(f"1h连{cons_1h_down}阴-持续弱势")
        # 新增：1小时连3阴+15m也向下→轻度警告
        if cons_1h_down >= 3:
            # 再加15m连续下跌检查
            k15_for_check = get_klines(symbol, "15m", 6)
            if k15_for_check and len(k15_for_check) >= 4:
                cons_15m_down = sum(1 for c in k15_for_check[-4:] if c["close"] < c["open"])
                if cons_15m_down >= 3:
                    scores["sl"] += 8; rsl.append(f"15m连{cons_15m_down}跌")

    # 压力位相关变量的默认值（可能被if k5 and len(k5) >= 6:块内覆盖）
    near_resist = mark
    resist_stuck = False
    touches = 0

    # 新增：15m连续阴线单独检测（2026-05-31）- 短期加速下跌
    # 如果最近4根15m K线有≥3根阴线且最后一根也收阴，直接触发止损
    if k5 and len(k5) >= 6:
        k5_recent = k5[-6:]
        cons_5m_down = 0
        for c in reversed(k5_recent):
            if c["close"] < c["open"]:
                cons_5m_down += 1
            else:
                break
        if cons_5m_down >= 5:
            scores["sl"] += 15; rsl.append(f"5m连{cons_5m_down}阴-加速下跌")

        # 压力位判断：用15m K线看近端压力和冲击次数
        k15_h26 = sorted([c["high"] for c in k15], reverse=True)
        near_resist = k15_h26[0] if len(k15_h26) > 0 else mark  # 15m周期最高点
        # 看价格是否在压力位附近（2%以内）
        d2_resist = (near_resist - mark) / mark * 100 if mark > 0 else 999
        # 看最近12根15m有多少根的高点接近当前压力位（冲击次数）
        touches = sum(1 for c in k15 if abs(c["high"] - near_resist) / near_resist < 0.01)
        # 如果多次冲击压力位不破，且价格在压力位附近，考虑止盈
        resist_stuck = d2_resist < 2 and touches >= 2 and c15_t <= 0

        # K线级别的回调判断指标（不依赖硬数值）：
        # 下影线 = 有买盘承接，回调健康
        # 量比 = 缩量回调健康，放量下跌危险
        # 支撑位距离 = 接近前低/均线可能有支撑
        # 连续阴线数 = 连跌过多趋势可能走坏
        # 以下在每个打分项中直接用这些指标判断，不设硬阈值

        # ① 趋势
        up = sum(1 for v in [c5_t, c15_t, h1h_t] if v > 0)
        dn = sum(1 for v in [c5_t, c15_t, h1h_t] if v < 0)
        if up >= 2: scores["hold"] += 12; rhd.append(f"共振向上({c5_t:+.1f}%/{c15_t:+.1f}%/{h1h_t:+.1f}%)")
        elif dn >= 2:
            # 共振向下的情况下，用K线特征判断是回调还是走坏
            # 看15m最后K线有没有下影线（买盘承接）
            last15 = k15[-1]
            body15 = abs(last15["close"] - last15["open"])
            wick15 = min(last15["open"], last15["close"]) - last15["low"]
            has_lower_wick = body15 > 0 and wick15 > body15 * 1.2

            # 看是否缩量（量比<1）
            lv = k15[-1]["volume"]
            av = sum(c["volume"] for c in k15) / len(k15)
            vol_shrink = lv < av

            # 看是否接近15m支撑位
            k15_lows = sorted(c["low"] for c in k15)
            recent_low = k15_lows[0] if k15_lows else 0
            d2_support = (mark - recent_low) / mark * 100 if mark > 0 else 999

            # 看大周期是否还在向上
            big_ok = big_trend > 0

            # 正常回调特征：有下影线 + 缩量 + 近支撑 + 大周期向上
            pullback_signals = sum([has_lower_wick, vol_shrink, d2_support < 3, big_ok])
            if pullback_signals >= 3:
                scores["hold"] += 6
                reasons_dn = []
                if has_lower_wick: reasons_dn.append("下影线")
                if vol_shrink: reasons_dn.append("缩量")
                if d2_support < 3: reasons_dn.append(f"近支撑({d2_support:.1f}%)")
                if big_ok: reasons_dn.append(f"大周期{big_trend:+.1f}%")
                rhd.append("回调(" + "+".join(reasons_dn) + ")")
            elif pullback_signals >= 2:
                scores["hold"] += 3
                rhd.append("偏弱观望")
            else:
                scores["tp"] += 10; rtp.append(f"趋势走弱({c5_t:+.1f}%/{c15_t:+.1f}%/{h1h_t:+.1f}%)")
        if len(k15) >= 4:
            rg = [(k15[i]["high"]-k15[i]["low"])/k15[i]["low"]*100 for i in range(-4,-1)]
            if rg[-1] < rg[-2]*0.6:
                scores["hold"] += 5; rhd.append("波幅收敛")
            elif rg[-1] > rg[-2]*1.8:
                # 波幅扩张：看方向特征
                last15 = k15[-1]
                body15 = abs(last15["close"] - last15["open"])
                wick15 = min(last15["open"], last15["close"]) - last15["low"]
                has_lower_wick = body15 > 0 and wick15 > body15 * 1.2
                if c15_t > 0:
                    scores["hold"] += 6; rhd.append("波幅扩张(上涨)")
                elif big_trend > 0 and has_lower_wick:
                    scores["hold"] += 3; rhd.append("波幅扩张(下影线支撑)")
                else:
                    scores["tp"] += 6; rtp.append("波幅扩张(下跌)")

        # ② 量价
        bv = [c["volume"] for c in k5[-8:] if c["close"] < c["open"]]
        lv = [c["volume"] for c in k5[-8:] if c["close"] >= c["open"]]
        ab = sum(bv)/len(bv) if bv else 0; al = sum(lv)/len(lv) if lv else ab
        bbr = (ab/al) if (ab and al) else 1.0
        if bbr > 1.5 and c15_t < 0:
            # 卖盘放量但大周期向上时看K线特征
            last15 = k15[-1]
            body15 = abs(last15["close"] - last15["open"])
            wick15 = min(last15["open"], last15["close"]) - last15["low"]
            has_lower_wick = body15 > 0 and wick15 > body15 * 1.2
            if big_trend > 0 and has_lower_wick:
                scores["hold"] += 3; rhd.append(f"卖盘{bbr:.1f}倍(有下影线承接)")
            else:
                scores["tp"] += 10; rtp.append(f"卖盘{bbr:.1f}倍买盘")
        elif bbr < 0.7 and c15_t > 0: scores["hold"] += 8; rhd.append(f"买盘{1/bbr:.1f}倍卖盘")
        l3v = [c["volume"] for c in k5[-3:]]; l3c = [c["close"] for c in k5[-3:]]
        vu, pu = l3v[-1]>l3v[0], l3c[-1]>l3c[0]
        if pu and not vu: scores["tp"] += 6; rtp.append("价涨量缩")
        elif not pu and vu: scores["tp"] += 8; rtp.append("价跌量增")
        elif pu and vu: scores["hold"] += 6; rhd.append("量价齐升")

        # ③ 位置与支撑
        k5h = sorted([c["high"] for c in k5], reverse=True)[:5]
        k5l = sorted([c["low"] for c in k5])[:5]
        sup, res = [], []
        lb = Counter(round(l,4) for l in k5l)
        hb = Counter(round(h,4) for h in k5h)
        if lb: sup.append(lb.most_common(1)[0][0])
        if hb: res.append(hb.most_common(1)[0][0])
        sup += [low_24h, sum(c["close"] for c in k5[-5:])/5, sum(c["close"] for c in k5[-10:])/10]
        d2s = min(abs(mark-s)/mark*100 for s in sup if s < mark) if any(s < mark for s in sup) else 999
        d2r = min(abs(r-mark)/mark*100 for r in res if r > mark) if any(r > mark for r in res) else 999
        d2s15 = (mark-min(c["low"] for c in k15))/mark*100 if k15 and mark > min(c["low"] for c in k15) else 999
        if (d2s < 2 or d2s15 < 3) and pnl_pct < 5: scores["hold"] += 10; rhd.append(f"近支撑(d{d2s:.1f}%/{d2s15:.1f}%)")
        if d2r < 2 and pnl_pct > 5: scores["tp"] += 8
        h5, l5 = max(c["high"] for c in k5), min(c["low"] for c in k5)
        p5 = (mark-l5)/(h5-l5)*100 if h5>l5 else 50
        if p5 > 80: scores["tp"] += 4; rtp.append(f"5m高位({p5:.0f}%)")
        elif p5 < 15 and pnl_pct < 5: scores["hold"] += 5; rhd.append(f"5m低位({p5:.0f}%)")

        # ④ 短期动量
        cd = sum(1 for c in reversed(k5) if c["close"] < c["open"])
        cu = sum(1 for c in reversed(k5) if c["close"] > c["open"])
        if cu >= 5 and h1h_t > 5: scores["tp"] += 6; rtp.append(f"连{cu}阳超买")
        if cd >= 5 and pnl_pct < 3: scores["hold"] += 5; rhd.append(f"连{cd}阴超卖")
        if cd >= 3:
            lk = k5[-1]; body = abs(lk["close"]-lk["open"]); wick = min(lk["open"],lk["close"])-lk["low"]
            if body > 0 and wick > body*1.5: scores["hold"] += 7; rhd.append("下影线止跌")
        if len(k5) >= 3:
            sp = [k5[i]["close"]-k5[i]["open"] for i in range(-3,0)]
            if all(s < 0 for s in sp) and abs(sp[-1]) > abs(sp[0])*1.3: scores["tp"] += 5; rtp.append("加速下跌")

        # ⑤ 入场后回撤判断（保护利润）
        high_since_entry = max(c["high"] for c in k5)
        retrace = (high_since_entry - mark) / high_since_entry * 100 if high_since_entry > entry_price else 0
        if retrace > 3 and pnl_pct > 2:
            scores["tp"] += 6; rtp.append(f"回撤{retrace:.1f}%锁定利润")
        elif retrace > 5 and pnl_pct > 0:
            scores["tp"] += 10; rtp.append(f"大幅回撤{retrace:.1f}%")
        elif retrace > 2 and pnl_pct < 0:
            scores["sl"] += 8; rsl.append(f"回撤{retrace:.1f}%趋势可能反转")
        # 连续3根5m阴线从高点下来
        recent = k5[-4:]
        if len(recent) >= 3:
            down_seq = sum(1 for c in recent if c["close"] < c["open"])
            if down_seq >= 3 and retrace > 2:
                scores["tp"] += 8; rtp.append("连阴回撤需警惕")

        # ⑥ 市场情绪
        if fr < -0.5: scores["hold"] += 6; rhd.append(f"负费率{fr:.3f}%")
        elif fr > 0.1: scores["tp"] += 4; rtp.append(f"正费率{fr:.3f}%")

    # 决策前计算动态止盈目标价（基于入场评分 + 趋势 + 压力位）
    tp_target = None
    tp_reason = ""
    # 根据tracker中的入场评分调整止盈策略
    trk = load_tracker()
    tr_entry = trk.get(symbol, {}).get("entry_score", 50)
    if k15 and k1h:
        c15_up = (k15[-1]["close"] - k15[-4]["close"]) / k15[-4]["close"] * 100 if k15[-4]["close"] else 0
        h1h_up = (k1h[-1]["close"] - k1h[-4]["close"]) / k1h[-4]["close"] * 100 if k1h[-4]["close"] else 0

        # 强趋势（≥60）：等趋势走坏才止盈，不设固定目标
        if tr_entry >= 60:
            # 只有双周期都向下时才考虑止盈
            if c15_up <= 0 and h1h_up <= 0:
                scores["tp"] += 8; rtp.append(f"强趋势衰减(15m{h1h_up:+.1f}%+1h{h1h_up:+.1f}%)")
            # 如果趋势还在，给hold加分防止误止盈
            if c15_up > 0 or h1h_up > 0:
                scores["hold"] += 6; rhd.append("强趋势延续")
        # 弱趋势（40~59）：有利润就走，或接近压力位就走
        elif tr_entry >= 40:
            # 见顶信号或压力位接近就止盈
            space_to_high = (high_24h - mark) / mark * 100 if mark > 0 else 0
            if space_to_high < 3:
                scores["tp"] += 10; rtp.append(f"弱趋势+空间耗尽({space_to_high:.1f}%)")
            # 15m趋势转负就止盈
            if c15_up <= 0:
                scores["tp"] += 8; rtp.append("弱趋势+15m转负")
            # 有利润且接近24h高点
            if pnl_pct > 2 and space_to_high < 5:
                scores["tp"] += 6; rtp.append("弱趋势+见好就收")
        if c15_up > 0 and h1h_up > 0:
            trend_strength = max(c15_up, h1h_up)
            if trend_strength > 8:
                tp_pct = 10
            elif trend_strength > 4:
                tp_pct = 7
            else:
                tp_pct = 5
            if pnl_pct > 0:
                extra = min(trend_strength * 0.5, 5)
                tp_pct = tp_pct + extra
            tp_pct = min(tp_pct, 15)
            tp_target = round(entry_price * (1 + tp_pct / 100), 8)
            tp_reason = f"趋势目标{tp_pct}%(${tp_target:.5f})"
            scores["tp"] += 3
        # 压力位作为附加参考：如果趋势目标超过了压力位，以压力位为参考
        if tp_target and near_resist and tp_target > near_resist:
            tp_target = near_resist
            tp_reason += f" 近端压力{near_resist:.4f}"

        # ⑦ 压力位冲击判断 — 价格到压力位多次冲不破，考虑止盈
        # 但如果大周期趋势很强（1h+4h向上），不因为短期压力位阻挡就平仓
        if resist_stuck and big_trend <= 0:
            scores["tp"] += 12; rtp.append(f"压力{near_resist:.4f}冲击{touches}次未破")

        # ⑧ 动量衰减止盈 — 15m连续8根K线不创新高+缩量=短期动能衰竭
        # 适用于强趋势中的正常回调，避免卖飞的同时也能及时止盈
        if len(k15) >= 10 and pnl_pct > 2:
            recent_high = max(c["high"] for c in k15[-8:])
            prev_high = max(c["high"] for c in k15[-12:-8])
            # 最近8根K线（2小时）没创新高
            no_new_high = recent_high <= prev_high * 1.002  # 允许0.2%误差
            # 最近4根成交量相比前4根萎缩
            v_recent = sum(c["volume"] for c in k15[-4:])
            v_before = sum(c["volume"] for c in k15[-8:-4])
            vol_shrink = v_recent < v_before * 0.8 if v_before > 0 else False
            if no_new_high and vol_shrink:
                scores["tp"] += 10; rtp.append(f"动量衰减-2h未创新高+缩量")

    # 决策：只输出评分报告给main循环，main自己决定执行什么
    report = {**scores, "pnl_pct": round(pnl_pct,2), "pullback_24h": round(pullback,1), "chg_24h": round(chg_24h,1), "fr": round(fr,3)}
    # 构建reason：取分数最高的维度作为描述
    all_reasons = []
    if rhd: all_reasons.append("; ".join(rhd[:2]))
    if rsl: all_reasons.append("; ".join(rsl[:2]))
    if rtp: all_reasons.append("; ".join(rtp[:2]))
    reason = "; ".join(all_reasons[:3]) if all_reasons else f"趋势健康({pnl_pct:+.1f}%)"
    return ("hold", reason, 0.5, report)

def _check_structural_stop(symbol, entry_price, amt, side):
    """结构止损检查：判断价格是否跌破关键结构位
    返回 (triggered, reason, threshold_price)
    """
    mark = get_mark_price(symbol)
    if not mark: return False, "", None
    
    k15 = get_klines(symbol, "15m", 12)
    if not k15 or len(k15) < 8:
        return False, "", None
    
    # 1) 15m前低（最近8根K线的最低点）
    k15_lows = [c["low"] for c in k15[-8:]]
    recent_low = min(k15_lows)
    break_pct = (mark - recent_low) / mark * 100 if mark > 0 else 0
    
    # 2) 入场后最高点回撤
    high_since_entry = max(c["high"] for c in k15)
    retrace_from_high = (high_since_entry - mark) / high_since_entry * 100 if high_since_entry > 0 else 0
    
    reasons = []
    
    # 条件A：跌破15m前低
    if break_pct < 0:
        reasons.append(f"跌破15m前低({recent_low:.5f})")
    
    # 条件B：从高点回撤超过5%且跌破入场价
    if retrace_from_high > 5 and mark < entry_price:
        reasons.append(f"结构破坏(回撤{retrace_from_high:.1f}%且破入场价)")
    
    if reasons:
        return True, "; ".join(reasons), recent_low
    return False, "", None


def _calc_trail_sl(entry_score, hold_score):
    """根据趋势强度计算移动止损比例
    弱趋势 5%, 普通 7%, 强趋势 9%
    """
    if entry_score >= 70 or hold_score >= 70:
        return 0.09  # 强趋势 9%
    elif entry_score >= 60 or hold_score >= 60:
        return 0.07  # 普通趋势 7%
    else:
        return 0.05  # 弱趋势 5%
_STEP_CACHE = {}
_STEP_CACHE_TIME = 0

def _get_step_size(symbol):
    """获取交易对的最小交易精度（stepSize），带缓存（每60秒刷新）"""
    global _STEP_CACHE, _STEP_CACHE_TIME
    now = time.time()
    if now - _STEP_CACHE_TIME > 60:
        # 缓存过期，拉全量
        info = curl_get("https://fapi.binance.com/fapi/v1/exchangeInfo")
        if info and "symbols" in info:
            _STEP_CACHE = {}
            for s in info["symbols"]:
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        step = float(f["stepSize"])
                        _STEP_CACHE[s["symbol"]] = 1.0 if step >= 1 else step
                        break
            _STEP_CACHE_TIME = now
    if symbol in _STEP_CACHE:
        return _STEP_CACHE[symbol]
    return 0.001  # fallback

def _round_qty(qty, step_size):
    """向下取整到stepSize的倍数"""
    if step_size <= 0: return qty
    if step_size >= 1:
        # 整数步长（stepSize>=1），直接取整
        return int(qty // step_size) * int(step_size)
    step_str = f"{step_size:.10f}".rstrip('0')
    if '.' in step_str:
        precision = len(step_str.split('.')[1])
    else:
        precision = 0
    return round(int(qty / step_size) * step_size, precision + 1)

def close_position(symbol, side, amt):
    step = _get_step_size(symbol)
    qty = _round_qty(abs(amt), step)
    side_to_close = "SELL" if amt > 0 else "BUY"
    return api_post("/fapi/v1/order", f"symbol={symbol}&side={side_to_close}&type=MARKET&quantity={qty}&positionSide={side}")

def place_order(symbol, side, pos_side, qty, leverage=3):
    api_post("/fapi/v1/leverage", f"symbol={symbol}&leverage={leverage}")
    step = _get_step_size(symbol)
    exact_qty = _round_qty(qty, step)
    return api_post("/fapi/v1/order", f"symbol={symbol}&side={side}&type=MARKET&quantity={exact_qty}&positionSide={pos_side}")

def set_leverage(symbol, lev): api_post("/fapi/v1/leverage", f"symbol={symbol}&leverage={lev}")

def scan_market():
    """24h合约涨幅榜前20名（候选池，开仓评分由entry_timing决定）"""
    tickers = curl_get("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if not tickers: return None
    valid = [t for t in tickers if t['symbol'].endswith('USDT')
             and not any(x in t['symbol'] for x in ['USDC','BUSD','FDUSD','USDP'])
             and 'TUSD' not in t['symbol'].replace('USDT','')
             and float(t.get('quoteVolume',0)) >= 100000]

    valid.sort(key=lambda x: float(x['priceChangePercent']), reverse=True)
    signals = []
    for t in valid[:20]:
        sym = t['symbol']
        price = float(t['lastPrice'])
        chg24 = float(t['priceChangePercent'])
        vol = float(t['quoteVolume'])
        pb = (float(t['highPrice']) - price) / float(t['highPrice']) * 100 if float(t['highPrice']) > 0 else 0
        signals.append({"symbol": sym, "price": price, "change": chg24, "volume": vol,
                        "fundingRate": 0, "pullback": round(pb, 1), "score": round(chg24, 1)})
    return signals[:20]

def format_status_header(balances, positions):
    now = datetime.now().strftime("%m/%d %H:%M")
    lines = [f"⏱️ {now}"]
    b = balances.get("USDT", {}) if balances else {}
    lines.append(f"💰 余额: {b.get('total',0):.2f} USDT (可用 {b.get('available',0):.2f})")
    if positions:
        for p in positions:
            mark = get_mark_price(p["symbol"])
            if mark:
                pp = (mark - p["entry"]) / p["entry"] * 100 * (1 if p["amt"] > 0 else -1)
                e = "🟢" if pp > 0 else "🔴"
                amt_usd = round(p["amt"] * mark)
                lines.append(f"{e} {p['symbol']} {p['side']} {amt_usd}u @${p['entry']:.5f}")
                lines.append(f"  当前: ${mark:.5f} PnL: ${p['pnl']:.2f} ({pp:.2f}%)")
    else: lines.append("📭 无持仓")
    return "\n".join(lines)

def entry_timing(symbol):
    """
    趋势延续性评分（0~100分）：
    判断一个币还能不能继续涨，能涨多少
    - ≥60分 → 全仓开（10x/95u）
    - 40~59分 → 半仓开（5x/55u）
    - <40分 → 不开
    """
    k15 = get_klines(symbol, "15m", 12)
    k1h = get_klines(symbol, "1h", 6)
    t24 = curl_get(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}")
    if not k15 or not k1h or not t24 or len(k15) < 6 or len(k1h) < 4:
        return 30, "数据不足,保守入场", None, None, None  # 数据不足时保守

    price = k15[-1]["close"]
    high_24h = float(t24["highPrice"])
    low_24h = float(t24["lowPrice"])
    high_15m = max(c["high"] for c in k15)
    low_15m = min(c["low"] for c in k15)

    # ====== 硬性否决条件 ======

    c15_t = (k15[-1]["close"] - k15[-4]["close"]) / k15[-4]["close"] * 100
    h1h_t = (k1h[-1]["close"] - k1h[-3]["close"]) / k1h[-3]["close"] * 100

    # 否决①：双周期都向下 → 不开
    if c15_t <= 0 and h1h_t <= 0:
        return 0, f"否决:15m向下({c15_t:+.1f}%)+1h向下({h1h_t:+.1f}%)", None, None, None

    # 否决②：从24h高点回撤超过8% → 不开（涨幅已结束）
    retrace = (high_24h - price) / high_24h * 100
    if retrace > 8:
        return 0, f"否决:24h高点回撤{retrace:.1f}%>8%", None, None, None

    score = 0
    reasons = []

    # ========== ① 趋势强度（30分） ==========

    # 15m斜率（最近4根15m）
    if c15_t > 2:
        score += 10; reasons.append(f"15m强({c15_t:+.1f}%)")
    elif c15_t > 1:
        score += 5; reasons.append(f"15m中({c15_t:+.1f}%)")
    elif c15_t > 0:
        score += 2; reasons.append(f"15m弱({c15_t:+.1f}%)")
    else:
        score -= 5; reasons.append(f"15m向下({c15_t:+.1f}%)->弱势-5")

    # 1h斜率（最近3根1h）
    if h1h_t > 3:
        score += 10; reasons.append(f"1h强({h1h_t:+.1f}%)")
    elif h1h_t > 1:
        score += 5; reasons.append(f"1h中({h1h_t:+.1f}%)")
    elif h1h_t > 0:
        score += 2; reasons.append(f"1h弱({h1h_t:+.1f}%)")
    else:
        score -= 8; reasons.append(f"1h向下({h1h_t:+.1f}%)->弱势-8")

    # 多周期共振
    if c15_t > 0 and h1h_t > 0:
        score += 10; reasons.append("15m1h共振向上")
    elif c15_t > 0 or h1h_t > 0:
        score += 5; reasons.append("单周期向上")

    # ========== ② 动能健康度（30分） ==========

    recent = k15[-5:]
    # 量价配合
    vol_up = sum(c["volume"] for c in recent if c["close"] >= c["open"])
    vol_dn = sum(c["volume"] for c in recent if c["close"] < c["open"])
    if vol_up > vol_dn * 2:
        score += 10; reasons.append("上涨放量")
    elif vol_up > vol_dn:
        score += 5; reasons.append("量价健康")
    else:
        score -= 5; reasons.append("下跌放量")

    # 见顶信号：用15m+1h高点趋势综合判断
    # 取最近6根15m线和最近3根1h线
    recent_6 = k15[-6:]
    recent_3h = k1h[-3:]
    highs_descending = all(recent_6[i]["high"] >= recent_6[i+1]["high"] for i in range(len(recent_6)-1))
    lows_descending = all(recent_6[i]["low"] >= recent_6[i+1]["low"] for i in range(len(recent_6)-1))
    highs_ascending = all(recent_6[i]["high"] <= recent_6[i+1]["high"] for i in range(len(recent_6)-1))
    h1_ascending = all(recent_3h[i]["high"] <= recent_3h[i+1]["high"] for i in range(len(recent_3h)-1))
    h1_descending = all(recent_3h[i]["high"] >= recent_3h[i+1]["high"] for i in range(len(recent_3h)-1))

    # 只要15m或1h有一个在突破新高就视为强势
    if highs_ascending or h1_ascending:
        # 高点不断抬高 → 强势延续
        score += 10; reasons.append("15m高点不断抬高->强势" if highs_ascending else "1h高点不断抬高->强势")
        # 检查是否有长上影线但后续仍破高 → 插针确认
        has_upper_wick = False
        for c in k15[-3:]:
            body = abs(c["close"] - c["open"])
            upper_wick = c["high"] - max(c["open"], c["close"])
            if body > 0 and upper_wick > body * 1.5:
                has_upper_wick = True; break
        if has_upper_wick and (highs_ascending or h1_ascending):
            score += 5; reasons.append("上影线后继续破高->插针洗盘")
    elif highs_descending and lows_descending:
        # 高点和低点都在降低 → 真见顶，越陡峭扣越多
        high_drop = (recent_6[0]["high"] - recent_6[-1]["high"]) / recent_6[-1]["high"] * 100
        penalty = -min(25, max(-10, -high_drop * 2))
        if penalty < -10:
            score += penalty; reasons.append(f"高点持续走低({high_drop:.1f}%)->见顶信号{penalty}")
    else:
        # 普通震荡，不扣分
        reasons.append("15m震荡盘整")

    # 买盘优势
    up_count = sum(1 for c in recent if c["close"] >= c["open"])
    dn_count = sum(1 for c in recent if c["close"] < c["open"])
    if up_count >= 4:
        score += 10; reasons.append(f"买盘优势({up_count}阳{dn_count}阴)")
    elif up_count >= 3:
        score += 5; reasons.append(f"买盘略优({up_count}阳)")

    # ====== 超买判断：15m或1h任意一个在突破新高就豁免 ======
    if not highs_ascending and not h1_ascending:
        k15_extended = get_klines(symbol, "15m", 15)
        if k15_extended and len(k15_extended) >= 12:
            total_green = sum(1 for c in k15_extended if c["close"] >= c["open"])
            green_ratio = total_green / len(k15_extended)
            if green_ratio >= 0.80:
                penalty = -10; reasons.append(f"超买(15m阳{total_green}/{len(k15_extended)}={green_ratio:.0%})")
                score += penalty

    # ========== ③ 上涨空间（30分） ==========

    # 取消"距离24h高点远就给分"的逻辑（跌得越深分越高，有悖入场逻辑）
    # 改为：只看压力位远近和突破信号
    
    # 判断15m和1h是否都在向上（强势突破模式）
    c15_up = (k15[-1]["close"] - k15[-4]["close"]) / k15[-4]["close"] * 100 > 0
    h1_up = (k1h[-1]["close"] - k1h[-3]["close"]) / k1h[-3]["close"] * 100 > 0
    both_up = c15_up and h1_up
    space_to_high = (high_24h - price) / price * 100 if price > 0 else 0

    # 极近/逼近新高 + 双周期向上 → 真实破信号
    if space_to_high <= 2 and both_up:
        score += 15; reasons.append(f"破新高在即({space_to_high:.1f}%)+双周期向上->真突破+15")
    elif space_to_high <= 5 and both_up:
        score += 12; reasons.append(f"逼近前高({space_to_high:.1f}%)+双周期向上->突破酝酿+12")

    # 近端压力位（15m周期最高点）
    d2_r = (high_15m - price) / price * 100 if price > 0 else 0
    if d2_r > 5:
        score += 10; reasons.append(f"压力位远({d2_r:.1f}%)")
    elif d2_r > 2:
        score += 5; reasons.append(f"压力位中({d2_r:.1f}%)")
    elif d2_r > 0:
        reasons.append(f"近压力位({d2_r:.1f}%)")

    # 下方有支撑（近3根15m低点就在附近，说明没破位）
    recent_low = min(c["low"] for c in k15[-3:])
    d2_support = (price - recent_low) / price * 100 if price > 0 else 0
    if d2_support < 2:
        score += 10; reasons.append(f"近支撑({d2_support:.1f}%)")

    # 计算开仓区间和压力位（给后续逻辑用）
    entry_low = round(min(price, recent_low * 1.01), 6)
    entry_high = round(price, 6)
    near_resist = round(high_15m, 6)
    far_resist = round(high_24h, 6)

    # ====== 创新高检测：6小时跨度看币的突破能力 ======
    k15_24 = get_klines(symbol, "15m", 24)
    if k15_24 and len(k15_24) >= 20:
        high_6h = max(c["high"] for c in k15_24)  # 6小时内最高点
        recent_high = max(c["high"] for c in k15_24[-12:])  # 最近3小时最高点

        if high_6h > 0:
            break_ratio = recent_high / high_6h

            # ✅ 持续创新高：最近3h高点 >= 6h高点的99.8%
            # 但需叠加趋势条件：15m或1h至少有一个在向上，否则可能是假突破
            c15_up = (k15[-1]["close"] - k15[-4]["close"]) / k15[-4]["close"] * 100 > 0 if len(k15) >= 4 else False
            h1_up = (k1h[-1]["close"] - k1h[-3]["close"]) / k1h[-3]["close"] * 100 > 0 if len(k1h) >= 4 else False
            if break_ratio >= 0.998 and (c15_up or h1_up):
                if break_ratio >= 1.0:
                    score += 15; reasons.append(f"破6h新高+{((break_ratio-1)*100):.2f}%->强势延续+15")
                else:
                    score += 10; reasons.append(f"逼近6h新高({(break_ratio*100):.1f}%)->突破酝酿+10")
            elif break_ratio >= 0.998 and not (c15_up or h1_up):
                # 逼近前高但趋势向下 → 可能是假突破/双顶，不扣分也不加分
                reasons.append(f"逼近前高({(break_ratio*100):.1f}%)但趋势向下-观望")

            # ❌ 弱势反弹：最近3h高点远低于6h高点
            elif break_ratio < 0.995:
                sc_file = os.path.expanduser("~/.hermes/scripts/stop_cooldown.json")
                has_stop = False
                if os.path.exists(sc_file):
                    try:
                        with open(sc_file) as f:
                            sc_data = json.load(f)
                        prev_stop = sc_data.get(symbol, {}).get("count", 0)
                        has_stop = prev_stop > 0
                    except:
                        pass
                if has_stop:
                    score -= 50; reasons.append(f"止损后弱势反弹未破新高({(break_ratio-1)*100:.1f}%)-否决")
                else:
                    score -= 25; reasons.append(f"弱势反弹未破新高({(break_ratio-1)*100:.1f}%)")

    # ====== 止损次数惩罚：止损越多，扣分越多 ======
    sc_file = os.path.expanduser("~/.hermes/scripts/stop_cooldown.json")
    if os.path.exists(sc_file):
        try:
            with open(sc_file) as f:
                sc_data = json.load(f)
        except:
            sc_data = {}
        sc_entry = sc_data.get(symbol, {})
        if isinstance(sc_entry, list):
            stop_count = len(sc_entry)
        else:
            stop_count = sc_entry.get("count", 0)
        if stop_count >= 4:
            score -= 25; reasons.append(f"止损{stop_count}次->重罚-25")
        elif stop_count >= 3:
            score -= 15; reasons.append(f"止损{stop_count}次->罚-15")
        elif stop_count >= 2:
            score -= 8; reasons.append(f"止损{stop_count}次->罚-8")
        elif stop_count == 1:
            score -= 3; reasons.append(f"止损被打过->轻度-3")

    return score, "; ".join(reasons), (entry_low, entry_high), near_resist, far_resist


def cooldown_seconds(count):
    """阶梯冷却时长：1次→1h, 2次→6h, 3次→12h, 4次→48h"""
    if count >= 4: return 172800  # 48h
    if count >= 3: return 43200   # 12h
    if count >= 2: return 21600   # 6h
    if count >= 1: return 3600    # 1h
    return 0

NEW_TRADES_ENABLED = True  # 设为False暂停开新仓

def place_new_trade(signals=None):
    if not NEW_TRADES_ENABLED:
        return None
    if not signals:
        signals = scan_market()
    if not signals: return None
    _, positions = get_account()
    existing = {p["symbol"] for p in positions} if positions else set()
    balances, _ = get_account()
    if not balances or "USDT" not in balances: return None
    avail = balances["USDT"]["available"]; cnt = len(positions) if positions else 0
    if cnt >= 6: return None

    # 加载全局止损冷却（阶梯式：3次→1h, 4次→5h, 5次→24h）
    sc_file = os.path.expanduser("~/.hermes/scripts/stop_cooldown.json")
    sc_data = {}
    if os.path.exists(sc_file):
        try:
            with open(sc_file) as f: sc_data = json.load(f)
        except: pass
    now_ts = time.time()

    # 按scan_market评分从高到低遍历，入场时机过滤，第一个过线的就开
    for best in signals[:20]:
        if best["symbol"] in existing: continue

        # 🔥 止损冷却检查：兼容两种数据格式（旧版list/新版dict）
        sc_entry = sc_data.get(best["symbol"], {})
        if isinstance(sc_entry, list):
            # 旧格式：时间戳列表 → 转为dict格式（只处理前3次）
            stop_count = len([t for t in sc_entry if time.time() - t < 86400])  # 24h内
            cooldown_until = 0
        else:
            stop_count = sc_entry.get("count", 0)
            cooldown_until = sc_entry.get("cooldown_until", 0)
        if stop_count >= 3 and now_ts < cooldown_until:
            remain = int(cooldown_until - now_ts)
            print(f"  ⏳ {best['symbol']} 已止损{stop_count}次, 冷却中({remain//60}m{remain%60}s)")
            continue
        # 冷却过期了就清零重新计数
        if stop_count >= 3 and now_ts >= cooldown_until:
            sc_data[best["symbol"]] = {"count": 0, "cooldown_until": 0}

        price = best["price"]

        timing, timing_reason, entry_range, near_resist, far_resist = entry_timing(best["symbol"])
        if timing < 40:
            continue  # 入场时机不过关就跳过，看下一个

        # 仓位分层：开仓金额固定40~80u，按评分调杠杆
        if timing >= 75:
            leverage = 10; target_value = 90
        elif timing >= 60:
            leverage = 8; target_value = 70
        else:
            leverage = 5; target_value = 50

        max_value = min(target_value, avail * leverage * 0.6)
        if max_value < 10: continue

        qty = max(1, int(max_value / price))
        if qty * price < 5: continue

        set_leverage(best["symbol"], leverage)
        r = place_order(best["symbol"], "BUY", "LONG", qty, leverage)
        if "orderId" in r:
            trk = load_tracker()
            trk[best["symbol"]] = {"entry_score": timing, "high": price, "trail_sl": None, "tp_triggered": False}
            save_tracker(trk)
            return {"symbol": best["symbol"], "qty": qty, "price": price, "value": round(qty*price, 1),
                    "leverage": leverage, "reason": f"评分{best['score']} 入场时机{timing}({timing_reason}) 涨幅{best['change']:.1f}% 费率{best['fundingRate']:+.4f}%",
                    "entry_range": entry_range,
                    "near_resist": near_resist, "far_resist": far_resist}
    return None

def load_tracker():
    """加载持仓追踪状态（最高价、止损线）"""
    import json, os
    f = os.path.expanduser("~/.hermes/scripts/tracker.json")
    if os.path.exists(f):
        try:
            with open(f) as fp: return json.load(fp)
        except: return {}
    return {}

def save_tracker(tracker):
    """保存持仓追踪状态"""
    import json, os
    f = os.path.expanduser("~/.hermes/scripts/tracker.json")
    with open(f, "w") as fp: json.dump(tracker, fp)

def check_btc_market():
    """判断BTC大盘环境，返回(状态, 描述, 操作系数, BTC价格)
    新增冷却机制：15mK线收阴且趋势向下→停15分钟，1hK线收阴且趋势向下→停30分钟
    用15m+1h+4h+24h四个维度综合判断
    """
    t = curl_get("https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=BTCUSDT")
    if not t: return "unknown", "BTC数据获取失败", 1.0, 0
    btc_chg = float(t['priceChangePercent'])
    btc_price = float(t['lastPrice'])

    k15 = get_klines("BTCUSDT", "15m", 5)
    btc_15m = 0
    if k15 and len(k15) >= 3:
        btc_15m = (k15[-1]["close"] - k15[-3]["close"]) / k15[-3]["close"] * 100

    k1h = get_klines("BTCUSDT", "1h", 4)
    btc_1h = 0
    if k1h and len(k1h) >= 3:
        btc_1h = (k1h[-1]["close"] - k1h[-3]["close"]) / k1h[-3]["close"] * 100

    k4h = get_klines("BTCUSDT", "4h", 4)
    btc_4h = 0
    if k4h and len(k4h) >= 2:
        btc_4h = (k4h[-1]["close"] - k4h[-2]["close"]) / k4h[-2]["close"] * 100

    # BTC冷却时间机制 ==== 升级版：1h趋势向下直接关停开仓 ====
    cd_file = os.path.expanduser("~/.hermes/scripts/btc_cooldown.json")
    cd = {"until_15m": 0, "until_1h": 0}
    if os.path.exists(cd_file):
        try:
            with open(cd_file) as f: cd = json.load(f)
        except: pass

    now = time.time()
    cd_15m = now < cd.get("until_15m", 0)
    cd_1h = now < cd.get("until_1h", 0)

    # 15m K线收阴 + 趋势向下 → 触发15分钟冷却
    if k15 and len(k15) >= 2:
        last = k15[-1]
        if last["close"] < last["open"] and btc_15m < 0 and not cd_15m:
            cd["until_15m"] = now + 15 * 60
            with open(cd_file, "w") as f: json.dump(cd, f)
            cd_15m = True

    # 1h K线收阴 + 趋势向下 → 触发30分钟冷却
    if k1h and len(k1h) >= 2:
        last = k1h[-1]
        if last["close"] < last["open"] and btc_1h < 0 and not cd_1h:
            cd["until_1h"] = now + 30 * 60
            with open(cd_file, "w") as f: json.dump(cd, f)
            cd_1h = True

    cd_msgs = []
    if cd_15m:
        r = int(cd["until_15m"] - now)
        cd_msgs.append(f"15m冷却{r//60}m{r%60}s")
    if cd_1h:
        r = int(cd["until_1h"] - now)
        cd_msgs.append(f"1h冷却{r//60}m{r%60}s")
    if cd_msgs:
        return "cooldown", f"💤 BTC冷却中({'|'.join(cd_msgs)}), 仅参考", 1.0, btc_price

    up = sum(1 for v in [btc_15m, btc_1h, btc_4h, btc_chg] if v > 0)
    down = sum(1 for v in [btc_15m, btc_1h, btc_4h, btc_chg] if v < 0)

    # 极端下跌（24h跌超3%且多周期向下）
    if btc_chg < -3 and down >= 3:
        return "danger", f"📉 BTC全周期下跌(24h{btc_chg:.1f}% 1h{btc_1h:+.1f}%), 不开新仓", 0.0, btc_price

    # 趋势向下（多周期跌多涨少）
    if down >= 3:
        return "down", f"📉 BTC偏弱(15m{btc_15m:+.1f}% 1h{btc_1h:+.1f}% 4h{btc_4h:+.1f}%), 不开新仓", 0.0, btc_price

    # 趋势向上（多周期涨多跌少或全涨）
    if up >= 3 or (btc_chg > 1 and up >= 2):
        return "up", f"📈 BTC向好(24h{btc_chg:+.1f}% 1h{btc_1h:+.1f}% 4h{btc_4h:+.1f}%), 正常操作", 1.0, btc_price

    # 横盘分化（涨跌各半）
    if up >= 1 and down >= 1:
        return "sideways", f"➡️ BTC横盘(24h{btc_chg:+.1f}% 1h{btc_1h:+.1f}%), 减半开仓", 0.5, btc_price

    # fallback
    return "unknown", f"❓ BTC信号不明(24h{btc_chg:+.1f}%), 谨慎操作", 0.3, btc_price


def main():
    msg = []
    balances, positions = get_account()
    if balances is None: print("⚠️ API连接失败"); return

    # BTC大盘环境判断
    btc_status, btc_desc, btc_coeff, btc_price = check_btc_market()
    msg.append(f"🟡 BTC: ${btc_price:.0f} {btc_desc}")

    # 加载全局止损冷却（同一个标止损3次后冷却1小时）
    sc_file = os.path.expanduser("~/.hermes/scripts/stop_cooldown.json")
    sc_data = {}
    if os.path.exists(sc_file):
        try:
            with open(sc_file) as f: sc_data = json.load(f)
        except: pass

    signals = scan_market()
    tracker = load_tracker()
    pos_map = {p["symbol"]: p for p in positions} if positions else {}

    # 清理已平仓的追踪记录
    for sym in list(tracker.keys()):
        if sym not in pos_map:
            del tracker[sym]

    # ========== 持仓处理：强趋势分层止盈止损体系 ==========
    if positions:
        for p in positions:
            sym = p["symbol"]
            mark = get_mark_price(sym)
            if not mark: continue
            entry = p["entry"]
            side = p["side"]
            amt = p["amt"]
            pnl_pct = (mark - entry) / entry * 100 if amt > 0 else (entry - mark) / entry * 100

            # 取tracker状态（默认值）
            tr = tracker.get(sym, {"high": mark, "trail_sl": None, "entry_score": 50})
            entry_score = tr.get("entry_score", 50)

            # 阶段0：硬止损 -5% 兜底
            loss = (entry - mark) / entry * 100 if amt > 0 else (mark - entry) / entry * 100
            if loss <= -5:
                r = close_position(sym, side, amt)
                if "orderId" in r:
                    msg.append(f"🛑 硬止损! {sym} 亏损{loss:.2f}%")
                    tr["stop_count"] = tr.get("stop_count", 0) + 1
                    tracker[sym] = tr
                    sc_entry2 = sc_data.get(sym, {"count": 0, "cooldown_until": 0})
                    sc_entry2["count"] = sc_entry2["count"] + 1
                    cd_sec = cooldown_seconds(sc_entry2["count"])
                    sc_entry2["cooldown_until"] = time.time() + cd_sec
                    sc_data[sym] = sc_entry2
                    with open(sc_file, "w") as _f: json.dump(sc_data, _f)
                else:
                    msg.append(f"⚠️ {sym} 硬止损失败: {r.get('msg','?')}")
                tracker.pop(sym, None)
                save_tracker(tracker)
                continue

            # 阶段1：结构止损（跌破15m前低 / 结构破坏）
            struct_triggered, struct_reason, struct_price = _check_structural_stop(sym, entry, amt, side)
            if struct_triggered:
                # 但如果有大周期向上保护（hold_score足够高），不触发结构止损
                _, _, _, rpt = analyze_position(sym, entry, amt)
                if rpt.get("hold", 0) >= rpt.get("sl", 0) + 10:
                    # hold 明显高于 sl，说明大周期还在向上，结构止损暂缓
                    msg.append(f"⚠️ {sym} 结构走弱但大周期hold{rpt['hold']}>sl{rpt['sl']}+10, 暂缓止损")
                else:
                    r = close_position(sym, side, amt)
                    if "orderId" in r:
                        msg.append(f"🔴 结构止损! {sym} {struct_reason}")
                        tr["stop_count"] = tr.get("stop_count", 0) + 1
                        tracker[sym] = tr
                        sc_entry2 = sc_data.get(sym, {"count": 0, "cooldown_until": 0})
                        sc_entry2["count"] = sc_entry2["count"] + 1
                        cd_sec = cooldown_seconds(sc_entry2["count"])
                        sc_entry2["cooldown_until"] = time.time() + cd_sec
                        sc_data[sym] = sc_entry2
                        with open(sc_file, "w") as _f: json.dump(sc_data, _f)
                    else:
                        msg.append(f"⚠️ {sym} 结构止损失败: {r.get('msg','?')}")
                    tracker.pop(sym, None)
                    save_tracker(tracker)
                    continue

            # ======== 更新tracker中的最高价追踪 ========
            if pnl_pct > 0 and mark > tr.get("high", 0):
                tr["high"] = mark
            tracker[sym] = tr

            # ======== 调用评分系统 ========
            action, reason, conf, rpt = analyze_position(sym, entry, amt)
            tp_s = rpt.get("tp", 0)
            hd_s = rpt.get("hold", 0)
            sl_s = rpt.get("sl", 0)

            total_s = tp_s + hd_s + sl_s
            hold_ratio = hd_s / total_s if total_s > 0 else 0.5

            e = "🟢" if pnl_pct > 0 else "🔴"

            # ======== 阶段2：评分止损执行 ========
            # 2a) sl_score >= 85 → 强制平仓
            if sl_s >= 85:
                r = close_position(sym, side, amt)
                if "orderId" in r:
                    msg.append(f"🔴 评分强制止损! {sym} sl{sl_s}⭐ {reason}")
                    tr["stop_count"] = tr.get("stop_count", 0) + 1
                    tracker[sym] = tr
                    sc_entry2 = sc_data.get(sym, {"count": 0, "cooldown_until": 0})
                    sc_entry2["count"] = sc_entry2["count"] + 1
                    cd_sec = cooldown_seconds(sc_entry2["count"])
                    sc_entry2["cooldown_until"] = time.time() + cd_sec
                    sc_data[sym] = sc_entry2
                    with open(sc_file, "w") as _f: json.dump(sc_data, _f)
                else:
                    msg.append(f"⚠️ {sym} 强制止损失败: {r.get('msg','?')}")
                tracker.pop(sym, None)
                save_tracker(tracker)
                continue

            # 2b) sl_score >= 70 → 走弱预警，减仓40%+收紧止损
            if sl_s >= 70:
                reduce_qty = round(amt * 0.4, 8)
                if reduce_qty > 0:
                    r = close_position(sym, side, reduce_qty)
                    if "orderId" in r:
                        msg.append(f"⚠️ 走弱减仓! {sym}-40% sl{sl_s}⭐ {reason}")
                        tr["sl_warn"] = True
                        tr["trail_sl"] = round(entry * 0.99, 8)  # 收紧到成本价99%
                        tr["high"] = mark
                        tracker[sym] = tr
                        save_tracker(tracker)
                    else:
                        msg.append(f"⚠️ {sym} 减仓失败: {r.get('msg','?')}")
                # 减仓后如果剩余为0则跳过
                remaining = amt - reduce_qty
                if remaining <= 0:
                    tracker.pop(sym, None)
                    save_tracker(tracker)
                    continue
                # 更新amt为剩余量
                amt = remaining
                pnl_pct = (mark - entry) / entry * 100 if amt > 0 else (entry - mark) / entry * 100

            # ======== 阶段3：分层减持止盈 ========
            # 更新tracker中的最高价
            if pnl_pct > 0 and mark > tr.get("high", 0):
                tr["high"] = mark
            tracker[sym] = tr

            executed = False

            # 3a) PnL >= 25% → 三级减持：保留40%-60%尾仓
            if pnl_pct >= 25:
                if hd_s >= 70:
                    # 强趋势：保留60%尾仓
                    reduce_pct = 0.4
                else:
                    # 弱趋势：保留40%尾仓
                    reduce_pct = 0.6
                reduce_qty = round(amt * reduce_pct, 8)
                if reduce_qty > 0:
                    r = close_position(sym, side, reduce_qty)
                    if "orderId" in r:
                        remain_pct = round((1-reduce_pct)*100)
                        msg.append(f"💰 止盈减仓! {sym} PnL{pnl_pct:.1f}% hold{hd_s}⭐ 保留{remain_pct}%尾仓")
                        tr["reduce_3"] = True
                        tr["trail_sl"] = round(entry * 1.01, 8)  # 止损推到成本线1%以上
                        tracker[sym] = tr
                        save_tracker(tracker)
                        executed = True
                    else:
                        msg.append(f"⚠️ {sym} 止盈减仓失败: {r.get('msg','?')}")
                if executed:
                    continue

            # 3b) PnL 15%~25% → 二级减持
            if pnl_pct >= 15 and pnl_pct < 25:
                if hd_s >= 70:
                    # 强趋势：不大幅减仓，继续持有
                    msg.append(f"🟢 {sym} PnL{pnl_pct:.1f}% hold{hd_s}⭐ 强趋势持有, 不止盈")
                else:
                    # 弱趋势：减仓30%
                    reduce_qty = round(amt * 0.3, 8)
                    if reduce_qty > 0:
                        r = close_position(sym, side, reduce_qty)
                        if "orderId" in r:
                            msg.append(f"💰 二级止盈! {sym} PnL{pnl_pct:.1f}% hold{hd_s}⭐ 减仓30%")
                            tr["reduce_2"] = True
                            tracker[sym] = tr
                            save_tracker(tracker)
                            executed = True
                        else:
                            msg.append(f"⚠️ {sym} 止盈减仓失败: {r.get('msg','?')}")
                if executed:
                    continue

            # 3c) PnL 8%~15% → 一级减持：减25%，止损推到成本价
            if pnl_pct >= 8 and pnl_pct < 15:
                reduce_qty = round(amt * 0.25, 8)
                if reduce_qty > 0:
                    r = close_position(sym, side, reduce_qty)
                    if "orderId" in r:
                        msg.append(f"💰 一级止盈! {sym} PnL{pnl_pct:.1f}% 减25%推成本")
                        tr["reduce_1"] = True
                        tr["trail_sl"] = round(entry, 8)  # 止损推到成本价
                        tracker[sym] = tr
                        save_tracker(tracker)
                        executed = True
                    else:
                        msg.append(f"⚠️ {sym} 止盈减仓失败: {r.get('msg','?')}")
                if executed:
                    continue

            # ======== 阶段4：移动止损（PnL >= 8%才启动） ========
            if pnl_pct >= 8:
                trail_pct = _calc_trail_sl(entry_score, hd_s)
                trail_sl_price = round(tr.get("high", mark) * (1 - trail_pct), 8)
                # 更新移动止损线（只会上调不会下调）
                if tr.get("trail_sl") is None or trail_sl_price > tr["trail_sl"]:
                    tr["trail_sl"] = trail_sl_price
                # 检查是否触发
                if mark < tr["trail_sl"]:
                    r = close_position(sym, side, amt)
                    if "orderId" in r:
                        msg.append(f"⚠️ 移动止损! {sym} 从高点回撤{trail_pct*100:.0f}%触发")
                    else:
                        msg.append(f"⚠️ {sym} 移动止损失败: {r.get('msg','?')}")
                    tracker.pop(sym, None)
                    save_tracker(tracker)
                    continue
                tracker[sym] = tr
            else:
                # PnL < 8%: 不启动移动止损，只更新最高价
                if pnl_pct > 0:
                    tracker[sym] = tr

            # ======== 阶段5：评分跟踪报告 ========
            if hd_s >= 70:
                verdict = "持有 ✅"
            elif hd_s >= 50:
                verdict = "观察 ⚠️"
            else:
                verdict = "关注 ❌"
            msg.append(f"{e} {sym} {pnl_pct:+.2f}% 📊 {hd_s}/{sl_s}/{tp_s}⭐ {verdict} | {reason}")

    save_tracker(tracker)
    cnt = len(positions) if positions else 0
    # 🚦 BTC不限制开仓，仅作参考（2026-05-31用户要求取消所有限制）
    # 只保留冷却提醒，不阻止开仓
    # 当日亏损停盘已取消 — 评分严格把关，自负盈亏

    # 开仓：只检查持仓上限，不限制
    max_pos = 7
    if len(positions) < max_pos and signals:
        t = place_new_trade(signals)
        if t: 
            er = t.get('entry_range', (0,0))
            nr, fr = t.get('near_resist', 0), t.get('far_resist', 0)
            msg.append(f"🚀 新开仓! {t['symbol']} {t['qty']}张 {t['leverage']}x 价值${t['value']} @${t['price']:.4f} ({t['reason']})")
            msg.append(f"   📊 区间: ${er[0]:.4f}~${er[1]:.4f} 🔴压力: ${nr:.4f}→${fr:.4f}")
    print(format_status_header(balances, positions))
    if msg:
        print("\n--- 动作 ---")
        for m in msg: print(m)
    if signals:
        print("\n📡 TOP 信号:")
        for s in signals[:15]:
            print(f"  {s['symbol']:10s} ${s['price']:<8.4f} +{s['change']:.1f}% 回调:{s['pullback']:.1f}% 费率:{s['fundingRate']:+.4f}% 评分:{s['score']}")

if __name__ == "__main__":
    main()
