"""种子数据：重置库并构造覆盖全流程的虚构订单，预置超量、断磅、拒签等异常样例。

由 run.py --reset 或启动时自动执行一次。车辆牌号、金额、地磅读数均为虚构。
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import adapters
from . import images as img_mod
from . import store
from .db import get_db, init_db

USERS = [
    ("zhang", "张调度", "dispatcher"),
    ("li", "李司磅", "weigher"),
    ("wang", "王装载", "loader"),
    ("zhao", "赵收银", "cashier"),
    ("sun", "孙主管", "supervisor"),
]

CUSTOMERS = [
    ("C01", "城东建工（虚构）"),
    ("C02", "宏基装饰（虚构）"),
    ("C03", "永固市政（虚构）"),
    ("C04", "南沙房建（虚构）"),
]

SILOS = [
    ("A", "M10", 80000, 12000),
    ("B", "M7.5", 80000, 9000),
    ("C", "M5", 80000, 8000),
]

PRICE_VERSION = "2026-Q3"
PRICES = {"M5": 0.42, "M7.5": 0.45, "M10": 0.48, "M15": 0.52}


def reset_db(path: str = "data/app.db") -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(path + suffix)
        if p.exists():
            p.unlink()
    for d in (img_mod.VEHICLE_DIR, img_mod.SLIP_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
    init_db(path)


def _slot(offset_min: int) -> str:
    # 对齐到 5 分钟的预约格子
    t = datetime.now() + timedelta(minutes=offset_min)
    t = t.replace(minute=(t.minute // 5) * 5, second=0, microsecond=0)
    return t.strftime("%H:%M")


def _master_data(conn: sqlite3.Connection) -> None:
    conn.executemany("INSERT INTO users(username,display,role) VALUES(?,?,?)", USERS)
    conn.executemany("INSERT INTO customers(id,code,name) VALUES(NULL,?,?)", CUSTOMERS)
    conn.execute("INSERT INTO price_versions(version,effective_at,note) VALUES(?,?,?)",
                 (PRICE_VERSION, f"{datetime.now():%Y-%m-%d}", "第三季度指导价（虚构）"))
    conn.executemany("INSERT INTO prices(version,model,unit_price) VALUES(?,?,?)",
                     [(PRICE_VERSION, m, p) for m, p in PRICES.items()])
    conn.executemany("INSERT INTO silos(id,model,capacity_kg,stock_kg) VALUES(?,?,?,?)", SILOS)


def seed(path: str = "data/app.db", reset: bool = True) -> dict:
    if reset:
        reset_db(path)
    init_db(path)
    conn = get_db(path)
    _master_data(conn)
    adapters.scale.set_online(True)

    def new_order(cust, model, silo, qty, slot_off, vtype="三轴罐车"):
        return store.create_order(customer_code=cust, model=model, silo_id=silo, qty_kg=qty,
                                  slot=_slot(slot_off), vehicle_type=vtype,
                                  price_version=PRICE_VERSION)

    def arrive(code, plate, vtype="三轴罐车"):
        store.check_in(code, plate, vtype)

    made: dict[str, str] = {}

    # ---------- 各环节演示订单 ----------
    # 1) 已入队，等待首次过磅
    c = new_order("C01", "M10", "A", 3000, 90); arrive(c, "沪B·7K203"); made["queued"] = c
    # 2) 已皮重过磅并锁定库存，等待装料
    c = new_order("C02", "M7.5", "B", 2500, 80); arrive(c, "沪B·5T886")
    store.weigh_tare(c); made["tare"] = c
    # 3) 装料中（已装 1200/2000）
    c = new_order("C03", "M5", "C", 2000, 70); arrive(c, "沪C·9D157")
    store.weigh_tare(c)
    store.load_material(c, "C", 1200, 0.0, operator="wang"); made["loading"] = c
    # 4) 装料完成待复磅
    c = new_order("C04", "M5", "C", 2000, 60); arrive(c, "沪A·3M662")
    store.weigh_tare(c)
    store.load_material(c, "C", 2000, 0.0, operator="wang")
    store.finish_loading(c); made["loaded"] = c
    # 5) 已二次过磅，等待客户确认
    c = new_order("C01", "M5", "C", 1500, 50); arrive(c, "沪B·8Q041")
    store.weigh_tare(c)
    store.load_material(c, "C", 1500, 0.0, operator="wang")
    store.finish_loading(c)
    store.weigh_gross(c); made["weighed"] = c
    # 6) 客户已确认，等待放行
    c = new_order("C02", "M10", "A", 2000, 40); arrive(c, "沪A·6F308")
    store.weigh_tare(c)
    store.load_material(c, "A", 2000, 0.0, operator="wang")
    store.finish_loading(c)
    store.weigh_gross(c)
    store.customer_confirm(c, True); made["confirmed"] = c
    # 7) 已放行出场（终态）
    c = new_order("C04", "M10", "A", 2000, -120); arrive(c, "沪C·2H795")
    store.weigh_tare(c)
    store.load_material(c, "A", 2000, 0.0, operator="wang")
    store.finish_loading(c)
    store.weigh_gross(c)
    store.customer_confirm(c, True)
    store.release_gate(c); made["released"] = c

    # ---------- 异常样例 ----------
    # 8) 装料超量：3000kg 订单尝试装 3200kg（被阻止并留痕，订单仍可正常装料）
    c = new_order("C03", "M10", "A", 3000, 30); arrive(c, "沪A·1Z520")
    store.weigh_tare(c)
    try:
        store.load_material(c, "A", 3200, 0.0, operator="wang")
    except store.DomainError:
        pass
    # 同单演示料仓错选
    try:
        store.load_material(c, "B", 100, 0.0, operator="wang")
    except store.DomainError:
        pass
    made["overload"] = c

    # 9) 补装后超单：已装 1800/2000 并复磅，补装 300kg 被阻止
    c = new_order("C01", "M5", "C", 2000, 20); arrive(c, "沪C·4V738")
    store.weigh_tare(c)
    store.load_material(c, "C", 1800, 0.0, operator="wang")
    store.finish_loading(c)
    store.weigh_gross(c)
    try:
        store.load_material(c, "C", 300, 0.0, operator="wang")
    except store.DomainError:
        pass
    made["refill_exceed"] = c

    # 10) 地磅中断：首磅时断磅留痕，恢复后可重试
    c = new_order("C02", "M7.5", "B", 2000, 15, vtype="牵引罐车")
    arrive(c, "沪B·0K914", vtype="牵引罐车")
    adapters.scale.set_online(False)
    try:
        store.weigh_tare(c)
    except store.DomainError:
        pass
    adapters.scale.set_online(True)
    made["scale_down"] = c

    # 11) 客户拒签：复磅后客户拒认重量，挂起
    c = new_order("C03", "M7.5", "B", 2000, 10); arrive(c, "沪A·7P260")
    store.weigh_tare(c)
    store.load_material(c, "B", 1980, 2.0, operator="wang")
    store.finish_loading(c)
    store.weigh_gross(c, moisture_pct=2.0)
    store.customer_confirm(c, False, reason="认为净重偏轻，拒绝签字")
    made["rejected"] = c

    # 12) 含水争议：客户对 5% 修正有异议，待主管裁定
    c = new_order("C04", "M7.5", "B", 2500, 5); arrive(c, "沪B·3R607")
    store.weigh_tare(c)
    store.load_material(c, "B", 2500, 5.0, operator="wang")
    store.finish_loading(c)
    store.weigh_gross(c, moisture_pct=5.0, disputed=True)
    made["moisture_dispute"] = c

    # 13) 过号：预约时段已过 15 分钟以上仍未入场，调度任务将自动释放
    c = new_order("C01", "M10", "A", 1000, -30); made["no_show"] = c

    # 14) 皮重异常：两轴罐车（合理 6000-9500kg）地磅偏差 +4000kg，首磅被拒留痕，
    #     订单留在入场队列，主管可在作业台“强制采纳皮重”
    c = new_order("C02", "M7.5", "B", 1500, 25, vtype="两轴罐车")
    arrive(c, "沪A·8W449", vtype="两轴罐车")
    adapters.scale.set_bias(4000)
    try:
        store.weigh_tare(c)
    except store.DomainError:
        pass
    adapters.scale.set_bias(0)
    made["tare_abnormal"] = c

    # 15) 车型不符：登记三轴罐车，到场为牵引罐车，入场被阻并留痕，订单留在已付款
    c = new_order("C03", "M5", "C", 2000, 35, vtype="三轴罐车")
    try:
        arrive(c, "沪C·5Y312", vtype="牵引罐车")
    except store.DomainError:
        pass
    made["vehicle_mismatch"] = c

    return made
