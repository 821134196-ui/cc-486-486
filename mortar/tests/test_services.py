"""领域规则测试：车辆唯一、库存锁定、净重计算、放行不可逆、退款幂等等。

每个用例使用独立临时 SQLite，互不污染。
"""
import pathlib
import tempfile
import threading
import unittest

from mortar import adapters, services as svc
from mortar.adapters import scale
from mortar.db import get_conn, init_db


class BaseCase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        self.path = pathlib.Path(path)
        self.img_dir = pathlib.Path(tempfile.mkdtemp(prefix="mortar-img-"))
        adapters.set_db_path(self.path)
        adapters.set_image_dir(self.img_dir)
        init_db(self.path)
        scale.outage = False
        self._seed_master()

    def _seed_master(self):
        c = get_conn(self.path)
        c.executescript("""
            INSERT INTO customers(code,name) VALUES('C1','甲客户'),('C2','乙客户');
            INSERT INTO products(code,name,unit_price,price_ver)
              VALUES('M10','抹灰',476,'2026-Q3'),('M5','砌筑',428,'2026-Q3');
            INSERT INTO price_versions VALUES('M10','2026-Q2',458,'2026-04-01'),
              ('M10','2026-Q3',476,'2026-07-01');
            INSERT INTO vehicles VALUES
              ('京A0001','两轴罐车',10.0,18.0),
              ('京A0002','三轴罐车',12.0,25.0);
            INSERT INTO silos VALUES('S1','M10',100.0,120.0),('S2','M5',50.0,120.0);
        """)
        c.commit()
        c.close()

    def make_order(self, qty=20.0, product="M10", plate="京A0001", axle="两轴罐车"):
        oid = svc.create_order(customer_code="C1", product_code=product, qty=qty,
                               slot_start="2026-09-30 10:00", slot_end="2026-09-30 10:30",
                               expected_plate=plate, expected_axle=axle)
        svc.confirm_payment(oid)
        return oid

    def load_round(self, oid, plate, silo, qty, tare=10.0, moisture=3.0,
                   axle="两轴罐车"):
        """完整跑一轮：入场皮重→发料→过毛重，返回 (round_id, 净重)。"""
        rid = svc.enter_and_tare(order_id=oid, plate=plate, axle_type=axle,
                                 by="operator", tare_override=tare)
        svc.begin_load(rid, silo, qty)
        w = svc.record_gross(rid, gross_override=round(tare + qty, 3),
                             moisture_pct=moisture)
        return rid, w["net"]


class TestVehicleUniqueness(BaseCase):
    def test_plate_serves_one_order_at_a_time(self):
        o1 = self.make_order(plate="京A0001", axle="两轴罐车")
        o2 = svc.create_order(customer_code="C2", product_code="M10", qty=15.0,
                              slot_start="2026-09-30 11:00", slot_end="2026-09-30 11:30",
                              expected_plate="京A0001", expected_axle="两轴罐车")
        svc.confirm_payment(o2)
        svc.enter_and_tare(order_id=o1, plate="京A0001", axle_type="两轴罐车", by="d")
        with self.assertRaises(svc.DomainError) as cm:
            svc.enter_and_tare(order_id=o2, plate="京A0001", axle_type="两轴罐车", by="d")
        self.assertEqual(cm.exception.code, "vehicle_busy")

    def test_vehicle_type_mismatch_recorded_and_blocked(self):
        oid = self.make_order(plate="京A0001", axle="两轴罐车")
        with self.assertRaises(svc.DomainError) as cm:
            svc.enter_and_tare(order_id=oid, plate="京A0002", axle_type="三轴罐车", by="d")
        self.assertEqual(cm.exception.code, "vehicle_mismatch")
        c = get_conn(self.path)
        inc = c.execute("SELECT code FROM incidents WHERE order_id=?", (oid,)).fetchall()
        c.close()
        self.assertTrue(any(r["code"] == "vehicle_mismatch" for r in inc))

    def test_plate_reuse_after_release(self):
        oid = self.make_order(qty=10.0)
        rid, net = self.load_round(oid, "京A0001", "S1", 10.0, tare=10.0)
        svc.customer_sign(rid)
        svc.release_gate(oid)
        # 放行后同一辆车可服务下一笔新订单
        o2 = svc.create_order(customer_code="C2", product_code="M10", qty=8.0,
                              slot_start="2026-09-30 11:00", slot_end="2026-09-30 11:30",
                              expected_plate="京A0001", expected_axle="两轴罐车")
        svc.confirm_payment(o2)
        rid2, _ = self.load_round(o2, "京A0001", "S1", 8.0, tare=10.0)
        self.assertIsNotNone(rid2)


