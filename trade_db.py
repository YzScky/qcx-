"""
trade_db.py — 交易数据库模块（SQLite）
存储所有开/平仓记录 + 余额快照
表结构：
  - trades: 每笔交易一条记录（开仓时INSERT，平仓时UPDATE）
  - balance_snapshots: 每次cron运行的余额快照（时间序列）
"""

import json, os, sqlite3, time
from datetime import datetime

DB_PATH = os.path.expanduser("~/.hermes/scripts/trades.db")

def _get_conn():
    """获取数据库连接（自动创建表）"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _ensure_tables(conn)
    return conn

def _ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,          -- LONG / SHORT
            leverage INTEGER DEFAULT 1,
            entry_time TEXT NOT NULL,    -- YYYY-MM-DD HH:MM:SS
            entry_price REAL NOT NULL,
            entry_qty REAL NOT NULL,
            entry_value REAL NOT NULL,   -- qty * price
            entry_score INTEGER,         -- entry_timing评分
            scan_score REAL,             -- scan_market评分
            change_24h REAL,             -- 开仓时24h涨幅
            reason TEXT,                 -- 开仓理由
            exit_time TEXT,              -- 平仓时间（NULL = 仍在持有）
            exit_price REAL,             -- 平仓价
            exit_qty REAL,              -- 实际平仓数量
            exit_value REAL,             -- 平仓价值
            exit_reason TEXT,            -- 平仓原因（硬止损/结构止损/评分止损/止盈/移动止损）
            pnl REAL,                    -- 盈亏USDT
            pnl_pct REAL,                -- 盈亏百分比
            closed INTEGER DEFAULT 0,    -- 0=持有中, 1=完全平仓, 2=部分平仓
            duration_minutes INTEGER,    -- 持仓时长（分钟）
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS balance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT NOT NULL,           -- YYYY-MM-DD HH:MM
            balance REAL NOT NULL,        -- totalWalletBalance
            available REAL NOT NULL,      -- availableBalance
            position_count INTEGER DEFAULT 0,
            total_pnl REAL DEFAULT 0,     -- 总浮动PnL
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_balance_time ON balance_snapshots(time)
    """)

