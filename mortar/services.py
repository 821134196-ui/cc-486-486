"""领域服务：提货队列、两次过磅、装料控制、补装、出场核验、结算复核。

所有重量事件追加写入 weight_events；异常写 incidents；人工改动保留原值。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from . import adapters
from .adapters import (
    ScaleError,
    image_capture,
    payment_gw,
    scale,
    set_db_path,
    silo_gw,
)
from .config import (
    DEFAULT_MOISTURE_PCT,
    ROLE_DISPATCHER,
    ROLE_FINANCE,
    ROLE_FINANCE_MGR,
    ROLE_OPERATOR,
    ROLE_SUPERVISOR,
    TARE_TOLERANCE,
)
from .db import get_conn, transaction

EPS = 0.005  # 吨，比较容差


class DomainError(RuntimeError):
    """业务规则冲突（携带 code 便于界面区分）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---- 仍在占用料仓库存的轮次状态 ----
LOCK_STATES = ("loading", "loaded", "disputed", "extra_pending")
OPEN_BLOCK_CODES = (
    "overload",
    "over_order_after_topup",
    "vehicle_mismatch",
    "silo_mismatch",
    "tare_abnormal",
    "customer_reject",
)


# ---------------------------------------------------------------- 基础查询

def _conn() -> sqlite3.Connection:
    return get_conn(adapters._DB_PATH)


def _get(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise DomainError("not_found", "记录不存在")
    return row


def _incident(conn, code, detail, order_id=None, plate=None, round_id=None,
              status="open"):
    cur = conn.execute(
        "INSERT INTO incidents(order_id, plate, round_id, code, detail, status)"
        " VALUES(?,?,?,?,?,?)",
        (order_id, plate, round_id, code, detail, status),
    )
    return cur.lastrowid


def _event(conn, *, order_id, plate, seq, kind, by, **kw):
    conn.execute(
        "INSERT INTO weight_events(order_id, plate, seq, kind, scale_reading, tare,"
        " gross, moisture_pct, net_raw, net, silo_code, lock_qty, note, created_by)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            order_id, plate, seq, kind,
            kw.get("scale_reading"), kw.get("tare"), kw.get("gross"),
            kw.get("moisture_pct"), kw.get("net_raw"), kw.get("net"),
            kw.get("silo_code"), kw.get("lock_qty", 0.0), kw.get("note", ""), by,
        ),
    )


def active_service_for_plate(conn, plate: str):
    return conn.execute(
        "SELECT * FROM vehicle_service WHERE plate=? AND status='active'", (plate,)
    ).fetchone()


def active_service_for_order(conn, order_id: int):
    return conn.execute(
        "SELECT * FROM vehicle_service WHERE order_id=? AND status='active'",
        (order_id,),
    ).fetchone()


# ---------------------------------------------------------------- 订单与付款

def create_order(*, customer_code, product_code, qty, slot_start, slot_end,
                 expected_plate, expected_axle, by="dispatcher"):
    conn = _conn()
    with transaction(conn):
        cust = conn.execute("SELECT 1 FROM customers WHERE code=?", (customer_code,)).fetchone()
        prod = conn.execute("SELECT * FROM products WHERE code=?", (product_code,)).fetchone()
        if cust is None:
            raise DomainError("bad_customer", f"客户代号 {customer_code} 不存在")
        if prod is None:
            raise DomainError("bad_product", f"砂浆型号 {product_code} 不存在")
        if qty <= 0:
            raise DomainError("bad_qty", "订购量必须大于 0")
        no = f"SO{datetime.now():%Y%m%d%H%M%S%f}{customer_code}"
        cur = conn.execute(
            "INSERT INTO orders(order_no, customer_code, product_code, price_ver,"
            " unit_price, qty_ordered, amount_paid, status, slot_start, slot_end,"
            " expected_plate, expected_axle)"
            " VALUES(?,?,?,?,?,?,?, 'pending_payment',?,?,?,?)",
            (no, customer_code, product_code, prod["price_ver"], prod["unit_price"],
             qty, prod["unit_price"] * qty, slot_start, slot_end,
             expected_plate, expected_axle),
        )
        return cur.lastrowid