class TestSiloLocking(BaseCase):
    def test_last_segment_split_by_locked_grant(self):
        c = get_conn(self.path)
        c.execute("UPDATE silos SET stock=8.0 WHERE code='S1'")
        c.commit()
        c.close()
        oa = self.make_order(qty=10.0, plate="京A0001", axle="两轴罐车")
        ob = svc.create_order(customer_code="C2", product_code="M10", qty=10.0,
                              slot_start="2026-09-30 11:00", slot_end="2026-09-30 11:30",
                              expected_plate="京A0002", expected_axle="三轴罐车")
        svc.confirm_payment(ob)
        ra = svc.enter_and_tare(order_id=oa, plate="京A0001", axle_type="两轴罐车",
                                by="d", tare_override=10.0)
        rb = svc.enter_and_tare(order_id=ob, plate="京A0002", axle_type="三轴罐车",
                                by="d", tare_override=12.0)
        ga = svc.begin_load(ra, "S1", 5.0)
        gb = svc.begin_load(rb, "S1", 5.0)
        self.assertAlmostEqual(ga["grant"], 5.0, places=2)
        self.assertAlmostEqual(gb["grant"], 3.0, places=2)
        c = get_conn(self.path)
        stock = c.execute("SELECT stock FROM silos WHERE code='S1'").fetchone()["stock"]
        c.close()
        self.assertAlmostEqual(stock, 0.0, places=2)

    def test_concurrent_grants_threads(self):
        """两线程并发抢最后 6t，授予之和不超过库存。"""
        c = get_conn(self.path)
        c.execute("UPDATE silos SET stock=6.0 WHERE code='S1'")
        c.commit(); c.close()
        results = []

        def worker(plate, axle, tare, out):
            try:
                oid = svc.create_order(customer_code="C1", product_code="M10", qty=9.0,
                                       slot_start="2026-09-30 10:00",
                                       slot_end="2026-09-30 10:30",
                                       expected_plate=plate, expected_axle=axle)
                svc.confirm_payment(oid)
                rid = svc.enter_and_tare(order_id=oid, plate=plate, axle_type=axle,
                                         by="d", tare_override=tare)
                g = svc.begin_load(rid, "S1", 5.0)
                out.append(g["grant"])
            except svc.DomainError as e:
                out.append(0.0 if e.code == "no_stock" else -1.0)

        t1 = threading.Thread(target=worker, args=("京A0001", "两轴罐车", 10.0, results))
        t2 = threading.Thread(target=worker, args=("京A0002", "三轴罐车", 12.0, results))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(results), 2)
        self.assertAlmostEqual(sum(results), 6.0, places=2)
        self.assertNotIn(-1.0, results)

    def test_silo_mismatch_leafves_trace(self):
        oid = self.make_order(product="M5", plate="京A0001", axle="两轴罐车")
        rid = svc.enter_and_tare(order_id=oid, plate="京A0001", axle_type="两轴罐车",
                                 by="d", tare_override=10.0)
        with self.assertRaises(svc.DomainError) as cm:
            svc.begin_load(rid, "S1", 5.0)  # S1 存 M10，订单是 M5
        self.assertEqual(cm.exception.code, "silo_mismatch")