def open_trade(symbol, side, leverage, entry_price, qty, entry_score=None, scan_score=None, change_24h=None, reason=None):
    """记录一笔新开仓"""
    conn = _get_conn()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry_value = round(qty * entry_price, 2)
        cursor = conn.execute("""
            INSERT INTO trades (symbol, side, leverage, entry_time, entry_price, entry_qty, entry_value,
                               entry_score, scan_score, change_24h, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, side, leverage, now, entry_price, qty, entry_value,
              entry_score, scan_score, change_24h, reason))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()

def close_trade(symbol, side, exit_price, exit_qty, exit_reason, pnl=None, pnl_pct=None):
    """更新平仓记录。部分平仓时创建新行记录本次平仓，完全平仓时更新原行。"""
    conn = _get_conn()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 找该标的最新的未完全平仓记录
        cursor = conn.execute("""
            SELECT id, entry_time, entry_price, entry_qty, entry_value, coalesce(exit_qty, 0) as already_closed
            FROM trades 
            WHERE symbol=? AND side=? AND closed!=1
            ORDER BY entry_time DESC LIMIT 1
        """, (symbol, side))
        row = cursor.fetchone()
        
        if not row:
            # 没有开仓记录但平仓了（旧仓或迁移数据），创建一条
            conn.execute("""
                INSERT INTO trades (symbol, side, entry_time, entry_price, entry_qty, entry_value,
                                   exit_time, exit_price, exit_qty, exit_value, exit_reason, pnl, pnl_pct, closed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (symbol, side, "2000-01-01 00:00:00", exit_price, 0, 0,
                  now, exit_price, abs(exit_qty), round(abs(exit_qty) * exit_price, 2),
                  exit_reason, pnl, pnl_pct, 1))
            conn.commit()
            return
        
        trade_id = row["id"]
        already_closed = row["already_closed"] or 0
        total_qty = row["entry_qty"]
        remaining = total_qty - already_closed
        
        close_qty = abs(exit_qty)
        
        if close_qty >= remaining * 0.99:  # 全部平完（容差1%）
            update_qty = already_closed + remaining
            exit_value = round(close_qty * exit_price, 2)
            # 计算持仓时长
            try:
                entry_dt = datetime.strptime(row["entry_time"], "%Y-%m-%d %H:%M:%S")
                duration = int((datetime.now() - entry_dt).total_seconds() / 60)
            except:
                duration = None
            conn.execute("""
                UPDATE trades SET 
                    exit_time=?, exit_price=?, exit_qty=?, exit_value=?,
                    exit_reason=?, pnl=?, pnl_pct=?, closed=1, duration_minutes=?
                WHERE id=?
            """, (now, exit_price, update_qty, exit_value, exit_reason, pnl, pnl_pct, duration, trade_id))
        else:  # 部分平仓
            # 记录本次部分平仓为单独一行
            exit_value = round(close_qty * exit_price, 2)
            conn.execute("""
                INSERT INTO trades (symbol, side, leverage, entry_time, entry_price, entry_qty, entry_value,
                                   exit_time, exit_price, exit_qty, exit_value, exit_reason, pnl, pnl_pct, closed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (symbol, side, 1, row["entry_time"], row["entry_price"], -close_qty, 0,
                  now, exit_price, close_qty, exit_value, exit_reason, pnl, pnl_pct, 2))
            # 更新原行的已平数量
            new_closed = already_closed + close_qty
            conn.execute("UPDATE trades SET exit_qty=? WHERE id=?", (new_closed, trade_id))
        
        conn.commit()
        return trade_id
    finally:
        conn.close()

def save_balance_snapshot(balance, available, position_count=0, total_pnl=0):
    """记录余额快照"""
    conn = _get_conn()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        conn.execute("""
            INSERT INTO balance_snapshots (time, balance, available, position_count, total_pnl)
            VALUES (?, ?, ?, ?, ?)
        """, (now, balance, available, position_count, total_pnl))
        conn.commit()
    finally:
        conn.close()

def get_open_trades():
    """获取所有未完全平仓的交易"""
    conn = _get_conn()
    try:
        cursor = conn.execute("""
            SELECT * FROM trades WHERE closed=0 OR (closed=2 AND closed!=1)
            ORDER BY entry_time DESC
        """)
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

def get_trade_history(symbol=None, limit=100):
    """获取已平仓历史"""
    conn = _get_conn()
    try:
        if symbol:
            cursor = conn.execute("""
                SELECT * FROM trades WHERE closed=1 AND symbol=? ORDER BY exit_time DESC LIMIT ?
            """, (symbol, limit))
        else:
            cursor = conn.execute("""
                SELECT * FROM trades WHERE closed=1 ORDER BY exit_time DESC LIMIT ?
            """, (limit,))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

def get_balance_history(hours=48):
    """获取余额历史"""
    conn = _get_conn()
    try:
        cutoff = (datetime.now().timestamp() - hours * 3600)
        cursor = conn.execute("""
            SELECT * FROM balance_snapshots 
            WHERE time >= datetime(?, 'unixepoch', 'localtime')
            ORDER BY time ASC
        """, (cutoff,))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

def get_stats():
    """获取交易统计"""
    conn = _get_conn()
    try:
        stats = {}
        cursor = conn.execute("""
            SELECT COUNT(*) as total, 
                   SUM(CASE WHEN pnl IS NOT NULL AND pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl IS NOT NULL AND pnl < 0 THEN 1 ELSE 0 END) as losses,
                   COALESCE(SUM(pnl), 0) as total_pnl,
                   COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct,
                   COALESCE(AVG(duration_minutes), 0) as avg_duration
            FROM trades WHERE closed=1
        """)
        row = cursor.fetchone()
        if row:
            stats = dict(row)
            stats["win_rate"] = round(stats["wins"] / stats["total"] * 100, 1) if stats["total"] > 0 else 0
        cursor = conn.execute("SELECT COUNT(*) as open_count FROM trades WHERE closed=0 OR closed=2")
        row = cursor.fetchone()
        stats["open_trades"] = row["open_count"] if row else 0
        return stats
    finally:
        conn.close()

def migrate_from_trade_log():
    """尝试从旧的trade_log.json导入数据（一次性迁移）"""
    log_file = os.path.expanduser("~/.hermes/scripts/trade_log.json")
    if not os.path.exists(log_file):
        return 0
    try:
        with open(log_file) as f:
            data = json.load(f)
    except:
        return 0
    
    conn = _get_conn()
    count = 0
    try:
        # 检查是否已有数据
        cursor = conn.execute("SELECT COUNT(*) as c FROM trades")
        if cursor.fetchone()["c"] > 0:
            return 0  # 已有数据，不重复迁移
        
        for entry in data:
            if entry.get("action") == "开仓":
                conn.execute("""
                    INSERT INTO trades (symbol, side, entry_time, entry_price, entry_qty, entry_value,
                                       entry_score, scan_score, change_24h, reason)
                    VALUES (?, 'LONG', ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    entry["symbol"],
                    _convert_time(entry.get("time", "")),
                    entry["price"],
                    entry.get("qty", 0),
                    entry.get("value", 0),
                    entry.get("entry_score"),
                    entry.get("scan_score"),
                    entry.get("change_24h"),
                    entry.get("reason", "")
                ))
                count += 1
            elif "balance" in entry:
                # 余额快照
                conn.execute("""
                    INSERT INTO balance_snapshots (time, balance, available, position_count)
                    VALUES (?, ?, ?, ?)
                """, (
                    _convert_time(entry.get("time", "")),
                    entry["balance"],
                    entry.get("available", 0),
                    len(entry.get("positions", []))
                ))
                count += 1
        
        conn.commit()
    finally:
        conn.close()
    return count

def _convert_time(t_str):
    """转换 MM/DD HH:MM 格式为 YYYY-MM-DD HH:MM:SS"""
    try:
        now = datetime.now()
        parts = t_str.strip().split()
        if len(parts) >= 2:
            md = parts[0].split("/")
            if len(md) == 2:
                month, day = int(md[0]), int(md[1])
                year = now.year
                # 如果月份大于当前月，可能是去年
                if month > now.month:
                    year -= 1
                return f"{year}-{month:02d}-{day:02d} {parts[1]}:00"
    except:
        pass
    return t_str

if __name__ == "__main__":
    # 测试
    conn = _get_conn()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r["name"] for r in cursor.fetchall()]
    print(f"✅ 数据库就绪: {DB_PATH}")
    print(f"   表: {tables}")
    
    # 尝试迁移旧数据
    migrated = migrate_from_trade_log()
    if migrated:
        print(f"   已从trade_log.json迁移 {migrated} 条记录")
    else:
        print("   无旧数据迁移")
    
    print(f"   当前开仓: {get_stats()['open_trades']}")
    print(f"   已平仓: {get_stats()['total']}")
    conn.close()