def confirm_payment(order_id: int, by="dispatcher"):
    """付款确认（模拟回执）后进入提货队列。"""
    conn = _conn()
    with transaction(conn):
        o = _get(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
        if o["status"] != "pending_payment":
            raise DomainError("bad_state", f"订单 {o['order_no']} 已确认或已关闭")
        receipt = payment_gw.confirm(o["order_no"], o["amount_paid"])
        conn.execute(
            "UPDATE orders SET status='queued' WHERE id=?", (order_id,)
        )
        return receipt


def call_order(order_id: int, by="dispatcher"):
    """呼号：开始计时，超过宽限未入场由后台任务判过号。"""
    conn = _conn()
    with transaction(conn):
        o = _get(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
        if o["status"] != "queued":
            raise DomainError("bad_state", "仅排队中订单可呼号")
        conn.execute(
            "UPDATE orders SET called_at=datetime('now','localtime'), no_show=0 WHERE id=?",
            (order_id,),
        )


def sweep_no_shows(grace_seconds: int) -> list[str]:
    """进程内定时任务：呼号超宽限未入场 → 过号，释放预约重新排队。"""
    released = []
    conn = _conn()
    with transaction(conn):
        rows = conn.execute(
            "SELECT * FROM orders WHERE status='queued' AND called_at IS NOT NULL"
            " AND datetime(called_at) <= datetime('now','localtime', ?)",
            (f"-{int(grace_seconds)} seconds",),
        ).fetchall()
        for o in rows:
            if active_service_for_order(conn, o["id"]):
                continue
            conn.execute(
                "UPDATE orders SET no_show=no_show+1, called_at=NULL,"
                " requeued_at=datetime('now','localtime') WHERE id=?",
                (o["id"],),
            )
            _incident(conn, "no_show",
                      f"订单 {o['order_no']} 呼号 {grace_seconds}s 未入场，预约释放并重排",
                      order_id=o["id"], status="resolved", )
            released.append(o["order_no"])
    return released


# ---------------------------------------------------------------- 车辆入场与皮重

def enter_and_tare(*, order_id: int, plate: str, axle_type: str, by: str,
                   tare_override: float | None = None) -> int:
    """车辆入场首次过磅：记录皮重并锁定对应订单。返回 round id。"""
    conn = _conn()
    with transaction(conn):
        o = _get(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
        if o["status"] not in ("queued", "serving"):
            raise DomainError("bad_state", "订单不在可提货状态")

        # 一辆车同一时刻只能服务一笔订单
        svc = active_service_for_plate(conn, plate)
        if svc and svc["order_id"] != order_id:
            other = _get(conn, "SELECT order_no FROM orders WHERE id=?",
                         (svc["order_id"],))
            raise DomainError("vehicle_busy",
                             f"车辆 {plate} 正在服务订单 {other['order_no']}，不能重复登记")
        if o["status"] == "queued":
            if active_service_for_order(conn, order_id):
                raise DomainError("order_busy", "该订单已有车辆在场服务")
            # 车型不符留痕并拒绝登记
            if o["expected_axle"] and axle_type != o["expected_axle"]:
                _incident(conn, "vehicle_mismatch",
                          f"预约车型 {o['expected_axle']}，到场车型 {axle_type}，车辆 {plate}",
                          order_id=order_id, plate=plate)
                conn.commit()
                raise DomainError("vehicle_mismatch",
                                  f"车型不符：预约 {o['expected_axle']}，到场 {axle_type}")
            if o["expected_plate"] and plate != o["expected_plate"]:
                _incident(conn, "vehicle_mismatch",
                          f"预约车牌 {o['expected_plate']}，到场车牌 {plate}",
                          order_id=order_id, plate=plate)
                conn.commit()
                raise DomainError("vehicle_mismatch",
                                  f"车牌不符：预约 {o['expected_plate']}，到场 {plate}")
            conn.execute(
                "INSERT INTO vehicle_service(plate, order_id, status) VALUES(?,?, 'active')",
                (plate, order_id),
            )
            conn.execute("UPDATE orders SET status='serving', called_at=NULL WHERE id=?",
                         (order_id,))
            img = image_capture.capture("vehicle", plate, f"o{order_id}")
            conn.execute(
                "INSERT INTO images(order_id, plate, kind, filename) VALUES(?,?, 'vehicle',?)",
                (order_id, plate, img),
            )
        else:
            # 已在服务中：车牌必须与在场登记一致
            cur_svc = active_service_for_order(conn, order_id)
            if not cur_svc or cur_svc["plate"] != plate:
                raise DomainError("vehicle_mismatch",
                                  "入场车辆与该订单锁定的在场车辆不一致")

        veh = conn.execute("SELECT * FROM vehicles WHERE plate=?", (plate,)).fetchone()
        if veh is None:
            raise DomainError("no_vehicle", f"车辆 {plate} 无备案档案")

        # 已有轮次（补装场景）则沿用上一轮皮重基线
        prev = conn.execute(
            "SELECT * FROM load_rounds WHERE order_id=? ORDER BY seq DESC LIMIT 1",
            (order_id,),
        ).fetchone()
        if prev is not None:
            if prev["gate_released"]:
                raise DomainError("released", "车辆已放行出场，不能重新开装")
            if prev["status"] != "signed":
                raise DomainError("bad_state",
                                  f"第 {prev['seq']} 轮状态为 {prev['status']}，"
                                  "需先签收才能开新一轮装料")
        seq = (prev["seq"] + 1) if prev else 1

        reading = tare_override if tare_override is not None else scale.read(plate)
        # 皮重异常：相对备案皮重偏离超阈值
        deviation = abs(reading - veh["rated_tare"]) / veh["rated_tare"]
        tare_abnormal = deviation > TARE_TOLERANCE
        cur = conn.execute(
            "INSERT INTO load_rounds(order_id, plate, seq, tare, moisture_pct,"
            " status) VALUES(?,?,?,?,?, 'tared')",
            (order_id, plate, seq, reading, DEFAULT_MOISTURE_PCT),
        )
        rid = cur.lastrowid
        _event(conn, order_id=order_id, plate=plate, seq=seq, kind="tare", by=by,
               scale_reading=reading, tare=reading,
               note=f"备案皮重 {veh['rated_tare']}，偏差 {deviation:.1%}")
        if tare_abnormal:
            _incident(conn, "tare_abnormal",
                      f"皮重 {reading}t 与备案 {veh['rated_tare']}t 偏差 {deviation:.1%}"
                      f"（阈值 {TARE_TOLERANCE:.0%}）",
                      order_id=order_id, plate=plate, round_id=rid)
        return rid


# ---------------------------------------------------------------- 装料与锁定

def begin_load(round_id: int, silo_code: str, req_qty: float, by=ROLE_OPERATOR) -> dict:
    """选择料仓并发料：校验型号、按锁定量分配库存（并发安全）。"""
    conn = _conn()
    with transaction(conn):
        r = _get(conn, "SELECT * FROM load_rounds WHERE id=?", (round_id,))
        o = _get(conn, "SELECT * FROM orders WHERE id=?", (r["order_id"],))
        if r["gate_released"]:
            raise DomainError("released", "已放行车辆不能重开装料")
        if r["status"] not in ("tared", "extra_pending"):
            raise DomainError("bad_state", f"轮次当前状态 {r['status']}，不能开装")
        silo = conn.execute("SELECT * FROM silos WHERE code=?", (silo_code,)).fetchone()
        if silo is None:
            raise DomainError("no_silo", f"料仓 {silo_code} 不存在")
        # 料仓错选
        if silo["product_code"] != o["product_code"]:
            _incident(conn, "silo_mismatch",
                      f"订单型号 {o['product_code']}，料仓 {silo_code} 存放 "
                      f"{silo['product_code']}", order_id=o["id"], plate=r["plate"],
                      round_id=r["id"])
            conn.commit()
            raise DomainError("silo_mismatch",
                              f"料仓错选：{silo_code} 不是 {o['product_code']}")
        if req_qty <= 0:
            raise DomainError("bad_qty", "装料申请量必须大于 0")

        # 订单余量
        settled = conn.execute(
            "SELECT COALESCE(SUM(net),0) s FROM load_rounds WHERE order_id=?"
            " AND status='signed'", (o["id"],)
        ).fetchone()["s"]
        remaining = o["qty_ordered"] - settled

        # 授予即从料仓实物库存原子扣减；并发争抢最后一段时，
        # 条件更新保证后到者按扣减后的余量重新分配
        def _grant(stock_now: float) -> float:
            return round(min(req_qty, remaining, max(stock_now, 0.0)), 3)

        grant = _grant(silo["stock"])
        if grant <= EPS:
            raise DomainError("no_stock",
                              f"料仓 {silo_code} 空仓或订单余量不足（库存 "
                              f"{silo['stock']:.2f}t，订单余量 {remaining:.2f}t）")
        if not silo_gw.try_deduct(conn, silo_code, grant):
            fresh = conn.execute("SELECT stock FROM silos WHERE code=?",
                                 (silo_code,)).fetchone()["stock"]
            grant = _grant(fresh)
            if grant <= EPS or not silo_gw.try_deduct(conn, silo_code, grant):
                raise DomainError("no_stock",
                                  f"料仓 {silo_code} 库存被并发占用，分配失败")

        conn.execute(
            "UPDATE load_rounds SET silo_code=?, lock_qty=?, dispensed=?,"
            " status='loading' WHERE id=?",
            (silo_code, grant, grant, round_id),
        )
        _event(conn, order_id=o["id"], plate=r["plate"], seq=r["seq"], kind="load",
               by=by, silo_code=silo_code, lock_qty=grant,
               note=f"申请 {req_qty}t，锁定/实发 {grant}t，装前库存 {silo['stock']:.2f}t")
        return {"grant": grant, "dispensed": grant, "available": grant}


def record_gross(round_id: int, by=ROLE_OPERATOR, gross_override: float | None = None,
                 moisture_pct: float | None = None) -> dict:
    """装料后二次过磅：毛重 - 皮重（补装轮次减上轮毛重），含水修正得出场净重。"""
    conn = _conn()
    try:
        r = _get(conn, "SELECT * FROM load_rounds WHERE id=?", (round_id,))
        o = _get(conn, "SELECT * FROM orders WHERE id=?", (r["order_id"],))
        if r["gate_released"]:
            raise DomainError("released", "车辆已放行")
        if r["status"] != "loading":
            raise DomainError("bad_state", "尚未开装或已过磅")
        try:
            if gross_override is not None:
                gross = gross_override
            elif r["seq"] > 1:
                prev = conn.execute(
                    "SELECT tare, gross FROM load_rounds WHERE order_id=? AND seq=?",
                    (r["order_id"], r["seq"] - 1),
                ).fetchone()
                # 累计装载 = 上轮毛重 - 上轮皮重；本轮毛重 = 本轮皮重 + 累计 + 本轮实发
                total_load = (prev["gross"] - prev["tare"]) + r["dispensed"]
                gross = scale.read_gross(r["plate"], total_load)
            else:
                gross = scale.read_gross(r["plate"], r["dispensed"])
        except ScaleError as exc:
            # 地磅中断留痕，轮次保持 loading 可稍后重试
            _incident(conn, "scale_outage", f"装料后过磅失败：{exc}",
                      order_id=o["id"], plate=r["plate"], round_id=r["id"])
            conn.commit()
            raise DomainError("scale_outage", str(exc))

        prev_gross = r["tare"]
        if r["seq"] > 1:
            prev = conn.execute(
                "SELECT gross FROM load_rounds WHERE order_id=? AND seq=?",
                (r["order_id"], r["seq"] - 1),
            ).fetchone()
            prev_gross = prev["gross"] if prev else r["tare"]
        net_raw = round(gross - prev_gross, 3)
        m = DEFAULT_MOISTURE_PCT if moisture_pct is None else moisture_pct
        net = round(net_raw * (1 - m / 100.0), 3)

        conn.execute(
            "UPDATE load_rounds SET gross=?, moisture_pct=?, net_raw=?, net=?,"
            " status='loaded' WHERE id=?",
            (gross, m, net_raw, net, round_id),
        )
        _event(conn, order_id=o["id"], plate=r["plate"], seq=r["seq"], kind="load",
               by=by, scale_reading=gross, tare=r["tare"], gross=gross,
               moisture_pct=m, net_raw=net_raw, net=net,
               silo_code=r["silo_code"], lock_qty=r["lock_qty"],
               note="二次过磅，含水修正后出场净重")

        # 装料超量：超过车辆额定载质量
        veh = _get(conn, "SELECT * FROM vehicles WHERE plate=?", (r["plate"],))
        if net_raw > veh["capacity"] + EPS:
            _incident(conn, "overload",
                      f"装料净重 {net_raw}t 超过车型额定 {veh['capacity']}t",
                      order_id=o["id"], plate=r["plate"], round_id=round_id)
        conn.commit()
        return {"gross": gross, "net_raw": net_raw, "net": net, "moisture_pct": m}
    finally:
        conn.close()


# ---------------------------------------------------------------- 含水争议与人工改重

def dispute_moisture(round_id: int, claimed_pct: float, by="customer"):
    """客户对含水修正有异议：留痕并发起改重申请，主管复核后生效。"""
    conn = _conn()
    with transaction(conn):
        r = _get(conn, "SELECT * FROM load_rounds WHERE id=?", (round_id,))
        if r["status"] not in ("loaded", "disputed"):
            raise DomainError("bad_state", "仅过磅后的轮次可提出含水争议")
        conn.execute("UPDATE load_rounds SET status='disputed' WHERE id=?", (round_id,))
        _incident(conn, "moisture_dispute",
                  f"客户主张含水率 {claimed_pct}%，系统取值 {r['moisture_pct']}%",
                  order_id=r["order_id"], plate=r["plate"], round_id=round_id)
        cur = conn.execute(
            "INSERT INTO weight_revisions(round_id, field, old_value, new_value,"
            " reason, requested_by) VALUES(?,?,?,?,?,?)",
            (round_id, "moisture_pct", r["moisture_pct"], claimed_pct,
             f"含水争议：客户主张 {claimed_pct}%", by),
        )
        return cur.lastrowid


def request_revision(round_id: int, field: str, new_value: float, reason: str,
                     actor: str, role: str) -> int:
    """操作员发起人工改重。"""
    if role != ROLE_OPERATOR:
        raise DomainError("forbidden", "仅磅房操作员可发起人工改重")
    if field not in ("tare", "gross", "moisture_pct"):
        raise DomainError("bad_field", "只允许修订 tare/gross/moisture_pct")
    conn = _conn()
    with transaction(conn):
        r = _get(conn, "SELECT * FROM load_rounds WHERE id=?", (round_id,))
        if r["gate_released"]:
            raise DomainError("released", "已放行轮次不得改重")
        old_value = r[field]
        cur = conn.execute(
            "INSERT INTO weight_revisions(round_id, field, old_value, new_value,"
            " reason, requested_by) VALUES(?,?,?,?,?,?)",
            (round_id, field, old_value, new_value, reason, actor),
        )
        return cur.lastrowid


def review_revision(revision_id: int, approve: bool, actor: str, role: str,
                    note=""):
    """主管复核（必须与发起人不同人）。通过后重算净重并留审计事件。"""
    if role != ROLE_SUPERVISOR:
        raise DomainError("forbidden", "仅当班主管可复核人工改重")
    conn = _conn()
    with transaction(conn):
        rev = _get(conn, "SELECT * FROM weight_revisions WHERE id=?", (revision_id,))
        if rev["status"] != "pending":
            raise DomainError("bad_state", "该改重申请已处理")
        if rev["requested_by"] == actor:
            raise DomainError("segregation", "复核人与发起人不能为同一人")
        if not approve:
            conn.execute(
                "UPDATE weight_revisions SET status='rejected', reviewed_by=?,"
                " reviewed_at=datetime('now','localtime'), review_note=? WHERE id=?",
                (actor, note, revision_id),
            )
            return
        r = _get(conn, "SELECT * FROM load_rounds WHERE id=?", (rev["round_id"],))
        new = dict(tare=r["tare"], gross=r["gross"], moisture_pct=r["moisture_pct"])
        new[rev["field"]] = rev["new_value"]
        prev_gross = new["tare"]
        if r["seq"] > 1:
            prev = conn.execute(
                "SELECT gross FROM load_rounds WHERE order_id=? AND seq=?",
                (r["order_id"], r["seq"] - 1),
            ).fetchone()
            if prev:
                prev_gross = prev["gross"]
        net_raw = round((new["gross"] or 0.0) - prev_gross, 3) if new["gross"] else None
        net = round(net_raw * (1 - new["moisture_pct"] / 100.0), 3) if net_raw is not None else None
        conn.execute(
            "UPDATE load_rounds SET tare=?, gross=?, moisture_pct=?, net_raw=?, net=?,"
            " status='loaded' WHERE id=?",
            (new["tare"], new["gross"], new["moisture_pct"], net_raw, net, r["id"]),
        )
        conn.execute(
            "UPDATE weight_revisions SET status='approved', reviewed_by=?,"
            " reviewed_at=datetime('now','localtime'), review_note=? WHERE id=?",
            (actor, note, revision_id),
        )
        _event(conn, order_id=r["order_id"], plate=r["plate"], seq=r["seq"],
               kind="adjust", by=actor, tare=new["tare"], gross=new["gross"],
               moisture_pct=new["moisture_pct"], net_raw=net_raw, net=net,
               note=f"人工改重 {rev['field']}: {rev['old_value']} → {rev['new_value']}"
                    f"（发起人 {rev['requested_by']}，复核 {actor}：{note}）")
        # 复核通过即视为争议解决
        conn.execute(
            "UPDATE incidents SET status='resolved', resolved_at=datetime('now','localtime')"
            " WHERE round_id=? AND code='moisture_dispute' AND status='open'",
            (r["id"],),
        )


# ---------------------------------------------------------------- 客户签收/拒签

def _open_blocking_incidents(conn, order_id):
    marks = ",".join("?" * len(OPEN_BLOCK_CODES))
    return conn.execute(
        f"SELECT code, detail FROM incidents WHERE order_id=? AND status='open'"
        f" AND code IN ({marks})",
        (order_id, *OPEN_BLOCK_CODES),
    ).fetchall()


def customer_sign(round_id: int, actor="customer"):
    conn = _conn()
    with transaction(conn):
        r = _get(conn, "SELECT * FROM load_rounds WHERE id=?", (round_id,))
        o = _get(conn, "SELECT * FROM orders WHERE id=?", (r["order_id"],))
        if r["gate_released"]:
            raise DomainError("released", "车辆已放行")
        if r["status"] != "loaded":
            raise DomainError("bad_state",
                              f"当前轮次状态为 {r['status']}，需处理争议/拒签后再签收")
        if r["net"] is None:
            raise DomainError("no_weight", "缺少净重数据")
        blocked = _open_blocking_incidents(conn, o["id"])
        if blocked:
            raise DomainError("incident_open",
                              "存在未处理异常：" + "；".join(b["code"] for b in blocked))

        signed_total = conn.execute(
            "SELECT COALESCE(SUM(net),0) s FROM load_rounds WHERE order_id=?"
            " AND status='signed'", (o["id"],)
        ).fetchone()["s"]
        # 补装后超单：累计签收量不得超过订单量
        if signed_total + r["net"] > o["qty_ordered"] + EPS:
            over = signed_total + r["net"] - o["qty_ordered"]
            _incident(conn, "over_order_after_topup",
                      f"补装后累计 {signed_total + r['net']:.3f}t 超出订单 "
                      f"{o['qty_ordered']}t，超 {over:.3f}t",
                      order_id=o["id"], plate=r["plate"], round_id=round_id)
            conn.commit()
            raise DomainError("over_order_after_topup",
                              f"补装后超单 {over:.3f}t，已留痕，禁止签收")

        conn.execute("UPDATE load_rounds SET status='signed' WHERE id=?", (round_id,))
        conn.execute(
            "UPDATE orders SET qty_loaded=qty_loaded+? WHERE id=?",
            (r["net"], o["id"]),
        )
        _event(conn, order_id=o["id"], plate=r["plate"], seq=r["seq"], kind="load",
               by=actor, net=r["net"], note="客户确认重量并签收")


def customer_reject(round_id: int, reason: str, actor="customer"):
    """客户拒签留痕，闸门冻结等待处理。"""
    conn = _conn()
    with transaction(conn):
        r = _get(conn, "SELECT * FROM load_rounds WHERE id=?", (round_id,))
        if r["gate_released"]:
            raise DomainError("released", "车辆已放行")
        conn.execute("UPDATE load_rounds SET status='rejected' WHERE id=?", (round_id,))
        _incident(conn, "customer_reject", f"客户拒签：{reason}",
                  order_id=r["order_id"], plate=r["plate"], round_id=round_id)


def resolve_incident(incident_id: int, resolution: str, actor: str, role: str):
    if role != ROLE_SUPERVISOR:
        raise DomainError("forbidden", "仅当班主管可关闭异常")
    conn = _conn()
    with transaction(conn):
        inc = _get(conn, "SELECT * FROM incidents WHERE id=?", (incident_id,))
        conn.execute(
            "UPDATE incidents SET status='resolved', resolved_at=datetime('now','localtime')"
            " WHERE id=?",
            (incident_id,),
        )
        # 拒签/争议关闭后轮次回到待签收，客户可重新确认
        if inc["round_id"] and inc["code"] in ("customer_reject", "moisture_dispute"):
            conn.execute(
                "UPDATE load_rounds SET status='loaded' WHERE id=? AND status IN"
                " ('rejected','disputed')",
                (inc["round_id"],),
            )
        # resolution 文本记录在事件流
        if inc["order_id"]:
            _event(conn, order_id=inc["order_id"], plate=inc["plate"], seq=0,
                   kind="adjust", by=actor,
                   note=f"异常 {inc['code']} 关闭：{resolution}")


# ---------------------------------------------------------------- 补装

def request_topup(order_id: int, by=ROLE_OPERATOR):
    """签收后在订单余量内补装：开新一轮（车辆不离场）。"""
    conn = _conn()
    with transaction(conn):
        o = _get(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
        svc = active_service_for_order(conn, order_id)
        if not svc:
            raise DomainError("no_service", "车辆未在场，不能补装")
        last = conn.execute(
            "SELECT * FROM load_rounds WHERE order_id=? ORDER BY seq DESC LIMIT 1",
            (order_id,),
        ).fetchone()
        if last is None or last["status"] != "signed":
            raise DomainError("bad_state", "需先签收上一轮才能申请补装")
        if last["gate_released"]:
            raise DomainError("released", "已放行车辆不能补装")
        if o["qty_loaded"] + EPS >= o["qty_ordered"]:
            raise DomainError("no_remaining", "订单余量不足，无法补装")
        rid = enter_and_tare(order_id=order_id, plate=svc["plate"],
                             axle_type=_get(conn, "SELECT axle_type FROM vehicles WHERE plate=?",
                                            (svc["plate"],))["axle_type"],
                             by=by)
        return rid


# ---------------------------------------------------------------- 出场与结算

def release_gate(order_id: int, actor=ROLE_DISPATCHER):
    """客户确认后放行出场并生成结算快照；放行不可逆。"""
    conn = _conn()
    with transaction(conn):
        o = _get(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
        svc = active_service_for_order(conn, order_id)
        if not svc:
            raise DomainError("no_service", "该订单无在场车辆（可能已放行）")
        rounds = conn.execute(
            "SELECT * FROM load_rounds WHERE order_id=? ORDER BY seq", (order_id,)
        ).fetchall()
        if not rounds:
            raise DomainError("no_round", "无过磅记录")
        for r in rounds:
            if r["status"] != "signed":
                raise DomainError("unsigned", f"第 {r['seq']} 轮尚未签收，不能放行")
        blocked = conn.execute(
            "SELECT code FROM incidents WHERE order_id=? AND status='open'", (order_id,)
        ).fetchall()
        if blocked:
            raise DomainError("incident_open",
                              "存在未关闭异常：" + ",".join(b["code"] for b in blocked))
        existing = conn.execute(
            "SELECT 1 FROM settlements WHERE order_id=?", (order_id,)
        ).fetchone()
        if existing:
            raise DomainError("already_settled", "结算快照已存在")

        total_net = round(sum(r["net"] for r in rounds), 3)
        amount = round(total_net * o["unit_price"], 2)
        snapshot = {
            "order_no": o["order_no"],
            "customer": o["customer_code"],
            "product": o["product_code"],
            "price_ver": o["price_ver"],
            "unit_price": o["unit_price"],
            "plate": svc["plate"],
            "rounds": [
                {"seq": r["seq"], "tare": r["tare"], "gross": r["gross"],
                 "moisture_pct": r["moisture_pct"], "net_raw": r["net_raw"],
                 "net": r["net"], "silo": r["silo_code"]}
                for r in rounds
            ],
            "total_net": total_net,
            "amount": amount,
            "paid": o["amount_paid"],
            "released_by": actor,
            "released_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        conn.execute(
            "INSERT INTO settlements(order_id, plate, total_net, unit_price,"
            " price_ver, amount, paid, snapshot, created_by) VALUES(?,?,?,?,?,?,?,?,?)",
            (order_id, svc["plate"], total_net, o["unit_price"], o["price_ver"],
             amount, o["amount_paid"], json.dumps(snapshot, ensure_ascii=False), actor),
        )
        bill = image_capture.capture("weighbill", o["order_no"], "settle")
        conn.execute(
            "INSERT INTO images(order_id, plate, kind, filename) VALUES(?,?, 'weighbill',?)",
            (order_id, svc["plate"], bill),
        )
        conn.execute(
            "UPDATE load_rounds SET gate_released=1 WHERE order_id=?", (order_id,)
        )
        conn.execute(
            "UPDATE vehicle_service SET status='released',"
            " released_at=datetime('now','localtime') WHERE id=?",
            (svc["id"],),
        )
        conn.execute(
            "UPDATE orders SET status='completed', qty_locked=0 WHERE id=?",
            (order_id,),
        )
        _event(conn, order_id=order_id, plate=svc["plate"], seq=0, kind="load",
               by=actor, net=total_net, note=f"放行出场，结算快照生成，合计净重 {total_net}t")
        return snapshot


# ---------------------------------------------------------------- 退款（双角色 + 幂等）

def request_refund(order_id: int, reason: str, actor: str, role: str,
                   qty: float | None = None) -> dict:
    """财务发起退款。同一订单重复发起返回原单（幂等）。"""
    if role != ROLE_FINANCE:
        raise DomainError("forbidden", "仅财务专员可发起退款")
    conn = _conn()
    with transaction(conn):
        o = _get(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
        idem = f"REFUND-{order_id}"
        existing = conn.execute(
            "SELECT * FROM refunds WHERE idem_key=?", (idem,)
        ).fetchone()
        if existing:
            return {"idempotent": True, "refund_id": existing["id"],
                    "status": existing["status"]}
        stl = conn.execute(
            "SELECT * FROM settlements WHERE order_id=?", (order_id,)
        ).fetchone()
        if stl is None:
            raise DomainError("not_settled", "未结算订单不能退款")
        refund_qty = round(o["qty_ordered"] - stl["total_net"], 3) if qty is None else qty
        if refund_qty < -EPS:
            raise DomainError("bad_refund", "签收量超过订单量，需补款而非退款")
        amount = round(max(refund_qty, 0.0) * o["unit_price"], 2)
        cur = conn.execute(
            "INSERT INTO refunds(order_id, idem_key, qty, amount, reason,"
            " requested_by, status) VALUES(?,?,?,?,?,?, 'pending')",
            (order_id, idem, refund_qty, amount, reason, actor),
        )
        return {"idempotent": False, "refund_id": cur.lastrowid, "amount": amount}


def review_refund(refund_id: int, approve: bool, actor: str, role: str, note=""):
    """财务主管复核（须与发起人不同人），通过即付款（虚构回执）。"""
    if role != ROLE_FINANCE_MGR:
        raise DomainError("forbidden", "仅财务主管可复核退款")
    conn = _conn()
    with transaction(conn):
        rf = _get(conn, "SELECT * FROM refunds WHERE id=?", (refund_id,))
        if rf["status"] not in ("pending",):
            raise DomainError("bad_state", "退款单已处理")
        if rf["requested_by"] == actor:
            raise DomainError("segregation", "复核人与发起人不能为同一人")
        if not approve:
            conn.execute(
                "UPDATE refunds SET status='rejected', reviewed_by=?,"
                " reviewed_at=datetime('now','localtime'), review_note=? WHERE id=?",
                (actor, note, refund_id),
            )
            return
        conn.execute(
            "UPDATE refunds SET status='paid', reviewed_by=?,"
            " reviewed_at=datetime('now','localtime'), review_note=? WHERE id=?",
            (actor, note, refund_id),
        )
        conn.execute(
            "UPDATE settlements SET refund_qty=?, refund_amt=? WHERE order_id=?",
            (rf["qty"], rf["amount"], rf["order_id"]),
        )
        _event(conn, order_id=rf["order_id"], plate="", seq=0, kind="refund",
               by=actor, net=rf["qty"],
               note=f"退款已付 {rf['amount']} 元（{rf['qty']}t，发起 {rf['requested_by']}，"
                    f"复核 {actor}）")


# ---------------------------------------------------------------- 看板查询

def queue_board():
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT o.*, c.name AS customer_name, p.name AS product_name,
                   vs.plate AS active_plate,
                   (SELECT status FROM load_rounds lr WHERE lr.order_id=o.id
                    ORDER BY lr.seq DESC LIMIT 1) AS round_status,
                   (SELECT MAX(seq) FROM load_rounds lr WHERE lr.order_id=o.id) AS rounds
            FROM orders o
            JOIN customers c ON c.code=o.customer_code
            JOIN products p ON p.code=o.product_code
            LEFT JOIN vehicle_service vs ON vs.order_id=o.id AND vs.status='active'
            WHERE o.status IN ('queued','serving')
            ORDER BY (o.called_at IS NULL), o.called_at, o.slot_start, o.id
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def order_detail(order_id: int):
    conn = _conn()
    try:
        o = conn.execute(
            "SELECT o.*, c.name customer_name, p.name product_name FROM orders o"
            " JOIN customers c ON c.code=o.customer_code"
            " JOIN products p ON p.code=o.product_code WHERE o.id=?",
            (order_id,),
        ).fetchone()
        if not o:
            return None
        rounds = conn.execute(
            "SELECT * FROM load_rounds WHERE order_id=? ORDER BY seq", (order_id,)
        ).fetchall()
        events = conn.execute(
            "SELECT * FROM weight_events WHERE order_id=? ORDER BY id", (order_id,)
        ).fetchall()
        incidents = conn.execute(
            "SELECT * FROM incidents WHERE order_id=? ORDER BY id", (order_id,)
        ).fetchall()
        stl = conn.execute("SELECT * FROM settlements WHERE order_id=?",
                           (order_id,)).fetchone()
        refund = conn.execute("SELECT * FROM refunds WHERE order_id=?",
                              (order_id,)).fetchone()
        revisions = conn.execute(
            "SELECT wr.* FROM weight_revisions wr JOIN load_rounds lr ON lr.id=wr.round_id"
            " WHERE lr.order_id=? ORDER BY wr.id", (order_id,)
        ).fetchall()
        images = conn.execute(
            "SELECT * FROM images WHERE order_id=? ORDER BY id", (order_id,)
        ).fetchall()
        return dict(o=dict(o), rounds=[dict(r) for r in rounds],
                    events=[dict(e) for e in events],
                    incidents=[dict(i) for i in incidents],
                    settlement=dict(stl) if stl else None,
                    refund=dict(refund) if refund else None,
                    revisions=[dict(x) for x in revisions],
                    images=[dict(x) for x in images])
    finally:
        conn.close()