class TestNetWeight(BaseCase):
    def test_net_formula_and_moisture(self):
        oid = self.make_order(qty=20.0)
        rid = svc.enter_and_tare(order_id=oid, plate="京A0001", axle_type="两轴罐车",
                                 by="d", tare_override=10.0)
        svc.begin_load(rid, "S1", 20.0)
        w = svc.record_gross(rid, gross_override=30.0, moisture_pct=3.0)
        self.assertAlmostEqual(w["net_raw"], 20.0, places=3)
        self.assertAlmostEqual(w["net"], 20.0 * 0.97, places=3)

    def test_topup_uses_prev_gross_baseline(self):
        # 第一轮 12t，补 6t：第二轮毛重=皮重+18，本轮净重应为 6 而非 18
        oid = self.make_order(qty=20.0)
        r1, n1 = self.load_round(oid, "京A0001", "S1", 12.0, tare=10.0)
        self.assertAlmostEqual(n1, 12.0 * 0.97, places=2)
        svc.customer_sign(r1)
        r2 = svc.request_topup(oid)
        svc.begin_load(r2, "S1", 6.0)
        w2 = svc.record_gross(r2, gross_override=28.0, moisture_pct=3.0)
        self.assertAlmostEqual(w2["net_raw"], 6.0, places=2)
        self.assertAlmostEqual(w2["net"], 6.0 * 0.97, places=2)
        svc.customer_sign(r2)
        c = get_conn(self.path)
        loaded = c.execute("SELECT qty_loaded FROM orders WHERE id=?", (oid,)).fetchone()["qty_loaded"]
        c.close()
        self.assertAlmostEqual(loaded, 18.0 * 0.97, places=2)

    def test_over_order_after_topup_blocked(self):
        oid = self.make_order(qty=10.0)
        r1, _ = self.load_round(oid, "京A0001", "S1", 6.0, tare=10.0)
        svc.customer_sign(r1)
        r2 = svc.request_topup(oid)
        svc.begin_load(r2, "S1", 6.0)  # 想再装 6t → 累计 12 > 订单 10
        svc.record_gross(r2, gross_override=22.0, moisture_pct=0.0)
        with self.assertRaises(svc.DomainError) as cm:
            svc.customer_sign(r2)
        self.assertEqual(cm.exception.code, "over_order_after_topup")

    def test_overload_incident(self):
        oid = self.make_order(qty=30.0)
        rid = svc.enter_and_tare(order_id=oid, plate="京A0001", axle_type="两轴罐车",
                                 by="d", tare_override=10.0)
        svc.begin_load(rid, "S1", 20.0)
        svc.record_gross(rid, gross_override=29.0, moisture_pct=0.0)  # 净重 19 > 额定 18
        c = get_conn(self.path)
        codes = [r["code"] for r in c.execute(
            "SELECT code FROM incidents WHERE round_id=?", (rid,))]
        c.close()
        self.assertIn("overload", codes)

    def test_tare_abnormal_incident(self):
        oid = self.make_order()
        rid = svc.enter_and_tare(order_id=oid, plate="京A0001", axle_type="两轴罐车",
                                 by="d", tare_override=13.0)  # 备案 10，+30%
        c = get_conn(self.path)
        codes = [r["code"] for r in c.execute(
            "SELECT code FROM incidents WHERE round_id=?", (rid,))]
        c.close()
        self.assertIn("tare_abnormal", codes)


class TestReleaseIrreversible(BaseCase):
    def _settled_order(self):
        oid = self.make_order(qty=10.0)
        rid, _ = self.load_round(oid, "京A0001", "S1", 10.0, tare=10.0, moisture=0.0)
        svc.customer_sign(rid)
        svc.release_gate(oid)
        return oid, rid

    def test_snapshot_created(self):
        oid, _ = self._settled_order()
        c = get_conn(self.path)
        stl = c.execute("SELECT total_net,amount,price_ver FROM settlements WHERE order_id=?",
                        (oid,)).fetchone()
        c.close()
        self.assertAlmostEqual(stl["total_net"], 10.0, places=2)
        self.assertAlmostEqual(stl["amount"], 10.0 * 476.0, places=2)
        self.assertEqual(stl["price_ver"], "2026-Q3")

    def test_cannot_reopen_after_release(self):
        oid, rid = self._settled_order()
        with self.assertRaises(svc.DomainError) as cm:
            svc.begin_load(rid, "S1", 1.0)
        self.assertEqual(cm.exception.code, "released")
        with self.assertRaises(svc.DomainError) as cm2:
            svc.request_topup(oid)
        # 车辆服务已释放，按无在场车辆处理
        self.assertIn(cm2.exception.code, ("no_service", "released"))

    def test_double_release_rejected(self):
        oid, _ = self._settled_order()
        with self.assertRaises(svc.DomainError):
            svc.release_gate(oid)


