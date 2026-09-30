"""SQLite schema 与连接管理。所有业务数据均为虚构。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 客户代号与价格版本
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_versions (
    id INTEGER PRIMARY KEY,
    version TEXT NOT NULL UNIQUE,
    effective_at TEXT NOT NULL,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY,
    version TEXT NOT NULL,
    model TEXT NOT NULL,
    unit_price REAL NOT NULL,
    UNIQUE(version, model)
);

-- 料仓（每仓一种砂浆型号）与库存（公斤）
CREATE TABLE IF NOT EXISTS silos (
    id TEXT PRIMARY KEY,              -- A/B/C...
    model TEXT NOT NULL,
    capacity_kg INTEGER NOT NULL,
    stock_kg INTEGER NOT NULL
);

-- 订单
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,        -- 订单号 SO-...
    customer_code TEXT NOT NULL,
    model TEXT NOT NULL,
    silo_id TEXT NOT NULL,
    qty_kg INTEGER NOT NULL,          -- 订单数量（干砂浆公斤）
    unit_price REAL NOT NULL,
    price_version TEXT NOT NULL,
    slot TEXT NOT NULL,               -- 预约时段
    slot_seq INTEGER NOT NULL,        -- 同序时段内的排队序号
    vehicle_type TEXT NOT NULL DEFAULT '三轴罐车',
    plate TEXT,                       -- 入场车辆（后四位用于大屏）
    pay_receipt TEXT DEFAULT '',      -- 付款回执（虚构凭证号）
    status TEXT NOT NULL DEFAULT 'PAID',
        -- PAID 已付款待入场 / QUEUED 已入队 / TARE 已皮重过磅
        -- LOADING 装料中 / LOADED 已装料待毛重 / WEIGHED 已二次过磅
        -- CONFIRMED 客户已确认 / RELEASED 已放行出场
        -- REJECTED 客户拒签 / NO_SHOW 过号释放 / CANCELLED 已取消退款
    tare_kg REAL,
    gross_kg REAL,
    load_kg REAL,                     -- 累计装料原始重量（湿料，含水前）
    moisture_pct REAL DEFAULT 0,      -- 含水修正百分比（湿料含水，折算扣减）
    net_kg REAL,                      -- 出场净重（干料结算重量）
    settle_price REAL,                -- 结算单价快照
    settle_amount REAL,               -- 结算金额快照
    refilled_qty_kg INTEGER DEFAULT 0,-- 补装累计
    paid_amount REAL DEFAULT 0,
    refunded_amount REAL DEFAULT 0,
    locked_stock_kg INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    released_at TEXT,
    reject_reason TEXT DEFAULT '',
    FOREIGN KEY(silo_id) REFERENCES silos(id)
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_plate ON orders(plate);

-- 重量事件：皮重/毛重/装料/含水修正/补装/改重，全部留痕
CREATE TABLE IF NOT EXISTS weight_events (
    id INTEGER PRIMARY KEY,
    order_code TEXT NOT NULL,
    kind TEXT NOT NULL,
        -- TARE 首次过磅 / GROSS 二次过磅 / LOAD 装料 / REFILL 补装
        -- MOISTURE 含水修正 / MANUAL 人工改重 / RESET 地磅中断复位
    value_kg REAL NOT NULL,
    prev_value_kg REAL,               -- 修订时保留原值（审计）
    moisture_pct REAL,
    note TEXT DEFAULT '',
    operator TEXT NOT NULL,
    reviewer TEXT,                    -- 人工改重复核人（须与 operator 不同角色）
    created_at TEXT NOT NULL,
    FOREIGN KEY(order_code) REFERENCES orders(code)
);

-- 异常留痕
CREATE TABLE IF NOT EXISTS exceptions (
    id INTEGER PRIMARY KEY,
    order_code TEXT,
    plate TEXT,
    kind TEXT NOT NULL,
        -- VEHICLE_MISMATCH 车型不符 / SILO_WRONG 料仓错选
        -- TARE_ABNORMAL 皮重异常 / OVERLOAD 装料超量
        -- SCALE_DOWN 地磅中断 / MOISTURE_DISPUTE 含水争议
        -- REFILL_EXCEED 补装后超单 / CUSTOMER_REJECT 客户拒签
        -- NO_SHOW 过号释放（由调度任务写入）
    detail TEXT NOT NULL,
    value_kg REAL,                   -- 皮重异常等场景记录当时读数，供主管复核采纳
    operator TEXT NOT NULL,
    resolved INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

-- 复核任务：人工改重、退款必须双人不同角色
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    order_code TEXT NOT NULL,
    kind TEXT NOT NULL,               -- MANUAL_WEIGHT / REFUND
    payload TEXT NOT NULL,            -- JSON
    requested_by TEXT NOT NULL,
    request_role TEXT NOT NULL,
    reviewed_by TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING/APPROVED/REJECTED
    comment TEXT DEFAULT '',
    idempotency_key TEXT UNIQUE,      -- 退款幂等键
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

-- 用户角色（虚构）：dispatcher 调度 / weigher 司磅 / loader 装料
-- / cashier 收款结算 / supervisor 主管复核
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    display TEXT NOT NULL,
    role TEXT NOT NULL
);

-- 库存变动流水（锁定/装料/释放）
CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY,
    silo_id TEXT NOT NULL,
    order_code TEXT,
    change_kg INTEGER NOT NULL,       -- 锁定为负占用，扣减为负，释放为正
    kind TEXT NOT NULL,               -- LOCK/LOAD/RELEASE
    created_at TEXT NOT NULL
);
"""


_lock = threading.RLock()
_ctx = threading.local()
_db_path = "data/app.db"


def configure(path: str) -> None:
    """切换默认数据库（测试/启动时使用），并清空当前线程缓存连接。"""
    global _db_path
    _db_path = path
    _ctx.conn = None


def get_db(path: str | None = None) -> sqlite3.Connection:
    path = path or _db_path
    conn = getattr(_ctx, "conn", None)
    if conn is None or getattr(_ctx, "path", None) != path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        _ctx.conn = conn
        _ctx.path = path
    return conn


def init_db(path: str | None = None) -> None:
    with _lock:
        conn = get_db(path)
        conn.executescript(SCHEMA)
