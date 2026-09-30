"""SQLite 数据层：建表、连接、行工厂。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import DATA_DIR, DB_PATH, IMG_DIR

SCHEMA = """
-- 客户
CREATE TABLE IF NOT EXISTS customers (
    code        TEXT PRIMARY KEY,          -- 客户代号
    name        TEXT NOT NULL,
    phone       TEXT DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 砂浆型号 + 当前生效价格版本
CREATE TABLE IF NOT EXISTS products (
    code        TEXT PRIMARY KEY,          -- 砂浆型号
    name        TEXT NOT NULL,
    unit_price  REAL NOT NULL,             -- 当前价格（元/吨，虚构）
    price_ver   TEXT NOT NULL,             -- 价格版本，如 2026-Q3
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 价格版本历史
CREATE TABLE IF NOT EXISTS price_versions (
    product_code TEXT NOT NULL REFERENCES products(code),
    ver          TEXT NOT NULL,
    unit_price   REAL NOT NULL,
    effective_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (product_code, ver)
);

-- 订单（一单一行，付款确认后进入提货队列）
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no        TEXT NOT NULL UNIQUE,
    customer_code   TEXT NOT NULL REFERENCES customers(code),
    product_code    TEXT NOT NULL REFERENCES products(code),
    price_ver       TEXT NOT NULL,         -- 下单时锁定的价格版本
    unit_price      REAL NOT NULL,         -- 下单时单价快照
    qty_ordered     REAL NOT NULL,         -- 订购量（吨）
    qty_locked      REAL NOT NULL DEFAULT 0, -- 被进行中车辆锁定的量
    qty_loaded      REAL NOT NULL DEFAULT 0, -- 已结算装料量
    amount_paid     REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending_payment',
                    -- pending_payment / queued / serving / completed / cancelled
    slot_start      TEXT,                  -- 预约时段起
    slot_end        TEXT,
    expected_plate  TEXT,                  -- 预约登记车辆
    expected_axle   TEXT,                  -- 要求车型
    called_at       TEXT,                  -- 呼号时刻
    requeued_at     TEXT,                  -- 过号重排时刻
    no_show         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK (qty_locked <= qty_ordered - qty_loaded + 0.000001)
);

-- 车辆档案（备案皮重用于皮重异常判定）
CREATE TABLE IF NOT EXISTS vehicles (
    plate       TEXT PRIMARY KEY,
    axle_type   TEXT NOT NULL,             -- 车型
    rated_tare  REAL NOT NULL,             -- 备案皮重（吨）
    capacity    REAL NOT NULL              -- 额定载质量（吨）
);

-- 料仓
CREATE TABLE IF NOT EXISTS silos (
    code        TEXT PRIMARY KEY,          -- 如 A1
    product_code TEXT NOT NULL REFERENCES products(code),
    stock       REAL NOT NULL,             -- 当前实物库存（吨）
    capacity    REAL NOT NULL
);

-- 车辆服务登记：一辆车同一时刻只能服务一笔订单（部分唯一索引保证）
CREATE TABLE IF NOT EXISTS vehicle_service (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plate       TEXT NOT NULL,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    status      TEXT NOT NULL,             -- active / released
    entered_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    released_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vs_plate_active
    ON vehicle_service(plate) WHERE status='active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_vs_order_active
    ON vehicle_service(order_id) WHERE status='active';

-- 过磅与装料事件流（所有重量事件追加写，不删除不改序）
CREATE TABLE IF NOT EXISTS weight_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    plate       TEXT NOT NULL,
    seq         INTEGER NOT NULL,          -- 该订单内第几轮 1,2,3…
    kind        TEXT NOT NULL,             -- tare / load / adjust / refund
    scale_reading REAL,                    -- 地磅原始读数（吨）；adjust/refund 无
    tare        REAL,                      -- 皮重
    gross       REAL,                      -- 毛重
    moisture_pct REAL,                     -- 本轮含水修正系数（%）
    net_raw     REAL,                      -- 未修正净重
    net         REAL,                      -- 出场净重（含水修正后）
    silo_code   TEXT,
    lock_qty    REAL NOT NULL DEFAULT 0,   -- 本轮在料仓/订单上的锁定量
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    created_by  TEXT NOT NULL
);

-- 装料轮次：每轮 tare->load->(dispute)->signed/rejected/extra
CREATE TABLE IF NOT EXISTS load_rounds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    plate       TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    silo_code   TEXT,
    lock_qty    REAL NOT NULL DEFAULT 0,
    dispensed   REAL NOT NULL DEFAULT 0,   -- 料仓实发（并发时按锁定量分配）
    tare        REAL,
    gross       REAL,
    moisture_pct REAL,
    net_raw     REAL,
    net         REAL,
    status      TEXT NOT NULL DEFAULT 'tared',
                -- tared / loaded / disputed / signed / rejected / extra_pending / extra
    gate_released INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(order_id, seq)
);

-- 异常留痕
CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER REFERENCES orders(id),
    plate       TEXT,
    round_id    INTEGER REFERENCES load_rounds(id),
    code        TEXT NOT NULL,
                -- vehicle_mismatch / silo_mismatch / tare_abnormal / overload /
                -- scale_outage / moisture_dispute / over_order_after_topup / customer_reject
    detail      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open',  -- open / resolved
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    resolved_at TEXT,
    resolution  TEXT DEFAULT ''
);

-- 结算快照（客户确认重量后生成，不可变）
CREATE TABLE IF NOT EXISTS settlements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL UNIQUE REFERENCES orders(id),
    plate       TEXT NOT NULL,
    total_net   REAL NOT NULL,
    unit_price  REAL NOT NULL,
    price_ver   TEXT NOT NULL,
    amount      REAL NOT NULL,
    paid        REAL NOT NULL,
    refund_qty  REAL NOT NULL DEFAULT 0,
    refund_amt  REAL NOT NULL DEFAULT 0,
    snapshot    TEXT NOT NULL,             -- JSON 明细
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    created_by  TEXT NOT NULL
);

-- 人工改重申请：操作员发起，主管复核（不同角色）
CREATE TABLE IF NOT EXISTS weight_revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id    INTEGER NOT NULL REFERENCES load_rounds(id),
    field       TEXT NOT NULL,             -- tare / gross / moisture_pct
    old_value   REAL NOT NULL,
    new_value   REAL NOT NULL,
    reason      TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    reviewed_by TEXT,
    reviewed_at TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected
    review_note TEXT DEFAULT ''
);

-- 退款单：财务发起、财务主管复核；幂等键 order_id 唯一（一单最多一笔退款）
CREATE TABLE IF NOT EXISTS refunds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL UNIQUE REFERENCES orders(id),
    idem_key    TEXT NOT NULL UNIQUE,      -- 幂等键：REFUND-<order_id>
    qty         REAL NOT NULL,
    amount      REAL NOT NULL,
    reason      TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    reviewed_by TEXT,
    reviewed_at TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected / paid
    review_note TEXT DEFAULT ''
);

-- 磅单/车辆图片（本地目录存文件，数据库存元数据）
CREATE TABLE IF NOT EXISTS images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER REFERENCES orders(id),
    plate       TEXT,
    kind        TEXT NOT NULL,             -- vehicle / weighbill
    filename    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_wevt_order ON weight_events(order_id);
CREATE INDEX IF NOT EXISTS idx_round_order ON load_rounds(order_id);
CREATE INDEX IF NOT EXISTS idx_incident_order ON incidents(order_id);
"""


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(db_path: Path | None = None) -> None:
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
