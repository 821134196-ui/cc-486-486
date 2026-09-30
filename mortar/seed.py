"""种子数据：虚构客户、型号、价格版本、车辆、料仓与全套样例订单。

覆盖：正常结算+补装+退款、排队等待、过磅中、装料超量、地磅中断、
客户拒签、含水争议待复核、车型不符留痕、过号释放、最后一段库存并发分配。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import adapters
from . import services as svc
from .adapters import scale
from .db import get_conn, init_db

CUSTOMERS = [
    ("C001", "城建第一工程公司", "139-0000-1001"),
    ("C002", "市政道路养护二队", "139-0000-2002"),
    ("C003", "宏远装饰工程公司", "139-0000-3003"),
    ("C004", "路桥建设第三处", "139-0000-4004"),
]

PRODUCTS = [
    # code, name, Q2价, Q3价
    ("M5", "M5 砌筑砂浆", 412.0, 428.0),
    ("M10", "M10 抹灰砂浆", 458.0, 476.0),
    ("M15", "M15 地面砂浆", 502.0, 521.0),
    ("M20", "M20 结构加固砂浆", 566.0, 588.0),
]

VEHICLES = [
    # 车牌, 车型, 备案皮重, 额定载质量（吨，虚构）
    ("京AF8301", "三轴罐车", 12.50, 25.0),
    ("京AF8302", "四轴罐车", 14.20, 31.0),
    ("京AK6108", "两轴罐车", 8.60, 18.0),
    ("京AT9921", "三轴罐车", 12.80, 25.0),
    ("京AF7702", "两轴罐车", 8.90, 18.0),
    ("京AF6603", "三轴罐车", 12.40, 25.0),
    ("京AF5519", "四轴罐车", 14.60, 31.0),
    ("京AF3308", "两轴罐车", 8.70, 18.0),
    ("京AF2207", "三轴罐车", 12.60, 25.0),
    ("京AF1109", "三轴罐车", 12.70, 25.0),
]

SILOS = [
    ("A1", "M10", 60.0),
    ("A2", "M15", 40.0),
    ("B1", "M5", 50.0),
    ("B2", "M20", 45.0),
]


def _slot(offset_min: int, dur_min: int = 30) -> tuple[str, str]:
    t = datetime.now() + timedelta(minutes=offset_min)
    return (t.strftime("%Y-%m-%d %H:%M"),
            (t + timedelta(minutes=dur_min)).strftime("%Y-%m-%d %H:%M"))


def seed(db_path=None) -> dict:
    init_db(db_path)
    adapters.set_db_path(db_path)
    scale.outage = False
    conn = get_conn(db_path)
    conn.executescript("""
        DELETE FROM images; DELETE FROM refunds; DELETE FROM settlements;
        DELETE FROM weight_revisions; DELETE FROM incidents; DELETE FROM load_rounds;
        DELETE FROM weight_events; DELETE FROM vehicle_service; DELETE FROM orders;
        DELETE FROM silos; DELETE FROM vehicles; DELETE FROM price_versions;
        DELETE FROM products; DELETE FROM customers;
    """)
    conn.commit()
    conn.close()

    # 清理上一批占位图片
    img_dir = adapters.get_img_dir()
    for old in img_dir.glob("*.png"):
        old.unlink()

    for code, name, phone in CUSTOMERS:
        conn = get_conn(db_path)
        conn.execute("INSERT INTO customers(code,name,phone) VALUES(?,?,?)",
                     (code, name, phone))
        conn.commit()
        conn.close()
    conn = get_conn(db_path)
    for code, name, q2, q3 in PRODUCTS:
        conn.execute(
            "INSERT INTO products(code,name,unit_price,price_ver) VALUES(?,?,?,?)",
            (code, name, q3, "2026-Q3"),
        )
        conn.execute(
            "INSERT INTO price_versions(product_code,ver,unit_price) VALUES(?,?,?)",
            (code, "2026-Q2", q2),
        )
        conn.execute(
            "INSERT INTO price_versions(product_code,ver,unit_price) VALUES(?,?,?)",
            (code, "2026-Q3", q3),
        )
    for plate, axle, tare, cap in VEHICLES:
        conn.execute("INSERT INTO vehicles(plate,axle_type,rated_tare,capacity)"
                     " VALUES(?,?,?,?)", (plate, axle, tare, cap))
    for code, prod, stock in SILOS:
        conn.execute("INSERT INTO silos(code,product_code,stock,capacity)"
                     " VALUES(?,?,?,60)", (code, prod, stock))
    conn.commit()
    conn.close()

    t = lambda off: _slot(off)
    ids = {}

    # ---- O1：正常流程 + 补装 + 结算 + 退款待复核 ----
    o1 = svc.create_order(customer_code="C001", product_code="M10", qty=30.0,
                          slot_start=t(-60)[0], slot_end=t(-60)[1],
                          expected_plate="京AF8301", expected_axle="三轴罐车")
    svc.confirm_payment(o1)
    svc.call_order(o1)
    r1 = svc.enter_and_tare(order_id=o1, plate="京AF8301", axle_type="三轴罐车",
                            by="dispatcher-张敏")
    tare1 = svc._conn().execute("SELECT tare FROM load_rounds WHERE id=?",
                                (r1,)).fetchone()["tare"]
    svc.begin_load(r1, "A1", 20.0)
    svc.record_gross(r1, gross_override=round(tare1 + 20.0, 3), moisture_pct=3.0)
    svc.customer_sign(r1)
    r1b = svc.request_topup(o1)
    tare1b = svc._conn().execute("SELECT tare FROM load_rounds WHERE id=?",
                                 (r1b,)).fetchone()["tare"]
    svc.begin_load(r1b, "A1", 9.7)
    svc.record_gross(r1b, gross_override=round(tare1b + 20.0 + 9.7, 3),
                     moisture_pct=3.0)
    svc.customer_sign(r1b)
    svc.release_gate(o1, actor="dispatcher-张敏")
    svc.request_refund(o1, "出场净重 28.82t，按订单 30t 预付，退还差额",
                       actor="finance-李娜", role="finance")
    ids["happy"] = o1

    # ---- O2：排队等待呼号 ----
    o2 = svc.create_order(customer_code="C002", product_code="M5", qty=25.0,
                          slot_start=t(20)[0], slot_end=t(20)[1],
                          expected_plate="京AF8302", expected_axle="四轴罐车")
    svc.confirm_payment(o2)
    ids["waiting"] = o2

    # ---- O3：已入场过完皮重，等待选料仓装料 ----
    o3 = svc.create_order(customer_code="C003", product_code="M15", qty=20.0,
                          slot_start=t(-15)[0], slot_end=t(-15)[1],
                          expected_plate="京AK6108", expected_axle="两轴罐车")
    svc.confirm_payment(o3)
    svc.call_order(o3)
    svc.enter_and_tare(order_id=o3, plate="京AK6108", axle_type="两轴罐车",
                       by="dispatcher-张敏")
    ids["tared"] = o3

    # ---- O4：装料超量（净重 26.1t > 额定 25t），异常未关闭，闸门冻结 ----
    o4 = svc.create_order(customer_code="C004", product_code="M20", qty=40.0,
                          slot_start=t(-30)[0], slot_end=t(-30)[1],
                          expected_plate="京AT9921", expected_axle="三轴罐车")
    svc.confirm_payment(o4)
    svc.call_order(o4)
    r4 = svc.enter_and_tare(order_id=o4, plate="京AT9921", axle_type="三轴罐车",
                            by="dispatcher-张敏")
    svc.begin_load(r4, "B2", 26.1)
    tare4 = svc._conn().execute("SELECT tare FROM load_rounds WHERE id=?",
                                (r4,)).fetchone()["tare"]
    svc.record_gross(r4, gross_override=round(tare4 + 26.1, 3), moisture_pct=3.0)
    ids["overload"] = o4

    # ---- O5：地磅中断，装料后无法二次过磅 ----
    o5 = svc.create_order(customer_code="C001", product_code="M10", qty=15.0,
                          slot_start=t(-10)[0], slot_end=t(-10)[1],
                          expected_plate="京AF7702", expected_axle="两轴罐车")
    svc.confirm_payment(o5)
    svc.call_order(o5)
    r5 = svc.enter_and_tare(order_id=o5, plate="京AF7702", axle_type="两轴罐车",
                            by="dispatcher-张敏")
    svc.begin_load(r5, "A1", 14.5)
    scale.outage = True
    try:
        svc.record_gross(r5)
    except svc.DomainError as e:
        assert e.code == "scale_outage"
    scale.outage = False
    ids["outage"] = o5

    # ---- O6：客户拒签，等待主管处理 ----
    o6 = svc.create_order(customer_code="C002", product_code="M15", qty=12.0,
                          slot_start=t(-20)[0], slot_end=t(-20)[1],
                          expected_plate="京AF6603", expected_axle="三轴罐车")
    svc.confirm_payment(o6)
    svc.call_order(o6)
    r6 = svc.enter_and_tare(order_id=o6, plate="京AF6603", axle_type="三轴罐车",
                            by="dispatcher-张敏")
    svc.begin_load(r6, "A2", 11.8)
    tare6 = svc._conn().execute("SELECT tare FROM load_rounds WHERE id=?",
                                (r6,)).fetchone()["tare"]
    svc.record_gross(r6, gross_override=round(tare6 + 11.8, 3), moisture_pct=3.0)
    svc.customer_reject(r6, "现场目测砂浆塌落度与型号标签不符，拒绝签字")
    ids["reject"] = o6

    # ---- O7：含水争议，改重申请待主管复核（3% → 5%）----
    o7 = svc.create_order(customer_code="C003", product_code="M5", qty=22.0,
                          slot_start=t(-25)[0], slot_end=t(-25)[1],
                          expected_plate="京AF5519", expected_axle="四轴罐车")
    svc.confirm_payment(o7)
    svc.call_order(o7)
    r7 = svc.enter_and_tare(order_id=o7, plate="京AF5519", axle_type="四轴罐车",
                            by="dispatcher-张敏")
    svc.begin_load(r7, "B1", 21.5)
    tare7 = svc._conn().execute("SELECT tare FROM load_rounds WHERE id=?",
                                (r7,)).fetchone()["tare"]
    svc.record_gross(r7, gross_override=round(tare7 + 21.5, 3), moisture_pct=3.0)
    svc.dispute_moisture(r7, claimed_pct=5.0, by="customer-王经理")
    ids["dispute"] = o7

    # ---- O8：车型不符留痕（三轴车到场，预约要求两轴），主管已劝退关闭 ----
    o8 = svc.create_order(customer_code="C004", product_code="M10", qty=18.0,
                          slot_start=t(40)[0], slot_end=t(40)[1],
                          expected_plate="", expected_axle="两轴罐车")
    svc.confirm_payment(o8)
    svc.call_order(o8)
    try:
        svc.enter_and_tare(order_id=o8, plate="京AF1109", axle_type="三轴罐车",
                           by="dispatcher-张敏")
    except svc.DomainError as e:
        assert e.code == "vehicle_mismatch"
    inc = svc._conn().execute(
        "SELECT id FROM incidents WHERE code='vehicle_mismatch' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    svc.resolve_incident(inc["id"], "三轴车与预约车型不符，现场劝返，等两轴车到场",
                         actor="supervisor-赵强", role="supervisor")
    ids["mismatch"] = o8

    # ---- O9：呼号后超时未入场（过号，后台任务将释放并计数）----
    o9 = svc.create_order(customer_code="C001", product_code="M5", qty=16.0,
                          slot_start=t(5)[0], slot_end=t(5)[1],
                          expected_plate="京AF3308", expected_axle="两轴罐车")
    svc.confirm_payment(o9)
    svc.call_order(o9)
    c2 = get_conn(db_path)
    c2.execute("UPDATE orders SET called_at=datetime('now','localtime','-120 seconds')"
               " WHERE id=?", (o9,))
    c2.commit()
    c2.close()
    ids["noshow"] = o9

    # ---- O10/O11：最后一段库存并发装料（A2 仓仅余 8t，各申请 5t → 5t + 3t）----
    c3 = get_conn(db_path)
    c3.execute("UPDATE silos SET stock=8.0 WHERE code='A2'")
    c3.commit()
    c3.close()
    o10 = svc.create_order(customer_code="C004", product_code="M15", qty=10.0,
                           slot_start=t(-5)[0], slot_end=t(-5)[1],
                           expected_plate="京AF3308", expected_axle="两轴罐车")
    # 注：F3308 同时是 O9 预约车，但 O9 尚未入场，无活跃服务登记，样例可并行
    svc.confirm_payment(o10)
    svc.call_order(o10)
    r10 = svc.enter_and_tare(order_id=o10, plate="京AF3308", axle_type="两轴罐车",
                             by="dispatcher-张敏")
    g10 = svc.begin_load(r10, "A2", 5.0)

    o11 = svc.create_order(customer_code="C002", product_code="M15", qty=10.0,
                           slot_start=t(-5)[0], slot_end=t(-5)[1],
                           expected_plate="京AF2207", expected_axle="三轴罐车")
    svc.confirm_payment(o11)
    svc.call_order(o11)
    r11 = svc.enter_and_tare(order_id=o11, plate="京AF2207", axle_type="三轴罐车",
                             by="dispatcher-张敏")
    g11 = svc.begin_load(r11, "A2", 5.0)
    ids["race_a"], ids["race_grant_a"] = o10, g10["grant"]
    ids["race_b"], ids["race_grant_b"] = o11, g11["grant"]

    return ids