class TestRefund(BaseCase):
    def _settle(self, ordered=10.0, delivered=9.0):
        oid = self.make_order(qty=ordered)
        rid, _ = self.load_round(oid, "京A0001", "S1", delivered, tare=10.0, moisture=0.0)
        svc.customer_sign(rid)
        svc.release_gate(oid)
        return oid

    def test_refund_idempotent(self):
        oid = self._settle()
        r1 = svc.request_refund(oid, "少装 1t", actor="fin1", role="finance")
        r2 = svc.request_refund(oid, "重复提交", actor="fin1", role="finance")
        self.assertFalse(r1["idempotent"])
        self.assertTrue(r2["idempotent"])
        self.assertEqual(r1["refund_id"], r2["refund_id"])
        c = get_conn(self.path)
        n = c.execute("SELECT COUNT(*) n FROM refunds WHERE order_id=?", (oid,)).fetchone()["n"]
        c.close()
        self.assertEqual(n, 1)

    def test_refund_amount_and_segregation(self):
        oid = self._settle(ordered=10.0, delivered=9.0)
        r = svc.request_refund(oid, "少 1t", actor="fin1", role="finance")
        self.assertAlmostEqual(r["amount"], 476.0, places=2)
        # 财务自己不能复核
        with self.assertRaises(svc.DomainError) as cm:
            svc.review_refund(r["refund_id"], True, actor="fin1", role="finance_mgr")
        self.assertEqual(cm.exception.code, "segregation")
        # 财务主管角色错误
        with self.assertRaises(svc.DomainError):
            svc.review_refund(r["refund_id"], True, actor="mgr1", role="operator")
        # 不同人 + 正确角色 → 付款
        svc.review_refund(r["refund_id"], True, actor="mgr1", role="finance_mgr")
        with self.assertRaises(svc.DomainError):
            svc.review_refund(r["refund_id"], True, actor="mgr2", role="finance_mgr")
        c = get_conn(self.path)
        stl = c.execute("SELECT refund_qty,refund_amt FROM settlements WHERE order_id=?",
                        (oid,)).fetchone()
        c.close()
        self.assertAlmostEqual(stl["refund_qty"], 1.0, places=2)
        self.assertAlmostEqual(stl["refund_amt"], 476.0, places=2)


class TestRevisionSegregation(BaseCase):
    def test_operator_request_supervisor_review(self):
        oid = self.make_order()
        rid, _ = self.load_round(oid, "京A0001", "S1", 10.0, tare=10.0, moisture=3.0)
        rev = svc.request_revision(rid, "moisture_pct", 5.0, "客户质疑含水率",
                                   actor="op1", role="operator")
        with self.assertRaises(svc.DomainError) as cm:
            svc.review_revision(rev, True, actor="op1", role="supervisor")
        self.assertEqual(cm.exception.code, "segregation")
        svc.review_revision(rev, True, actor="sup1", role="supervisor", note="取样复核 5%")
        c = get_conn(self.path)
        r = c.execute("SELECT net FROM load_rounds WHERE id=?", (rid,)).fetchone()
        c.close()
        self.assertAlmostEqual(r["net"], 10.0 * 0.95, places=2)


class TestScaleOutage(BaseCase):
    def test_outage_recorded_and_round_retryable(self):
        oid = self.make_order()
        rid = svc.enter_and_tare(order_id=oid, plate="京A0001", axle_type="两轴罐车",
                                 by="d", tare_override=10.0)
        svc.begin_load(rid, "S1", 10.0)
        scale.outage = True
        with self.assertRaises(svc.DomainError) as cm:
            svc.record_gross(rid)
        self.assertEqual(cm.exception.code, "scale_outage")
        scale.outage = False
        w = svc.record_gross(rid, gross_override=20.0, moisture_pct=0.0)
        self.assertAlmostEqual(w["net"], 10.0, places=2)
        c = get_conn(self.path)
        codes = [r["code"] for r in c.execute(
            "SELECT code FROM incidents WHERE round_id=?", (rid,))]
        c.close()
        self.assertIn("scale_outage", codes)


class TestNoShow(BaseCase):
    def test_sweep_releases_called_order(self):
        oid = self.make_order()
        svc.call_order(oid)
        c = get_conn(self.path)
        c.execute("UPDATE orders SET called_at=datetime('now','localtime','-120 seconds')"
                  " WHERE id=?", (oid,))
        c.commit(); c.close()
        released = svc.sweep_no_shows(45)
        c = get_conn(self.path)
        o = c.execute("SELECT called_at,no_show FROM orders WHERE id=?", (oid,)).fetchone()
        c.close()
        self.assertEqual(len(released), 1)
        self.assertIsNone(o["called_at"])
        self.assertEqual(o["no_show"], 1)
        # 释放后车辆仍可正常入场
        rid = svc.enter_and_tare(order_id=oid, plate="京A0001", axle_type="两轴罐车",
                                 by="d", tare_override=10.0)
        self.assertIsNotNone(rid)


if __name__ == "__main__":
    unittest.main()
