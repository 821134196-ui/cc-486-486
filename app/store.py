"""领域层：订单状态机、两次过磅、库存锁定、补装、结算、复核与退款。

约定：订单数量 qty_kg 为“干砂浆公斤”；地磅读数为湿料实际重量，
按含水百分比折算干量：dry = wet * (1 - moisture_pct/100)。
库存与锁定均以干量公斤记账。

状态流转：
  PAID 已付款 → QUEUED 入场排队 → TARE 首磅(皮重+锁库存)
  → LOADING 装料中 → LOADED 待复磅 → WEIGHED 复磅完成
  → CONFIRMED 客户确认(结算快照) → RELEASED 已放行(终态)
异常终态：REJECTED 拒签 / NO_SHOW 过号 / CANCELLED 取消并退款
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import adapters, images
from .db import get_db

# ---------------------------------------------------------------- 常量

ACTIVE_STATUSES = ("QUEUED", "TARE", "LOADING", "LOADED", "WEIGHED", "CONFIRMED", "REJECTED")
TERMINAL_STATUSES = ("RELEASED", "NO_SHOW", "CANCELLED")

# 各车型合理皮重区间（公斤，虚构）
TARE_BANDS = {
    "牵引罐车": (12000, 18000),
    "三轴罐车": (9000, 13500),
    "两轴罐车": (6000, 9500),
}

NO_SHOW_GRACE_MIN = 15  # 时段开始后 15 分钟未入场即过号

_write_lock = threading.RLock()


class DomainError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AuditedError(DomainError):
    """业务被拒绝、但异常留痕已写入当前事务。

    事务提交（保留审计行）而不是回滚。仅用于在抛错前没有其他业务写操作
    的校验路径；正常守卫（缺单/状态不符）仍用 DomainError 回滚。
    """


# ---------------------------------------------------------------- 工具

def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def slot_dt(slot: str) -> datetime:
    h, m = map(int, slot.split(":"))
    t = datetime.now()
    return t.replace(hour=h, minute=m, second=0, microsecond=0)


@contextmanager
def tx():
    conn = get_db()
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except AuditedError:
            # 异常留痕必须保留：提交本事务（其中只有审计插入）
            conn.execute("COMMIT")
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _row(conn, sql: str, args=()) -> sqlite3.Row | None:
    return conn.execute(sql, args).fetchone()


def dry_equiv(wet_kg: float, moisture_pct: float) -> float:
    return round(wet_kg * (1 - (moisture_pct or 0) / 100.0), 1)


def get_order(code: str) -> sqlite3.Row | None:
    return _row(get_db(), "SELECT * FROM orders WHERE code=?", (code,))


def log_exception(conn, kind: str, detail: str, operator: str,
                  order_code: str | None = None, plate: str | None = None,
                  value_kg: float | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO exceptions(order_code,plate,kind,detail,value_kg,operator,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (order_code, plate, kind, detail, value_kg, operator, now()),
    )
    return cur.lastrowid


def add_event(conn, order_code: str, kind: str, value: float, operator: str,
              prev: float | None = None, moisture: float | None = None,
              note: str = "", reviewer: str | None = None) -> None:
    conn.execute(
        "INSERT INTO weight_events(order_code,kind,value_kg,prev_value_kg,moisture_pct,"
        "note,operator,reviewer,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (order_code, kind, value, prev, moisture, note, operator, reviewer, now()),
    )


def remaining_qty(conn, o: sqlite3.Row) -> float:
    """订单干量余量。"""
    loaded = dry_equiv(o["load_kg"] or 0, o["moisture_pct"] or 0)
    return round(o["qty_kg"] - loaded, 1)


def locked_total_for_silo(conn, silo_id: str, exclude_code: str | None = None) -> int:
    sql = "SELECT COALESCE(SUM(locked_stock_kg),0) s FROM orders WHERE silo_id=? AND status IN ('TARE','LOADING','LOADED','WEIGHED','CONFIRMED')"
    args: list = [silo_id]
    if exclude_code:
        sql += " AND code<>?"
        args.append(exclude_code)
    return int(_row(conn, sql, args)["s"])


# ---------------------------------------------------------------- 订单登记与付款

def create_order(*, customer_code: str, model: str, silo_id: str, qty_kg: int,
                 slot: str, vehicle_type: str, price_version: str,
                 operator: str = "dispatcher") -> str:
    conn = get_db()
    with tx():
        silo = _row(conn, "SELECT * FROM silos WHERE id=?", (silo_id,))
        if silo is None:
            raise DomainError("SILO_MISSING", f"料仓 {silo_id} 不存在")
        if silo["model"] != model:
            raise DomainError("SILO_MODEL", f"料仓 {silo_id} 存的是 {silo['model']}，与订单型号 {model} 不符")
        price = _row(conn, "SELECT * FROM prices WHERE version=? AND model=?",
                     (price_version, model))
        if price is None:
            raise DomainError("PRICE_MISSING", f"价格版本 {price_version} 中没有 {model} 的报价")
        seq_row = _row(conn, "SELECT COALESCE(MAX(slot_seq),0)+1 n FROM orders")
        seq = int(seq_row["n"])
        code = f"SO-{datetime.now():%Y%m%d}-{seq:03d}-{customer_code}"
        amount = round(qty_kg * price["unit_price"], 2)
        # 模拟付款渠道确认 → 进入提货队列
        receipt = adapters.payment.confirm(code, amount)
        conn.execute(
            "INSERT INTO orders(code,customer_code,model,silo_id,qty_kg,unit_price,"
            "price_version,slot,slot_seq,vehicle_type,plate,status,paid_amount,"
            "pay_receipt,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,'PAID',?,?,?,?)",
            (code, customer_code, model, silo_id, qty_kg, price["unit_price"],
             price_version, slot, seq, vehicle_type, None, amount,
             receipt["receipt"], now(), now()),
        )
    return code


def check_in(order_code: str, plate: str, actual_vehicle_type: str,
             operator: str = "dispatcher") -> None:
    """车辆入场：绑定车牌并入队。车型不符留痕并阻断，需主管处理。"""
    conn = get_db()
    with tx():
        o = _row(conn, "SELECT * FROM orders WHERE code=?", (order_code,))
        if o is None:
            raise DomainError("ORDER_MISSING", "订单不存在")
        if o["status"] != "PAID":
            raise DomainError("NOT_PAID", f"订单当前状态 {o['status']}，不能入场")
        # 一辆车同一时刻只能服务一笔订单（占用中的车牌不可再绑）
        clash = _row(
            conn,
            "SELECT code FROM orders WHERE plate=? AND status IN ({})".format(
                ",".join("?" * len(ACTIVE_STATUSES))),
            (plate, *ACTIVE_STATUSES))
        if clash is not None:
            log_exception(conn, "VEHICLE_MISMATCH",
                          f"车牌 {plate} 已在服务订单 {clash['code']}，车辆唯一性冲突",
                          operator, order_code, plate)
            raise AuditedError("VEHICLE_BUSY", f"车辆 {plate} 正服务订单 {clash['code']}（已留痕）")
        if actual_vehicle_type != o["vehicle_type"]:
            log_exception(conn, "VEHICLE_MISMATCH",
                          f"登记车型 {o['vehicle_type']}，到场车型 {actual_vehicle_type}，车牌 {plate}",
                          operator, order_code, plate)
            raise AuditedError("VEHICLE_MISMATCH",
                               f"车型不符：登记 {o['vehicle_type']} / 到场 {actual_vehicle_type}，已留痕，请主管处理")
        images.save_vehicle(plate, o["model"])
        conn.execute("UPDATE orders SET plate=?,status='QUEUED',updated_at=? WHERE code=?",
                     (plate, now(), order_code))


def override_vehicle_type(order_code: str, actual_vehicle_type: str,
                          supervisor: str, comment: str) -> None:
    """主管对车型不符的裁定：以到场车型为准，之后调度可重新办理入场。"""
    conn = get_db()
    with tx():
        o = _row(conn, "SELECT * FROM orders WHERE code=?", (order_code,))
        if o is None:
            raise DomainError("ORDER_MISSING", "订单不存在")
        prev = o["vehicle_type"]
        conn.execute("UPDATE orders SET vehicle_type=?,updated_at=? WHERE code=?",
                     (actual_vehicle_type, now(), order_code))
        conn.execute("UPDATE exceptions SET resolved=1 WHERE order_code=? AND kind='VEHICLE_MISMATCH' AND resolved=0",
                     (order_code,))
        conn.execute("INSERT INTO reviews(order_code,kind,payload,requested_by,request_role,"
                     "reviewed_by,status,comment,created_at,reviewed_at)"
                     " VALUES(?,?,?,?,?,?,'APPROVED',?,?,?)",
                     (order_code, "VEHICLE_OVERRIDE",
                      json.dumps({"prev": prev, "actual": actual_vehicle_type}),
                      supervisor, "supervisor", supervisor,
                      comment or f"主管核定以到场车型 {actual_vehicle_type} 为准", now(), now()))


def resolve_exception(exc_id: int, supervisor: str, comment: str) -> None:
    conn = get_db()
    with tx():
        e = _row(conn, "SELECT * FROM exceptions WHERE id=?", (exc_id,))
        if e is None:
            raise DomainError("NO_EXCEPTION", "异常记录不存在")
        conn.execute("UPDATE exceptions SET resolved=1 WHERE id=?", (exc_id,))
        conn.execute("INSERT INTO reviews(order_code,kind,payload,requested_by,request_role,"
                     "reviewed_by,status,comment,created_at,reviewed_at)"
                     " VALUES(?,?,?,?,?,?,'APPROVED',?,?,?)",
                     (e["order_code"] or "", "EXCEPTION_RESOLVE",
                      json.dumps({"exc_id": exc_id}), supervisor, "supervisor",
                      supervisor, comment or "异常已核实处理", now(), now()))


# ---------------------------------------------------------------- 首次过磅：皮重 + 锁库存

def weigh_tare(order_code: str, operator: str = "weigher", force: bool = False) -> dict:
    conn = get_db()
    with tx():
        o = _row(conn, "SELECT * FROM orders WHERE code=?", (order_code,))
        if o is None:
            raise DomainError("ORDER_MISSING", "订单不存在")
        if o["status"] not in ("QUEUED",):
            raise DomainError("NOT_QUEUED", f"状态 {o['status']} 下不能记录皮重")
        # 模拟地磅读数（车型决定大致皮重，虚构）
        band = TARE_BANDS.get(o["vehicle_type"], (8000, 12000))
        if force:
            # 主管强制采纳：复用此前留痕的异常读数，不再重新取数
            u = _row(conn, "SELECT * FROM users WHERE username=?", (operator,))
            if u is None or u["role"] != "supervisor":
                raise DomainError("ROLE_CONFLICT", "皮重异常的强制采纳须主管角色")
            prior = _row(conn,
                         "SELECT * FROM exceptions WHERE order_code=? AND kind='TARE_ABNORMAL' "
                         "AND resolved=0 ORDER BY id DESC LIMIT 1", (order_code,))
            if prior is None or prior["value_kg"] is None:
                raise DomainError("NO_TARE_EXC", "没有待处理的皮重异常记录，不能强制采纳")
            reading = float(prior["value_kg"])
            lo, hi = band
        else:
            # 固定基线 + 车牌字符扰动，得到可复现的虚构皮重
            base = (band[0] + band[1]) // 2
            perturb = (sum(ord(c) for c in (o["plate"] or "AAAA")) % 400) - 200
            expected = base + perturb
            try:
                reading = adapters.scale.read(expected)
            except adapters.ScaleOffline:
                log_exception(conn, "SCALE_DOWN", "首次过磅时地磅通讯中断，未取得皮重",
                              operator, order_code, o["plate"])
                raise AuditedError("SCALE_DOWN", "地磅中断，无法记录皮重（已留痕）")
            lo, hi = band
            if not (lo <= reading <= hi):
                log_exception(conn, "TARE_ABNORMAL",
                              f"皮重 {reading:.0f}kg 超出 {o['vehicle_type']} 合理区间 "
                              f"{lo}-{hi}kg，车牌 {o['plate']}（皮重暂存失败）",
                              operator, order_code, o["plate"], value_kg=reading)
                raise AuditedError("TARE_ABNORMAL",
                                   f"皮重 {reading:.0f}kg 异常（合理 {lo}-{hi}kg），已留痕，需主管确认")
        # 锁定订单余量对应库存（并发：最后一段库存按锁定量分配）
        need = int(round(remaining_qty(conn, o)))
        silo = _row(conn, "SELECT * FROM silos WHERE id=?", (o["silo_id"],))
        locked_by_others = locked_total_for_silo(conn, o["silo_id"], exclude_code=order_code)
        available = silo["stock_kg"] - locked_by_others
        if need > available:
            # 库存不足：按可锁量部分锁定并留痕
            granted = max(0, available)
            log_exception(conn, "OVERLOAD",
                          f"库存锁定不足：订单需 {need}kg，料仓 {silo['id']} 可分配仅 {granted}kg"
                          f"（在锁 {locked_by_others}kg / 库存 {silo['stock_kg']}kg）",
                          operator, order_code, o["plate"])
            if granted <= 0:
                raise AuditedError("NO_STOCK",
                                   f"料仓 {silo['id']} 无可分配库存（已锁 {locked_by_others}kg，已留痕）")
        else:
            granted = need
        # 校验通过后再写皮重事件与磅单
        ticket = adapters.scale.ticket_no()
        images.save_slip(ticket, o["plate"], "皮重", reading,
                         extra=f"{o['vehicle_type']} · 合理 {lo}-{hi}kg"
                               + (" · 主管强制采纳" if force else ""))
        add_event(conn, order_code, "TARE", reading, operator,
                  note=f"票号 {ticket}" + ("（主管确认皮重异常后采纳）" if force else ""),
                  reviewer=operator if force else None)
        if force:
            conn.execute("UPDATE exceptions SET resolved=1 WHERE order_code=? AND kind='TARE_ABNORMAL'",
                         (order_code,))
        conn.execute("UPDATE orders SET tare_kg=?,locked_stock_kg=?,status='TARE',updated_at=? "
                     "WHERE code=?", (reading, granted, now(), order_code))
        conn.execute("INSERT INTO stock_movements(silo_id,order_code,change_kg,kind,created_at)"
                     " VALUES(?,?,?,'LOCK',?)", (o["silo_id"], order_code, -granted, now()))
        return {"tare_kg": reading, "ticket": ticket, "locked_kg": granted}


# ---------------------------------------------------------------- 装料

def load_material(order_code: str, silo_id: str, wet_kg: float,
                  moisture_pct: float, operator: str = "loader") -> dict:
    conn = get_db()
    with tx():
        o = _row(conn, "SELECT * FROM orders WHERE code=?", (order_code,))
        if o is None:
            raise DomainError("ORDER_MISSING", "订单不存在")
        if o["status"] == "RELEASED":
            raise DomainError("RELEASED_LOCK", "车辆已放行出场，不能重开装料")
        if o["status"] not in ("TARE", "LOADING", "LOADED", "WEIGHED"):
            raise DomainError("BAD_STATE", f"状态 {o['status']} 下不能装料")
        if silo_id != o["silo_id"]:
            log_exception(conn, "SILO_WRONG",
                          f"装料选择料仓 {silo_id}，订单指定料仓 {o['silo_id']}，"
                          f"型号 {o['model']}，车牌 {o['plate']}",
                          operator, order_code, o["plate"])
            raise AuditedError("SILO_WRONG",
                               f"料仓错选：应选 {o['silo_id']}，实际 {silo_id}，已留痕并阻断装料")
        if wet_kg <= 0:
            raise DomainError("BAD_WEIGHT", "装料重量必须大于 0")
        dry = dry_equiv(wet_kg, moisture_pct)
        remain = remaining_qty(conn, o)
        if dry > remain + 0.5:
            kind = "REFILL_EXCEED" if o["status"] in ("LOADED", "WEIGHED") else "OVERLOAD"
            label = "补装后超单" if kind == "REFILL_EXCEED" else "装料超量"
            log_exception(conn, kind,
                          f"本次折算干量 {dry}kg，订单余量仅 {remain}kg（订单 {o['qty_kg']}kg），"
                          f"车牌 {o['plate']}",
                          operator, order_code, o["plate"])
            raise AuditedError(kind, f"{label}：申请 {dry}kg > 余量 {remain}kg，已留痕，本次装料被阻止")
        if int(round(dry)) > int(o["locked_stock_kg"]):
            # 最后一段库存并发场景：本单锁定量已不足以覆盖本次装料
            log_exception(conn, "OVERLOAD",
                          f"本次折算干量 {dry}kg 超过本单剩余锁定 {o['locked_stock_kg']}kg，"
                          f"库存可能已被并发订单分配，车牌 {o['plate']}",
                          operator, order_code, o["plate"])
            raise AuditedError("LOCK_EXCEEDED",
                               f"本次装料 {dry}kg 超过本单剩余锁定 {o['locked_stock_kg']}kg，"
                               "库存已被并发装料分配，已留痕，请联系调度")
        # 锁定量核销 + 实际扣库存
        new_load = round((o["load_kg"] or 0) + wet_kg, 1)
        new_locked = max(0, int(o["locked_stock_kg"]) - int(round(dry)))
        ev_kind = "REFILL" if o["status"] in ("LOADED", "WEIGHED") else "LOAD"
        if ev_kind == "REFILL":
            conn.execute(
                "UPDATE orders SET load_kg=?,moisture_pct=?,locked_stock_kg=?,"
                "refilled_qty_kg=refilled_qty_kg+?,status='LOADING',gross_kg=NULL,updated_at=? "
                "WHERE code=?",
                (new_load, moisture_pct, new_locked, int(round(dry)), now(), order_code))
        else:
            conn.execute(
                "UPDATE orders SET load_kg=?,moisture_pct=?,locked_stock_kg=?,"
                "status='LOADING',gross_kg=NULL,updated_at=? WHERE code=?",
                (new_load, moisture_pct, new_locked, now(), order_code))
        conn.execute("UPDATE silos SET stock_kg=stock_kg-? WHERE id=?",
                     (int(round(dry)), silo_id))
        conn.execute("INSERT INTO stock_movements(silo_id,order_code,change_kg,kind,created_at)"
                     " VALUES(?,?,?, 'LOAD',?)", (silo_id, order_code, -int(round(dry)), now()))
        add_event(conn, order_code, ev_kind, wet_kg, operator,
                  moisture=moisture_pct,
                  note=f"湿料 {wet_kg}kg，含水 {moisture_pct}%，折干 {dry}kg，订单余量 {remain}kg")
        return {"wet_kg": wet_kg, "dry_kg": dry, "remaining": round(remain - dry, 1)}


def finish_loading(order_code: str, operator: str = "loader") -> None:
    """装料完成，驶往地磅复磅。"""
    conn = get_db()
    with tx():
        o = _row(conn, "SELECT * FROM orders WHERE code=?", (order_code,))
        if o is None or o["status"] not in ("TARE", "LOADING"):
            raise DomainError("BAD_STATE", "当前不可结束装料")
        if not o["load_kg"]:
            raise DomainError("EMPTY_LOAD", "尚未装料")
        conn.execute("UPDATE orders SET status='LOADED',updated_at=? WHERE code=?",
                     (now(), order_code))


# ---------------------------------------------------------------- 二次过磅：毛重与净重

def weigh_gross(order_code: str, moisture_pct: float | None = None,
                disputed: bool = False, operator: str = "weigher") -> dict:
    conn = get_db()
    with tx():
        o = _row(conn, "SELECT * FROM orders WHERE code=?", (order_code,))
        if o is None:
            raise DomainError("ORDER_MISSING", "订单不存在")
        if o["status"] not in ("LOADED", "WEIGHED"):
            raise DomainError("NOT_LOADED", f"状态 {o['status']} 下不能二次过磅")
        if not o["tare_kg"]:
            raise DomainError("NO_TARE", "缺少皮重")
        # 仪表毛重 = 皮重 + 实际湿料（经地磅抖动）
        gross_expected = o["tare_kg"] + (o["load_kg"] or 0)
        try:
            gross = adapters.scale.read(gross_expected)
        except adapters.ScaleOffline:
            log_exception(conn, "SCALE_DOWN", "二次过磅时地磅通讯中断，毛重未取得",
                          operator, order_code, o["plate"])
            raise AuditedError("SCALE_DOWN", "地磅中断，复磅失败（已留痕）")
        m = o["moisture_pct"] or 0 if moisture_pct is None else moisture_pct
        if disputed:
            log_exception(conn, "MOISTURE_DISPUTE",
                          f"客户对含水修正 {m}% 提出异议（车牌 {o['plate']}），暂按司磅值计算，待主管裁定",
                          operator, order_code, o["plate"])
        net = dry_equiv(gross - o["tare_kg"], m)
        ticket = adapters.scale.ticket_no()
        images.save_slip(ticket, o["plate"], "毛重", gross,
                         extra=f"皮重 {o['tare_kg']}kg · 净重 {net}kg · 含水 {m}%")
        add_event(conn, order_code, "GROSS", gross, operator, moisture=m,
                  note=f"票号 {ticket}；净重 {net}kg" + ("；含水争议待裁定" if disputed else ""))
        conn.execute("UPDATE orders SET gross_kg=?,moisture_pct=?,net_kg=?,status='WEIGHED',"
                     "updated_at=? WHERE code=?",
                     (gross, m, net, now(), order_code))
        return {"gross_kg": gross, "net_kg": net, "moisture_pct": m, "ticket": ticket}


def adjudicate_moisture(order_code: str, moisture_pct: float, supervisor: str) -> dict:
    """主管裁定含水争议：重算净重并留痕。"""
    conn = get_db()
    with tx():
        o = _row(conn, "SELECT * FROM orders WHERE code=?", (order_code,))
        if o is None or not o["gross_kg"]:
            raise DomainError("BAD_STATE", "缺少复磅数据，不能裁定")
        prev = o["moisture_pct"]
        net = dry_equiv(o["gross_kg"] - o["tare_kg"], moisture_pct)
        add_event(conn, order_code, "MOISTURE", moisture_pct, supervisor,
                  prev=prev, note=f"主管裁定含水由 {prev}% 改为 {moisture_pct}%，净重重算 {net}kg",
                  reviewer=supervisor)
        conn.execute("UPDATE orders SET moisture_pct=?,net_kg=?,updated_at=? WHERE code=?",
                     (moisture_pct, net, now(), order_code))
        conn.execute("UPDATE exceptions SET resolved=1 WHERE order_code=? AND kind='MOISTURE_DISPUTE' AND resolved=0",
                     (order_code,))
        return {"moisture_pct": moisture_pct, "net_kg": net}


# ---------------------------------------------------------------- 人工改重（双人复核）

def request_manual_weight(order_code: str, field: str, new_value: float,
                          requester: str, reason: str) -> int:
    """field: tare / gross。申请人与复核人必须不同角色。"""
    conn = get_db()
    role = _row(conn, "SELECT role FROM users WHERE username=?", (requester,))
    if role is None:
        raise DomainError("NO_USER", f"操作员 {requester} 不存在")
    with tx():
        o = _row(conn, "SELECT * FROM orders WHERE code=?", (order_code,))
        if o is None:
            raise DomainError("ORDER_MISSING", "订单不存在")
        if field not in ("tare", "gross"):
            raise DomainError("BAD_FIELD", "只能修改 tare 或 gross")
        cur = conn.execute(
            "INSERT INTO reviews(order_code,kind,payload,requested_by,request_role,"
            "status,comment,created_at) VALUES(?,?,?,?,?,'PENDING',?,?)",
            (order_code, "MANUAL_WEIGHT",
             json.dumps({"field": field, "new_value": new_value, "reason": reason}),
             requester, role["role"], reason, now()))
        return cur.lastrowid


def approve_manual_weight(review_id: int, reviewer: str) -> None:
    conn = get_db()
    with tx():
        r = _row(conn, "SELECT * FROM reviews WHERE id=?", (review_id,))
        if r is None or r["kind"] != "MANUAL_WEIGHT":
            raise DomainError("NO_REVIEW", "复核单不存在")
        if r["status"] != "PENDING":
            raise DomainError("DONE", "该复核单已处理")
        u = _row(conn, "SELECT * FROM users WHERE username=?", (reviewer,))
        if u is None:
            raise DomainError("NO_USER", "复核人不存在")
        if u["username"] == r["requested_by"] or u["role"] == r["request_role"]:
            raise DomainError("ROLE_CONFLICT", "人工改重必须由不同角色的第二人复核")
        payload = json.loads(r["payload"])
        o = _row(conn, "SELECT * FROM orders WHERE code=?", (r["order_code"],))
        field = payload["field"]
        new_value = float(payload["new_value"])
        if field == "tare":
            prev, new_tare, new_gross = o["tare_kg"], new_value, o["gross_kg"]
            conn.execute("UPDATE orders SET tare_kg=? WHERE code=?", (new_tare, r["order_code"]))
            add_event(conn, r["order_code"], "MANUAL", new_tare, r["requested_by"],
                      prev=prev, reviewer=reviewer, note=f"人工修改皮重：{prev}→{new_tare}kg；{payload.get('reason','')}")
        else:
            prev, new_gross = o["gross_kg"], new_value
            conn.execute("UPDATE orders SET gross_kg=? WHERE code=?", (new_gross, r["order_code"]))
            add_event(conn, r["order_code"], "MANUAL", new_gross, r["requested_by"],
                      prev=prev, reviewer=reviewer, note=f"人工修改毛重：{prev}→{new_gross}kg；{payload.get('reason','')}")
        # 重算净重
        o2 = _row(conn, "SELECT * FROM orders WHERE code=?", (r["order_code"],))
        if o2["tare_kg"] is not None and o2["gross_kg"] is not None:
            net = dry_equiv(o2["gross_kg"] - o2["tare_kg"], o2["moisture_pct"] or 0)
            conn.execute("UPDATE orders SET net_kg=?,updated_at=? WHERE code=?",
                         (net, now(), r["order_code"]))
        conn.execute("UPDATE reviews SET status='APPROVED',reviewed_by=?,reviewed_at=? WHERE id=?",
                     (reviewer, now(), review_id))


# ---------------------------------------------------------------- 客户确认 / 拒签 / 放行

def customer_confirm(order_code: str, sign: bool, operator: str = "cashier",
                     reason: str = "") -> None:
    conn = get_db()
    with tx():
        o = _row(conn, "SELECT * FROM orders WHERE code=?", (order_code,))
        if o is None:
            raise DomainError("ORDER_MISSING", "订单不存在")
        if o["status"] not in ("WEIGHED", "REJECTED"):
            raise DomainError("BAD_STATE", f"状态 {o['status']} 下不能确认重量")
        if o["net_kg"] is None:
            raise DomainError("NO_NET", "净重未计算")
        if not sign:
            log_exception(conn, "CUSTOMER_REJECT",
                          f"客户拒签出场重量 {o['net_kg']}kg，车牌 {o['plate']}，原因：{reason or '未填'}",
                          operator, order_code, o["plate"])
            conn.execute("UPDATE orders SET status='REJECTED',reject_reason=?,updated_at=? WHERE code=?",
                         (reason or "客户拒签", now(), order_code))
            return
        # 生成结算快照（价格版本固定）
        amount = round(o["net_kg"] * o["unit_price"], 2)
        conn.execute("UPDATE orders SET status='CONFIRMED',settle_price=?,settle_amount=?,"
                     "reject_reason='',updated_at=? WHERE code=?",
                     (o["unit_price"], amount, now(), order_code))


def release_gate(order_code: str, operator: str = "dispatcher") -> None:
    conn = get_db()
    with tx():
        o = _row(conn, "SELECT * FROM orders WHERE code=?", (order_code,))
        if o is None:
            raise DomainError("ORDER_MISSING", "订单不存在")
        if o["status"] != "CONFIRMED":
            raise DomainError("NOT_CONFIRMED", f"状态 {o['status']}，未确认不能放行")
        conn.execute("UPDATE orders SET status='RELEASED',released_at=?,updated_at=? WHERE code=?",
                     (now(), now(), order_code))
        # 释放残余锁定（正常应已核销为 0）
        if o["locked_stock_kg"]:
            conn.execute("INSERT INTO stock_movements(silo_id,order_code,change_kg,kind,created_at)"
                         " VALUES(?,?,?,'RELEASE',?)",
                         (o["silo_id"], order_code, int(o["locked_stock_kg"]), now()))
            conn.execute("UPDATE orders SET locked_stock_kg=0 WHERE code=?", (order_code,))


# ---------------------------------------------------------------- 退款（双人复核 + 幂等）

def request_refund(order_code: str, amount: float, requester: str, reason: str) -> int:
    conn = get_db()
    role = _row(conn, "SELECT role FROM users WHERE username=?", (requester,))
    if role is None:
        raise DomainError("NO_USER", f"操作员 {requester} 不存在")
    idem = f"REFUND:{order_code}"  # 同一订单退款同一幂等键
    with tx():
        o = _row(conn, "SELECT * FROM orders WHERE code=?", (order_code,))
        if o is None:
            raise DomainError("ORDER_MISSING", "订单不存在")
        dup = _row(conn, "SELECT id,status FROM reviews WHERE idempotency_key=?", (idem,))
        if dup is not None:
            # 退款幂等：重复请求直接返回原复核单，不重复退款
            return int(dup["id"])
        already = o["refunded_amount"] or 0
        if amount <= 0 or amount + already > (o["settle_amount"] or o["paid_amount"]) + 0.01:
            raise DomainError("BAD_AMOUNT",
                              f"退款金额非法：可退余额 {(o['settle_amount'] or o['paid_amount']) - already:.2f}")
        cur = conn.execute(
            "INSERT INTO reviews(order_code,kind,payload,requested_by,request_role,status,"
            "comment,idempotency_key,created_at) VALUES(?,?,?,?,?,'PENDING',?,?,?)",
            (order_code, "REFUND", json.dumps({"amount": amount, "reason": reason}),
             requester, role["role"], reason, idem, now()))
        return cur.lastrowid


def approve_refund(review_id: int, reviewer: str) -> dict:
    conn = get_db()
    with tx():
        r = _row(conn, "SELECT * FROM reviews WHERE id=?", (review_id,))
        if r is None or r["kind"] != "REFUND":
            raise DomainError("NO_REVIEW", "退款单不存在")
        if r["status"] == "APPROVED":
            # 幂等：已批准则直接返回既有结果，绝不重复打款
            payload = json.loads(r["payload"])
            return {"idempotent": True, "receipt": r["comment"], "amount": payload["amount"]}
        if r["status"] != "PENDING":
            raise DomainError("DONE", "该退款单已处理")
        u = _row(conn, "SELECT * FROM users WHERE username=?", (reviewer,))
        if u is None:
            raise DomainError("NO_USER", "复核人不存在")
        if u["username"] == r["requested_by"] or u["role"] == r["request_role"]:
            raise DomainError("ROLE_CONFLICT", "退款必须由不同角色的第二人复核（如主管）")
        payload = json.loads(r["payload"])
        o = _row(conn, "SELECT * FROM orders WHERE code=?", (r["order_code"],))
        receipt = adapters.payment.refund(r["order_code"], payload["amount"])["receipt"]
        conn.execute("UPDATE orders SET refunded_amount=COALESCE(refunded_amount,0)+?,"
                     "updated_at=? WHERE code=?",
                     (payload["amount"], now(), r["order_code"]))
        # 未放行订单全额退讫 → 取消；已放行订单仅登记退款（如短重退差）
        o2 = _row(conn, "SELECT * FROM orders WHERE code=?", (r["order_code"],))
        total = o2["settle_amount"] or o2["paid_amount"]
        if o2["status"] != "RELEASED" and (o2["refunded_amount"] or 0) >= total - 0.01:
            conn.execute("UPDATE orders SET status='CANCELLED' WHERE code=?", (r["order_code"],))
            if o2["locked_stock_kg"]:
                conn.execute("INSERT INTO stock_movements(silo_id,order_code,change_kg,kind,created_at)"
                             " VALUES(?,?,?,'RELEASE',?)",
                             (o2["silo_id"], r["order_code"], int(o2["locked_stock_kg"]), now()))
                conn.execute("UPDATE orders SET locked_stock_kg=0 WHERE code=?", (r["order_code"],))
        conn.execute("UPDATE reviews SET status='APPROVED',reviewed_by=?,reviewed_at=?,"
                     "comment=? WHERE id=?",
                     (reviewer, now(), receipt, review_id))
        return {"idempotent": False, "receipt": receipt, "amount": payload["amount"]}


# ---------------------------------------------------------------- 过号释放（进程内任务）

def sweep_no_shows() -> list[str]:
    """释放超过预约时段宽限期仍未入场（PAID）的订单。"""
    released: list[str] = []
    conn = get_db()
    with tx():
        rows = conn.execute("SELECT * FROM orders WHERE status='PAID'").fetchall()
        for o in rows:
            try:
                deadline = slot_dt(o["slot"]) + timedelta(minutes=NO_SHOW_GRACE_MIN)
            except (ValueError, AttributeError):
                continue
            if datetime.now() > deadline:
                exc_id = log_exception(
                    conn, "NO_SHOW",
                    f"预约 {o['slot']} 超过宽限 {NO_SHOW_GRACE_MIN} 分钟未入场，过号释放",
                    "scheduler", o["code"], o["plate"])
                conn.execute("UPDATE orders SET status='NO_SHOW',updated_at=? WHERE code=?",
                             (now(), o["code"]))
                released.append(o["code"])
    return released


# ---------------------------------------------------------------- 查询视图

def queue_view() -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM orders WHERE status IN "
        "('QUEUED','TARE','LOADING','LOADED','WEIGHED','CONFIRMED','REJECTED')"
        " ORDER BY slot,slot_seq").fetchall()


def silo_view() -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT s.*, COALESCE(SUM(CASE WHEN o.status IN "
        "('TARE','LOADING','LOADED','WEIGHED','CONFIRMED') THEN o.locked_stock_kg END),0) locked_kg "
        "FROM silos s LEFT JOIN orders o ON o.silo_id=s.id GROUP BY s.id ORDER BY s.id"
    ).fetchall()


def events_of(order_code: str) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM weight_events WHERE order_code=? ORDER BY id", (order_code,)).fetchall()
