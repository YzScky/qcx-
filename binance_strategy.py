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
CURLE = ["curl", "-s", "--max-time", "10", "--socks5-hostname", "127.0.0.1:7891"]

def get_api_keys():
    with open(os.path.expanduser("~/.binance/trading")) as f:
        lines = f.read().strip().split("\n")
        api = {}
        for l in lines:
            if "=" in l:
                k,v = l.split("=",1)
                api[k.strip()] = v.strip()
        return api["api-key"], api["api-secret"]

API_KEY, SECRET = get_api_keys()

def sign_request(params_str):
    return hmac.new(SECRET.encode(), params_str.encode(), hashlib.sha256).hexdigest()

def api_get(path, params=None):
    ts = int(time.time()*1000)
    qs = f"timestamp={ts}&recvWindow=50000"
    if params: qs += "&" + params
    sig = sign_request(qs)
    req = urllib.request.Request(f"https://fapi.binance.com{path}?{qs}&signature={sig}", headers={"X-MBX-APIKEY": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as r: return json.loads(r.read())
    except Exception as e: return {"error": str(e)}

def api_post(path, data_str):
    ts = int(time.time()*1000)
    qs = f"{data_str}&timestamp={ts}&recvWindow=50000"
    sig = sign_request(qs)
    req = urllib.request.Request(f"https://fapi.binance.com{path}", data=f"{qs}&signature={sig}".encode(), headers={"X-MBX-APIKEY": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as r: return json.loads(r.read())
    except Exception as e: return {"error": str(e)}

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
    return None

def analyze_position(symbol, entry_price, amt):
    """
    五维智能评分: 趋势/量价/支撑/动量/情绪
    输出整数累加评分 ⭐{tp}/{hold}/{sl}
    """
    mark = get_mark_price(symbol)
    if not mark: return ("hold", "无法获取价格", 0.5, {})
    pnl_pct = (mark - entry_price) / entry_price * 100 if amt > 0 else (entry_price - mark) / entry_price * 100
    t = curl_get(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}")
    high_24h = float(t['highPrice']) if t else mark
    low_24h = float(t['lowPrice']) if t else mark
    chg_24h = float(t['priceChangePercent']) if t else 0
    pullback = (high_24h - mark) / high_24h * 100 if high_24h > 0 else 0
    funding = curl_get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}")
    fr = float(funding['lastFundingRate']) * 100 if funding else 0
    k5 = get_klines(symbol, "5m", 24)
    k15 = get_klines(symbol, "15m", 12)
    k1h = get_klines(symbol, "1h", 8)

    scores = {"tp": 0, "hold": 0, "sl": 0}
    rtp, rhd, rsl = [], [], []

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
        d2s15 = (mark-min(c["low"] for c in k15))/mark*100 if mark > min(c["low"] for c in k15) else 999
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

    # 决策前计算动态止盈目标价（基于趋势强度 + 压力位）
    tp_target = None
    tp_reason = ""
    if k15 and k1h:
        c15_up = (k15[-1]["close"] - k15[-4]["close"]) / k15[-4]["close"] * 100 if k15[-4]["close"] else 0
        h1h_up = (k1h[-1]["close"] - k1h[-4]["close"]) / k1h[-4]["close"] * 100 if k1h[-4]["close"] else 0
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
        if resist_stuck:
            scores["tp"] += 12; rtp.append(f"压力{near_resist:.4f}冲击{touches}次未破")

    # 决策
    if pnl_pct <= -7:
        return ("sl", f"亏损{pnl_pct:.2f}%触发止损", 1.0, {"tp":0,"hold":0,"sl":0,"pnl_pct":round(pnl_pct,2),"pullback_24h":round(pullback,1),"chg_24h":round(chg_24h,1),"fr":round(fr,3)})
    max_ = max(scores["tp"], scores["hold"], scores["sl"])
    if max_ == 0: action, reason, conf = "hold", f"趋势平稳({pnl_pct:+.1f}%)", 0.5
    elif scores["tp"] == max_ and scores["tp"] > scores["hold"]: action, reason, conf = "tp", "; ".join(rtp[:2]), min(0.5+max_/60,0.95)
    elif scores["sl"] == max_ and scores["sl"] > scores["hold"]: action, reason, conf = "sl", "; ".join(rsl[:2]), min(0.5+max_/40,0.95)
    else: action, reason, conf = "hold", "; ".join(rhd[:2]) if rhd else f"趋势健康({pnl_pct:+.1f}%)", min(0.5+max_/50,0.85)
    report = {**scores, "pnl_pct": round(pnl_pct,2), "pullback_24h": round(pullback,1), "chg_24h": round(chg_24h,1), "fr": round(fr,3)}
    return (action, reason, round(conf,2), report)

def close_position(symbol, side, amt):
    abs_amt = abs(amt); side_to_close = "SELL" if amt > 0 else "BUY"
    return api_post("/fapi/v1/order", f"symbol={symbol}&side={side_to_close}&type=MARKET&quantity={abs_amt}&positionSide={side}")

def place_order(symbol, side, pos_side, qty, leverage=3):
    api_post("/fapi/v1/leverage", f"symbol={symbol}&leverage={leverage}")
    return api_post("/fapi/v1/order", f"symbol={symbol}&side={side}&type=MARKET&quantity={qty}&positionSide={pos_side}")

def set_leverage(symbol, lev): api_post("/fapi/v1/leverage", f"symbol={symbol}&leverage={lev}")

def scan_market():
    tickers = curl_get("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if not tickers: return None
    valid = [t for t in tickers if t['symbol'].endswith('USDT')
             and not any(x in t['symbol'] for x in ['USDC','BUSD','FDUSD','TUSD','USDP'])
             and float(t.get('quoteVolume',0)) >= 100000]
    gainers = sorted(valid, key=lambda x: float(x['priceChangePercent']), reverse=True)
    funding = curl_get("https://fapi.binance.com/fapi/v1/premiumIndex")
    fr_map = {f['symbol']: float(f['lastFundingRate']) for f in funding} if funding else {}
    signals = []
    candidates = []
    for t in gainers:
        sym, chg, vol, price = t['symbol'], float(t['priceChangePercent']), float(t['quoteVolume']), float(t['lastPrice'])
        if chg < 8 or chg > 60 or vol < 500000: continue
        fr = fr_map.get(sym, 0)
        pb = (float(t['highPrice']) - price) / float(t['highPrice']) * 100 if float(t['highPrice']) > 0 else 0
        candidates.append({"symbol": sym, "price": price, "change": chg, "volume": vol, "fundingRate": fr*100, "pullback": pb, "score": 0})

    # 取涨幅前40名，用潜力指标评分（不是看已经涨了多少）
    for c in candidates[:40]:
        k15 = get_klines(c["symbol"], "15m", 4)
        k1h = get_klines(c["symbol"], "1h", 5)
        k4h = get_klines(c["symbol"], "4h", 3)

        score = 0
        reasons = []

        # ① 资金费率（最大20分）— 空头挤压潜力，独立于价格位置
        fr = c["fundingRate"]
        if fr < -0.1:
            score += 20; reasons.append(f"费率{fr:.2f}%-强烈空头挤压")
        elif fr < -0.05:
            score += 15; reasons.append(f"费率{fr:.2f}%-空头挤压")
        elif fr < -0.01:
            score += 10; reasons.append(f"费率{fr:.2f}%-轻度空头")
        elif fr < 0:
            score += 5; reasons.append(f"费率{fr:.2f}%-微负")

        # ② 成交量爆发（最高15分）— 看最近1h量对比前几小时是否放量，资金刚进场
        if k1h and len(k1h) >= 4:
            last_vol = k1h[-1]["volume"]
            prev_vols = sum(k["volume"] for k in k1h[-4:-1]) / 3
            vol_surge = last_vol / prev_vols if prev_vols > 0 else 1
            if vol_surge > 3:
                score += 15; reasons.append(f"放量{vol_surge:.1f}x-资金爆发")
            elif vol_surge > 2:
                score += 10; reasons.append(f"放量{vol_surge:.1f}x-资金进场")
            elif vol_surge > 1.5:
                score += 5; reasons.append(f"放量{vol_surge:.1f}x-量能放大")

        # ③ 多周期共振刚刚形成（最高15分）— 15m和1h刚刚同时翻多，不是已经涨了很久
        c15_t = 0
        h1h_t = 0
        h4h_t = 0
        if k15 and len(k15) >= 3:
            c15_t = (k15[-1]["close"] - k15[-3]["close"]) / k15[-3]["close"] * 100
        if k1h and len(k1h) >= 3:
            h1h_t = (k1h[-1]["close"] - k1h[-3]["close"]) / k1h[-3]["close"] * 100
        if k4h and len(k4h) >= 2:
            h4h_t = (k4h[-1]["close"] - k4h[-2]["close"]) / k4h[-2]["close"] * 100

        # 刚共振：短时间内多周期同时翻多（表示趋势刚开始）
        if c15_t > 0 and h1h_t > 0:
            score += 10; reasons.append(f"15m1h共振向上")
            if h4h_t > 0:
                score += 5; reasons.append("4h也向上-全周期共振")
        elif c15_t > 0 or h1h_t > 0:
            score += 3; reasons.append("单周期向上")

        # ④ 24h涨幅（最高10分）— 基础动能，但不给太高权重
        chg = c["change"]
        if 8 <= chg <= 20:
            score += 10; reasons.append(f"涨幅{chg:.0f}%-启动区")
        elif 20 < chg <= 30:
            score += 5; reasons.append(f"涨幅{chg:.0f}%-拉升中")
        elif 30 < chg <= 60:
            score += 2; reasons.append(f"涨幅{chg:.0f}%-高位")

        c["score"] = round(score, 1)
        signals.append(c)
    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals[:30]

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
    入场时机评分（0~20分）：判断价格位置是否适合进场
    - 价格在区间低位 +8
    - 15m短期向上 +4
    - 回调止跌信号 +3
    - 回调缩量 +3
    - 不创新低 +3
    - 连跌惩罚 -5
    - 放量下跌惩罚 -5
    """
    k15 = get_klines(symbol, "15m", 22)
    if not k15 or len(k15) < 20:
        return 10, "数据不足,保守入场"
    price = k15[-1]["close"]
    hi = max(c["high"] for c in k15[-20:])
    lo = min(c["low"] for c in k15[-20:])
    rng = hi - lo
    pos_pct = (price - lo) / rng * 100 if rng > 0 else 50
    s = 0; reasons = []

    # ① 价格位置（8分）
    if pos_pct <= 25:
        s += 8; reasons.append(f"低位({pos_pct:.0f}%)")
    elif pos_pct <= 50:
        s += 5; reasons.append(f"中低位({pos_pct:.0f}%)")
    elif pos_pct <= 70:
        s += 2; reasons.append(f"中高位({pos_pct:.0f}%)")
    else:
        reasons.append(f"高位({pos_pct:.0f}%)-追高风险")

    # ② 15m短期方向（4分）
    c15_t = (k15[-1]["close"] - k15[-3]["close"]) / k15[-3]["close"] * 100
    if c15_t > 0:
        s += 4; reasons.append(f"15m向上({c15_t:+.1f}%)")
    else:
        reasons.append(f"15m向下({c15_t:+.1f}%)")

    # ③ 回调止跌K线（3分）
    last = k15[-1]
    body = abs(last["close"] - last["open"])
    wick = min(last["open"], last["close"]) - last["low"]
    if body > 0 and wick > body * 2 and c15_t > 0:
        s += 3; reasons.append("下影线止跌")
    elif body > 0 and wick > body * 1.5 and last["close"] > last["open"]:
        s += 2; reasons.append("小下影线")

    # ④ 缩量回调（3分）
    avg_vol = sum(c["volume"] for c in k15[-10:]) / 10
    last_vol = last["volume"]
    vol_r = last_vol / avg_vol if avg_vol > 0 else 1
    if last["close"] < last["open"] and vol_r < 0.7:
        s += 3; reasons.append(f"缩量回调({vol_r:.1f}x)")
    elif last["close"] < last["open"] and vol_r < 1:
        s += 1; reasons.append(f"量能正常({vol_r:.1f}x)")

    # ⑤ 不创新低（3分）
    prev3 = k15[-4:-1]
    prev3_lo = min(c["low"] for c in prev3)
    if last["low"] >= prev3_lo:
        s += 3; reasons.append("不创新低")
    else:
        reasons.append("新低")

    # ⑥ 连跌惩罚（-5分）
    cons_down = 0
    for c in reversed(k15[-5:]):
        if c["close"] < c["open"]:
            cons_down += 1
        else:
            break
    if cons_down >= 3:
        s -= 5; reasons.append(f"连{cons_down}阴-惩罚")

    # ⑦ 放量下跌惩罚（-5分）
    if last["close"] < last["open"] and vol_r > 1.5:
        s -= 5; reasons.append("放量下跌-惩罚")

    s = max(0, min(20, s))

    # 根据K线数据计算价格区间和压力位
    # 支撑位：20根15m最低点 + 最近5根15m的最低点（取更高 = 更近的支撑）
    k5_low = min(c["low"] for c in k15[-5:])
    support_price = max(lo, k5_low)
    entry_low = round(min(price, support_price * 1.01), 6)
    entry_high = round(price, 6)

    # 近端压力位：最近5根15m最高点（最近的阻力）
    k5_high = max(c["high"] for c in k15[-5:])
    near_resist = round(min(hi, k5_high), 6)

    # 远端压力位：20根15m最高点（大周期阻力）
    far_resist = round(hi, 6)

    return s, "; ".join(reasons), (entry_low, entry_high), (near_resist, far_resist)


def place_new_trade(signals=None):
    if not signals:
        signals = scan_market()
    if not signals: return None
    _, positions = get_account()
    existing = {p["symbol"] for p in positions} if positions else set()
    balances, _ = get_account()
    if not balances or "USDT" not in balances: return None
    avail = balances["USDT"]["available"]; cnt = len(positions) if positions else 0
    if cnt >= 6: return None

    # 遍历前15名，跳过已有持仓的币
    for best in signals[:15]:
        if best["symbol"] in existing: continue
        if best["score"] < 12: continue  # 门槛从6→12，过滤掉低质量信号减少亏损
        score = best["score"]
        price = best["price"]

        # 入场时机检查
        timing, timing_reason, entry_range, tp_target = entry_timing(best["symbol"])
        if timing < 10:
            continue  # 入场时机不佳，跳过等下次
        
        # 入场位置过高（>70%区间高位）直接禁止开仓 — 追高是亏损第一原因
        if "高位" in timing_reason and timing_reason.startswith("高位"):
            h_pct = 0
            import re as _re_high
            hm = _re_high.search(r'(\d+)%', timing_reason)
            if hm: h_pct = int(hm.group(1))
            if h_pct >= 70:
                continue

        if score >= 20:
            leverage = 10; target_value = 95
        elif score >= 15:
            leverage = 8; target_value = 75
        else:
            leverage = 5; target_value = 55

        max_value = min(target_value, avail * leverage * 0.6)
        if max_value < 10: continue

        qty = max(1, int(max_value / price))
        if qty * price < 5: continue

        set_leverage(best["symbol"], leverage)
        r = place_order(best["symbol"], "BUY", "LONG", qty, leverage)
        if "orderId" in r:
            return {"symbol": best["symbol"], "qty": qty, "price": price, "value": round(qty*price, 1),
                    "leverage": leverage, "reason": f"评分{score} 入场时机{timing}({timing_reason}) 涨幅{best['change']:.1f}% 费率{best['fundingRate']:+.4f}%",
                    "entry_range": entry_range, "tp_target": tp_target}
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
    三档：向下→不开仓  横盘→正常  向上→积极
    用15m+1h+4h+24h四个维度综合判断
    """
    t = curl_get("https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=BTCUSDT")
    if not t: return "unknown", "BTC数据获取失败", 1.0, 0
    btc_chg = float(t['priceChangePercent'])
    btc_price = float(t['lastPrice'])

    # 15m趋势
    k15 = get_klines("BTCUSDT", "15m", 4)
    btc_15m = 0
    if k15 and len(k15) >= 3:
        btc_15m = (k15[-1]["close"] - k15[-3]["close"]) / k15[-3]["close"] * 100

    # 1h趋势
    k1h = get_klines("BTCUSDT", "1h", 4)
    btc_1h = 0
    if k1h and len(k1h) >= 3:
        btc_1h = (k1h[-1]["close"] - k1h[-3]["close"]) / k1h[-3]["close"] * 100

    # 4h趋势
    k4h = get_klines("BTCUSDT", "4h", 3)
    btc_4h = 0
    if k4h and len(k4h) >= 2:
        btc_4h = (k4h[-1]["close"] - k4h[-2]["close"]) / k4h[-2]["close"] * 100

    # 看四个维度中涨的多还是跌的多
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

    # 横盘分化
    return "sideways", f"➡️ BTC横盘(24h{btc_chg:+.1f}% 1h{btc_1h:+.1f}%), 正常操作", 1.0, btc_price


def main():
    msg = []
    balances, positions = get_account()
    if balances is None: print("⚠️ API连接失败"); return

    # BTC大盘环境判断
    btc_status, btc_desc, btc_coeff, btc_price = check_btc_market()
    msg.append(f"🟡 BTC: ${btc_price:.0f} {btc_desc}")

    signals = scan_market()
    tracker = load_tracker()
    pos_map = {p["symbol"]: p for p in positions} if positions else {}

    # 清理已平仓的追踪记录
    for sym in list(tracker.keys()):
        if sym not in pos_map:
            del tracker[sym]

    # 移动止损 + 固定止盈
    if positions:
        for p in positions:
            mark = get_mark_price(p["symbol"])
            if not mark: continue
            entry = p["entry"]
            pnl_pct = (mark - entry) / entry * 100 if p["amt"] > 0 else (entry - mark) / entry * 100
            sym = p["symbol"]

            # 更新最高价追踪
            tr = tracker.get(sym, {"high": mark, "trail_sl": None, "tp_triggered": False})
            if pnl_pct > 0 and mark > tr.get("high", 0):
                tr["high"] = mark
                # 价格创新高，调整移动止损
                tr["trail_sl"] = round(mark * 0.94, 8)  # 从最高点回撤6%触发移动止损
            tracker[sym] = tr

            # ① 固定止损：-7%硬止损（最高优先）
            loss = (entry - mark) / entry * 100 if p["amt"] > 0 else (mark - entry) / entry * 100
            if 7 <= loss < 50:
                r = close_position(p["symbol"], p["side"], p["amt"])
                if "orderId" in r: msg.append(f"🛑 硬止损! {sym} 亏损{loss:.2f}%")
                else: msg.append(f"⚠️ {sym} 止损失败: {r.get('msg','?')}")
                tracker.pop(sym, None)
                save_tracker(tracker)
                continue

            # ② 移动止损：从高点回撤6%触发（保护利润）
            if pnl_pct > 0 and tr.get("trail_sl"):
                trail_pct = (mark - tr["trail_sl"]) / tr["trail_sl"] * 100
                if trail_pct < 0:
                    r = close_position(p["symbol"], p["side"], p["amt"])
                    if "orderId" in r: msg.append(f"⚠️ 移动止损! {sym} 高点回撤6% 锁定利润{trail_pct:.2f}%")
                    else: msg.append(f"⚠️ {sym} 移动止损失败: {r.get('msg','?')}")
                    tracker.pop(sym, None)
                    save_tracker(tracker)
                    continue

            # ③ 固定止盈移除 — 完全由评分系统动态判断止盈
            # 只有硬止损保留：-7%硬止损

    save_tracker(tracker)
    # 智能评分
    if positions:
        for p in positions:
            sym = p["symbol"]
            # 已被固定止盈/移动止损平仓的跳过
            if sym not in tracker: continue
            action, reason, conf, rpt = analyze_position(p["symbol"], p["entry"], p["amt"])
            e = "🟢" if rpt.get("pnl_pct", 0) > 0 else "🔴"
            if action == "tp" or action == "sl":
                if action == "tp" and rpt.get("pnl_pct", 0) < 0:
                    label = "止损(趋势走弱)"
                elif action == "tp":
                    label = "止盈"
                else:
                    label = "止损"
                r = close_position(p["symbol"], p["side"], p["amt"])
                if "orderId" in r:
                    msg.append(f"💰 {label}! {p['symbol']} ⭐{rpt['tp']}/{rpt['hold']}/{rpt['sl']} {reason}")
                    tracker.pop(sym, None)
                    save_tracker(tracker)
                else:
                    msg.append(f"⚠️ {p['symbol']} 平仓失败: {r.get('msg','?')}")
            else:
                tp_s = rpt.get('tp', 0)
                hd_s = rpt.get('hold', 0)
                sl_s = rpt.get('sl', 0)
                total_s = tp_s + hd_s + sl_s
                composite = round(hd_s / total_s * 100) if total_s > 0 else 50
                if composite >= 70: verdict = "建议持有 ✅"
                elif composite >= 50: verdict = "中性观察 ⚠️"
                else: verdict = "建议关注 ❌"
                msg.append(f"{e} {p['symbol']} {rpt['pnl_pct']:+.2f}% 📊综合{composite}分 {verdict} | {reason}")
    cnt = len(positions) if positions else 0
    # BTC向下时不开新仓，向上或横盘时正常开
    if btc_coeff > 0 and cnt < 4 and signals and signals[0]["score"] >= 12:
        t = place_new_trade(signals)
        if t: 
            er = t.get('entry_range', (0,0))
            nr, fr = t.get('tp_target', (0,0))
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
