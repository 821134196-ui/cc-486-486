#!/usr/bin/env python3
"""端到端冒烟：走 HTTP 验证全链路与关键拦截。"""
import re
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def _do(path, data, cookie=None):
    body = urllib.parse.urlencode(data or {}).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    if cookie:
        req.add_header("Cookie", f"op={cookie}")
    try:
        with OPENER.open(req) as r:
            return r.status, r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303):
            return e.code, e.headers.get("Location", "")
        raise


def post(path, data=None):
    return _do(path, data)


def post_as(user, path, data=None):
    return _do(path, data, cookie=user)


def new_order(cust, model="M10", silo="A", qty=2000, slot="23:50"):
    st, loc = post("/orders/new", {
        "customer_code": cust, "model": model, "silo_id": silo, "qty_kg": qty,
        "slot": slot, "vehicle_type": "三轴罐车", "price_version": "2026-Q3"})
    return re.search(r"/orders/([^?]+)", loc).group(1)


def flash(loc):
    q = urllib.parse.urlparse(loc).query
    return urllib.parse.parse_qs(q).get("err", urllib.parse.parse_qs(q).get("msg", [""]))[0]


c1 = new_order("C01", qty=2000)
c2 = new_order("C02", qty=1000, slot="23:55")
print("订单:", c1, c2)

st, loc = post(f"/orders/{c1}/checkin", {"plate": "沪B·9X001", "vehicle_type": "三轴罐车"})
print("1 车辆入场:", st)
st, loc = post(f"/orders/{c2}/checkin", {"plate": "沪B·9X001", "vehicle_type": "三轴罐车"})
print("2 同车再绑另一单 ->", flash(loc))

st, loc = post(f"/orders/{c1}/tare")
print("3 首磅:", flash(loc))
st, loc = post(f"/orders/{c1}/load", {"silo_id": "A", "wet_kg": 2100, "moisture_pct": 0})
print("4 超量装料 ->", flash(loc))
st, loc = post(f"/orders/{c1}/load", {"silo_id": "B", "wet_kg": 100, "moisture_pct": 0})
print("5 错选料仓 ->", flash(loc))
st, loc = post(f"/orders/{c1}/load", {"silo_id": "A", "wet_kg": 1900, "moisture_pct": 5})
print("6 正常装料(含水5%):", flash(loc))
st, _ = post(f"/orders/{c1}/finish-load")
st, loc = post(f"/orders/{c1}/gross")
print("7 复磅:", flash(loc))
st, loc = post(f"/orders/{c1}/load", {"silo_id": "A", "wet_kg": 200, "moisture_pct": 5})
print("8 补装(余量内):", flash(loc))
st, _ = post(f"/orders/{c1}/finish-load")
st, loc = post(f"/orders/{c1}/gross")
print("9 补装后复磅:", flash(loc))
st, loc = post(f"/orders/{c1}/load", {"silo_id": "A", "wet_kg": 200, "moisture_pct": 5})
print("10 补装超单 ->", flash(loc))

# 人工改重：以司磅 li 申请，li 同角色复核应被拒，主管 sun 通过
st, loc = post_as("li", f"/orders/{c1}/manual-weight",
                  {"field": "tare", "new_value": 11200, "reason": "校准偏差"})
print("11 改重申请(司磅发起):", loc)
# cookie 切人
import sqlite3, sys
sys.path.insert(0, ".")
from app.db import get_db
rid = get_db("data/app.db").execute(
    "SELECT id FROM reviews WHERE order_code=? AND kind='MANUAL_WEIGHT' ORDER BY id DESC LIMIT 1", (c1,)).fetchone()["id"]
st, loc = post_as("li", f"/reviews/{rid}/approve-weight")
print("12 同角色(司磅)复核 ->", flash(loc))
st, loc = post_as("sun", f"/reviews/{rid}/approve-weight")
print("13 主管复核 ->", flash(loc))

st, _ = post(f"/orders/{c1}/confirm", {"sign": "1"})
st, loc = post(f"/orders/{c1}/release")
print("14 放行:", flash(loc))
st, loc = post(f"/orders/{c1}/load", {"silo_id": "A", "wet_kg": 10, "moisture_pct": 0})
print("15 放行后再装料 ->", flash(loc))

# 退款幂等
st, loc = post(f"/orders/{c1}/refund", {"amount": 100, "reason": "短重退差"})
rrid = get_db("data/app.db").execute(
    "SELECT id FROM reviews WHERE order_code=? AND kind='REFUND' ORDER BY id DESC LIMIT 1", (c1,)).fetchone()["id"]
st, loc = post(f"/orders/{c1}/refund", {"amount": 100, "reason": "重复发起"})
rrid2 = get_db("data/app.db").execute(
    "SELECT id FROM reviews WHERE order_code=? AND kind='REFUND' ORDER BY id DESC LIMIT 1", (c1,)).fetchone()["id"]
print("16 重复退款申请返回同一复核单:", rrid == rrid2)
st, loc = post_as("zhao", f"/reviews/{rrid}/approve-refund")
print("17 收银(同cashier?申请默认dispatcher) 复核: ", flash(loc))
st, loc = post_as("sun", f"/reviews/{rrid}/approve-refund")
print("18 主管批准退款:", flash(loc))
st, loc = post_as("sun", f"/reviews/{rrid}/approve-refund")
print("19 重复审批幂等:", flash(loc))
o = get_db("data/app.db").execute("SELECT refunded_amount FROM orders WHERE code=?", (c1,)).fetchone()
print("20 退款累计（应=100 而非200）:", o["refunded_amount"])
