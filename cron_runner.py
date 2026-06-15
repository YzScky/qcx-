#!/usr/bin/env python3
"""
币安策略直接执行→Telegram发送（不走Hermes delivery）
通过SOCKS5代理调用Telegram API
"""
import json, subprocess, os, sys, hashlib, hmac, time
from datetime import datetime

BOT_TOKEN = "8626927697:AAE45wqThmHw4wU0jZYoL0cilRVxhqbMMMQ"
CHAT_ID = "1887448779"

def send_telegram(text):
    """通过SOCKS5代理发送到Telegram"""
    if len(text) > 3900:
        text = text[:3900] + "\n\n...（截断）"
    
    data = json.dumps({"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"})
    cmd = [
        "curl", "-s", "--connect-timeout", "5", "--max-time", "15",
        "--socks5-hostname", "127.0.0.1:7891",
        "-X", "POST", "-H", "Content-Type: application/json",
        "--data-raw", data,
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        result = json.loads(r.stdout) if r.stdout.strip() else {}
        return result.get("ok", False)
    except Exception as e:
        print(f"Telegram send error: {e}")
        return False

# ====== 直接导入并执行策略 ======
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from binance_strategy import main

# 保存原始stdout
old_stdout = sys.stdout

# 捕获输出
from io import StringIO
capture = StringIO()
sys.stdout = capture

try:
    main()
except Exception as e:
    print(f"策略执行异常: {e}")

sys.stdout = old_stdout
output = capture.getvalue()

# 发送到Telegram
if output.strip():
    sent = send_telegram(output.strip())
    if sent:
        print("✅ Telegram发送成功")
    else:
        print("❌ Telegram发送失败")
else:
    print("⚠️ 空输出，未发送")

# 同时也打印到stdout（给cron log）
print(output)
