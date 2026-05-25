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

        # ① 趋势
        up = sum(1 for v in [c5_t, c15_t, h1h_t] if v > 0)
        dn = sum(1 for v in [c5_t, c15_t, h1h_t] if v < 0)
        if up >= 2: scores["hold"] += 12; rhd.append(f"共振向上({c5_t:+.1f}%/{c15_t:+.1f}%/{h1h_t:+.1f}%)")
        elif dn >= 2: scores["tp"] += 8; rtp.append(f"共振向下({c5_t:+.1f}%/{c15_t:+.1f}%/{h1h_t:+.1f}%)")
        if len(k15) >= 4:
            rg = [(k15[i]["high"]-k15[i]["low"])/k15[i]["low"]*100 for i in range(-4,-1)]
            if rg[-1] < rg[-2]*0.6: scores["hold" if h1h_t>0 else "tp"] += 5; (rhd if h1h_t>0 else rtp).append("波幅收敛")
            elif rg[-1] > rg[-2]*1.8: scores["hold" if c15_t>0 else "tp"] += 6; (rhd if c15_t>0 else rtp).append("波幅扩张")

        # ② 量价
        bv = [c["volume"] for c in k5[-8:] if c["close"] < c["open"]]
        lv = [c["volume"] for c in k5[-8:] if c["close"] >= c["open"]]
        ab = sum(bv)/len(bv) if bv else 0; al = sum(lv)/len(lv) if lv else ab
        bbr = (ab/al) if (ab and al) else 1.0
        if bbr > 1.5 and c15_t < 0: scores["tp"] += 10; rtp.append(f"卖盘{bbr:.1f}倍买盘")
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

        # ⑤ 市场情绪
        if fr < -0.5: scores["hold"] += 6; rhd.append(f"负费率{fr:.3f}%")
        elif fr > 0.1: scores["tp"] += 4; rtp.append(f"正费率{fr:.3f}%")
        if 3 <= pullback <= 8 and chg_24h > 10: scores["hold"] += 4; rhd.append("健康回调")

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
        score = (abs(fr)*3 if fr < 0 else 0) + (pb if 3 <= pb <= 15 else 0) + (10 if 10 <= chg <= 40 else 0) + min(vol/10000000, 5)
        candidates.append({"symbol": sym, "price": price, "change": chg, "volume": vol, "fundingRate": fr*100, "pullback": pb, "score": round(score,1), "base_score": score})
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # 对前25名做K线趋势评分加成（权重提高到总评分的50%+）
    for c in candidates[:25]:
        k15 = get_klines(c["symbol"], "15m", 6)
        k1h = get_klines(c["symbol"], "1h", 8)
        k4h = get_klines(c["symbol"], "4h", 4)

        c["score"] = 0  # 重置，以K线趋势为主重新打分

        # 原静态基础分（权重30%）
        base = c["base_score"]

        # 15分钟趋势（权重20%）
        trend_15 = 0
        if k15 and len(k15) >= 3:
            c15_t = (k15[-1]["close"] - k15[-3]["close"]) / k15[-3]["close"] * 100
            trend_15 = c15_t * 2  # 1%趋势=2分
            # 15分钟最后K线量能验证
            last = k15[-1]
            body = last["close"] - last["open"]
            avg_vol = sum(k["volume"] for k in k15) / len(k15)
            vol_r = last["volume"] / avg_vol if avg_vol > 0 else 1
            if body > 0 and vol_r > 1.3: trend_15 += 4
            elif body < 0 and vol_r > 1.3: trend_15 -= 4
            # 连续阳线加分
            cons_up = sum(1 for k in k15[-3:] if k["close"] > k["open"])
            if cons_up >= 2: trend_15 += 3
            elif cons_up <= 1: trend_15 -= 2

        # 1小时趋势（权重30%）
        trend_1h = 0
        if k1h and len(k1h) >= 4:
            c1h_short = (k1h[-1]["close"] - k1h[-3]["close"]) / k1h[-3]["close"] * 100
            c1h_long = (k1h[-1]["close"] - k1h[-4]["close"]) / k1h[-4]["close"] * 100
            trend_1h = (c1h_short * 3 + c1h_long * 2) / 2  # 短周期权重更高
            # 1小时放量验证
            last = k1h[-1]
            body = last["close"] - last["open"]
            avg_vol = sum(k["volume"] for k in k1h) / len(k1h)
            vol_r = last["volume"] / avg_vol if avg_vol > 0 else 1
            if body > 0 and vol_r > 1.3: trend_1h += 5
            elif body < 0 and vol_r > 1.3: trend_1h -= 5
            # 趋势方向一致性
            cons_down = sum(1 for k in k1h[-4:] if k["close"] < k["open"])
            if cons_down >= 3: trend_1h -= 4

        # 4小时趋势（权重20%）
        trend_4h = 0
        if k4h and len(k4h) >= 2:
            c4h_t = (k4h[-1]["close"] - k4h[-2]["close"]) / k4h[-2]["close"] * 100
            trend_4h = c4h_t * 3
            # 4小时放量
            last = k4h[-1]
            body = last["close"] - last["open"]
            avg_vol = sum(k["volume"] for k in k4h) / len(k4h) if len(k4h) > 1 else last["volume"]
            vol_r = last["volume"] / avg_vol if avg_vol > 0 else 1
            if body > 0 and vol_r > 1.3: trend_4h += 6
            elif body < 0 and vol_r > 1.3: trend_4h -= 6

        c["score"] = round(base * 0.3 + trend_15 + trend_1h + trend_4h, 1)
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
    入场时机评分（0~20分）：
    - 价格在区间低位 +8
    - 15m趋势向上 +4
    - 回调止跌信号 +3
    - 回调缩量 +3
    - 不创新低 +3
    - 连跌惩罚 -5
    - 放量下跌惩罚 -5
    """
    k15 = get_klines(symbol, "15m", 24)
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

    # ② 15m短期趋势（4分）
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
    return s, "; ".join(reasons)


def place_new_trade():
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
        if best["score"] < 6: continue
        score = best["score"]
        price = best["price"]

        # 入场时机检查
        timing, timing_reason = entry_timing(best["symbol"])
        if timing < 10:
            continue  # 入场时机不佳，跳过等下次

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
                    "leverage": leverage, "reason": f"评分{score} 入场时机{timing}({timing_reason}) 涨幅{best['change']:.1f}% 费率{best['fundingRate']:+.4f}% 回调{best['pullback']:.1f}%"}
    return None

def main():
    msg = []
    balances, positions = get_account()
    if balances is None: print("⚠️ API连接失败"); return
    # 硬止损
    if positions:
        for p in positions:
            mark = get_mark_price(p["symbol"])
            if not mark: continue
            loss = (p["entry"] - mark) / p["entry"] * 100 if p["amt"] > 0 else (mark - p["entry"]) / p["entry"] * 100
            if 7 <= loss < 50:
                r = close_position(p["symbol"], p["side"], p["amt"])
                if "orderId" in r: msg.append(f"🛑 止损! {p['symbol']} 亏损{loss:.2f}%")
                else: msg.append(f"⚠️ {p['symbol']} 止损失败: {r.get('msg','?')}")
    signals = scan_market()
    # 智能评分
    if positions:
        for p in positions:
            action, reason, conf, rpt = analyze_position(p["symbol"], p["entry"], p["amt"])
            e = "🟢" if rpt.get("pnl_pct", 0) > 0 else "🔴"
            if action == "tp":
                r = close_position(p["symbol"], p["side"], p["amt"])
                if "orderId" in r: msg.append(f"💰 止盈! {p['symbol']} ⭐{rpt['tp']}/{rpt['hold']}/{rpt['sl']} {reason}")
                else: msg.append(f"⚠️ {p['symbol']} 止盈失败: {r.get('msg','?')}")
            elif action == "sl":
                r = close_position(p["symbol"], p["side"], p["amt"])
                if "orderId" in r: msg.append(f"🛑 止损! {p['symbol']} ⭐{rpt['tp']}/{rpt['hold']}/{rpt['sl']} {reason}")
                else: msg.append(f"⚠️ {p['symbol']} 止损失败: {r.get('msg','?')}")
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
    if cnt < 4 and signals and signals[0]["score"] >= 6:
        t = place_new_trade()
        if t: msg.append(f"🚀 新开仓! {t['symbol']} {t['qty']}张 {t['leverage']}x 价值${t['value']} @${t['price']:.4f} ({t['reason']})")
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
