"""领域层测试：车辆唯一、库存锁定（含并发）、净重计算、放行不可逆、退款幂等。

运行：python3 -m unittest discover -s tests   或   python3 -m pytest tests/
每个用例使用独立临时 SQLite，地磅置为稳定读数（毛重=皮重+湿料），便于复算净重。
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import adapters, db, images, store  # noqa: E402

USERS = [
    ("zhang", "张调度", "dispatcher"),
    ("li", "李司磅", "weigher"),
    ("wang", "王装载", "loader"),
    ("zhao", "赵收银", "cashier"),
    ("sun", "孙主管", "supervisor"),
]


class DomainTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        db.configure(self.db_path)
        db.init_db(self.db_path)
        conn = db.get_db(self.db_path)
        conn.executemany("INSERT INTO users(username,display,role) VALUES(?,?,?)", USERS)
        conn.execute("INSERT INTO customers(id,code,name) VALUES(NULL,'C01','测试客户（虚构）')")
        conn.execute("INSERT INTO price_versions(version,effective_at,note) VALUES('V1','2026-01-01','测试价')")
        conn.executemany("INSERT INTO prices(version,model,unit_price) VALUES(?,?,?)",
                         [("V1", "M5", 0.42), ("V1", "M10", 0.48)])
        # A 仓 M10 库存 80000（并发用例另行调小）
        conn.execute("INSERT INTO silos(id,model,capacity_kg,stock_kg) VALUES('A','M10',80000,80000)")
        # 图片输出重定向到临时目录，避免污染工作区
        self.img_dir = Path(self.tmp.name) / "img"
        images.VEHICLE_DIR = self.img_dir / "vehicles"
        images.SLIP_DIR = self.img_dir / "slips"
        adapters.scale.set_stable(True)
        adapters.scale.set_online(True)

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------ 辅助流程
    def make_order(self, qty=2000, model="M10", silo="A", slot="23:30", vtype="三轴罐车"):
        return store.create_order(customer_code="C01", model=model, silo_id=silo, qty_kg=qty,
                                  slot=slot, vehicle_type=vtype, price_version="V1")

    def drive(self, code, wet=2000, moisture=0.0, plate=None, sign=True, checkin=True):
        """入场→皮重→装料→复磅→（客户确认）。返回各步结果字典。"""
        plate = plate or f"沪A·T{code[-3:]}"
        if checkin:
            store.check_in(code, plate, "三轴罐车")
        tare = store.weigh_tare(code)
        store.load_material(code, "A", wet, moisture, operator="wang")
        store.finish_loading(code)
        gross = store.weigh_gross(code, moisture_pct=moisture)
        if sign:
            store.customer_confirm(code, True)
        return {"plate": plate, **tare, **gross}


class TestVehicleUniqueness(DomainTestBase):
    def test_one_plate_one_active_order(self):
        o1 = self.make_order(2000, slot="23:00")
        o2 = self.make_order(1000, slot="23:30")
        store.check_in(o1, "沪B·7K203", "三轴罐车")
        # 同一车牌不能服务第二笔在场订单
        with self.assertRaises(store.AuditedError) as cm:
            store.check_in(o2, "沪B·7K203", "三轴罐车")
        self.assertEqual(cm.exception.code, "VEHICLE_BUSY")
        # 冲突已留痕
        exc = db.get_db(self.db_path).execute(
            "SELECT * FROM exceptions WHERE kind='VEHICLE_MISMATCH' AND plate=?", ("沪B·7K203",)).fetchone()
        self.assertIsNotNone(exc)
        # 第二笔订单仍未被绑定
        self.assertEqual(store.get_order(o2)["status"], "PAID")
        # 第一笔走完放行后，车牌释放，可服务新订单
        self.drive(o1, plate="沪B·7K203", checkin=False)
        store.release_gate(o1)
        o3 = self.make_order(500, slot="23:55")
        store.check_in(o3, "沪B·7K203", "三轴罐车")  # 不抛异常
        self.assertEqual(store.get_order(o3)["status"], "QUEUED")

    def test_vehicle_type_mismatch_logged(self):
        o = self.make_order(2000, vtype="三轴罐车")
        with self.assertRaises(store.AuditedError) as cm:
            store.check_in(o, "沪B·1Z000", "牵引罐车")
        self.assertEqual(cm.exception.code, "VEHICLE_MISMATCH")
        self.assertEqual(store.get_order(o)["status"], "PAID")


class TestStockLocking(DomainTestBase):
    def _set_stock(self, kg):
        db.get_db(self.db_path).execute("UPDATE silos SET stock_kg=? WHERE id='A'", (kg,))

    def test_last_segment_allocated_by_lock_amount(self):
        self._set_stock(3000)  # 最后一段库存只够 1.5 车
        o1 = self.make_order(2000, slot="22:00")
        o2 = self.make_order(2000, slot="22:30")
        store.check_in(o1, "沪A·L001", "三轴罐车")
        store.check_in(o2, "沪A·L002", "三轴罐车")
        r1 = store.weigh_tare(o1)
        self.assertEqual(r1["locked_kg"], 2000)
        # 第二车只能拿到剩余 1000kg，并产生库存不足留痕
        r2 = store.weigh_tare(o2)
        self.assertEqual(r2["locked_kg"], 1000)
        exc = db.get_db(self.db_path).execute(
            "SELECT * FROM exceptions WHERE order_code=? AND kind='OVERLOAD'", (o2,)).fetchone()
        self.assertIsNotNone(exc)

    def test_cannot_load_beyond_locked_amount(self):
        self._set_stock(1200)
        o = self.make_order(2000, slot="22:00")
        store.check_in(o, "沪A·L003", "三轴罐车")
        r = store.weigh_tare(o)
        self.assertEqual(r["locked_kg"], 1200)  # 部分锁定
        # 装 1200 可以
        store.load_material(o, "A", 1200, 0.0, operator="wang")
        store.finish_loading(o)
        store.weigh_gross(o)
        # 补装 100kg 超过锁定量 → 阻止（并发订单可能已取走剩余库存）
        with self.assertRaises(store.AuditedError) as cm:
            store.load_material(o, "A", 100, 0.0, operator="wang")
        self.assertEqual(cm.exception.code, "LOCK_EXCEEDED")
        # 库存没有被扣成负数
        stock = db.get_db(self.db_path).execute("SELECT stock_kg FROM silos WHERE id='A'").fetchone()["stock_kg"]
        self.assertEqual(stock, 0)

    def test_concurrent_tare_never_oversells(self):
        """8 线程同时首磅锁定 2000kg/单，库存 5000kg，总锁定绝不超卖。"""
        self._set_stock(5000)
        codes, plates = [], []
        for i in range(8):
            codes.append(self.make_order(2000, slot=f"21:{i:02d}"))
            plates.append(f"沪A·C{i:03d}")
        results, errors = [], []

        def worker(code, plate):
            try:
                store.check_in(code, plate, "三轴罐车")
                results.append(store.weigh_tare(code)["locked_kg"])
            except store.AuditedError as e:
                errors.append(e.code)

        threads = [threading.Thread(target=worker, args=(c, p)) for c, p in zip(codes, plates)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(results), 5000)                 # 恰好分完
        self.assertTrue(all(x <= 2000 for x in results))
        self.assertIn("NO_STOCK", errors)                    # 后到者无库存可锁
        stock = db.get_db(self.db_path).execute("SELECT stock_kg FROM silos WHERE id='A'").fetchone()["stock_kg"]
        self.assertEqual(stock, 5000)                        # 只锁未扣，库存不变
        # 锁定台账合计 = 5000
        locked = db.get_db(self.db_path).execute(
            "SELECT COALESCE(SUM(locked_stock_kg),0) s FROM orders WHERE status='TARE'").fetchone()["s"]
        self.assertEqual(locked, 5000)


class TestNetWeight(DomainTestBase):
    def test_moisture_correction(self):
        o = self.make_order(2000)
        r = self.drive(o, wet=2000, moisture=5.0)
        # 稳定地磅：净重 = (毛重-皮重)*(1-5%) = 2000*0.95
        self.assertAlmostEqual(r["net_kg"], 1900.0, places=1)

    def test_supervisor_adjudication_recomputes_net(self):
        o = self.make_order(2500)
        self.drive(o, wet=2500, moisture=5.0, sign=False)
        before = store.get_order(o)["net_kg"]
        out = store.adjudicate_moisture(o, 2.0, "sun")
        self.assertAlmostEqual(out["net_kg"], 2450.0, places=1)
        # 裁定事件保留原含水值，紧邻新值
        ev = db.get_db(self.db_path).execute(
            "SELECT * FROM weight_events WHERE order_code=? AND kind='MOISTURE'", (o,)).fetchone()
        self.assertEqual(ev["prev_value_kg"], 5.0)
        self.assertEqual(ev["value_kg"], 2.0)
        self.assertEqual(ev["reviewer"], "sun")
        self.assertNotEqual(before, out["net_kg"])

    def test_scale_down_recorded_and_retryable(self):
        o = self.make_order(2000)
        store.check_in(o, "沪A·D001", "三轴罐车")
        adapters.scale.set_online(False)
        with self.assertRaises(store.AuditedError) as cm:
            store.weigh_tare(o)
        self.assertEqual(cm.exception.code, "SCALE_DOWN")
        adapters.scale.set_online(True)
        r = store.weigh_tare(o)  # 恢复后可重试
        self.assertGreater(r["tare_kg"], 0)

    def test_tare_abnormal_requires_supervisor_force(self):
        o = self.make_order(1500, vtype="两轴罐车")
        store.check_in(o, "沪A·D002", "两轴罐车")
        adapters.scale.set_bias(4000)  # 读数超出两轴罐车 6000-9500 区间
        with self.assertRaises(store.AuditedError) as cm:
            store.weigh_tare(o)
        self.assertEqual(cm.exception.code, "TARE_ABNORMAL")
        adapters.scale.set_bias(0)
        # 留痕保留了当时读数
        exc = db.get_db(self.db_path).execute(
            "SELECT value_kg FROM exceptions WHERE order_code=? AND kind='TARE_ABNORMAL'", (o,)).fetchone()
        self.assertIsNotNone(exc["value_kg"])
        self.assertEqual(store.get_order(o)["status"], "QUEUED")  # 未采纳
        # 非主管不能强制
        with self.assertRaises(store.DomainError):
            store.weigh_tare(o, operator="li", force=True)
        # 主管强制采纳异常读数，事件带复核人
        r = store.weigh_tare(o, operator="sun", force=True)
        self.assertEqual(r["tare_kg"], exc["value_kg"])
        self.assertEqual(store.get_order(o)["status"], "TARE")
        ev = db.get_db(self.db_path).execute(
            "SELECT * FROM weight_events WHERE order_code=? AND kind='TARE'", (o,)).fetchone()
        self.assertEqual(ev["reviewer"], "sun")


class TestReleaseIrreversibility(DomainTestBase):
    def test_released_cannot_reopen_loading(self):
        o = self.make_order(2000)
        self.drive(o, wet=2000)
        store.release_gate(o)
        self.assertEqual(store.get_order(o)["status"], "RELEASED")
        # 已放行车辆不能重开装料
        with self.assertRaises(store.DomainError) as cm:
            store.load_material(o, "A", 100, 0.0, operator="wang")
        self.assertEqual(cm.exception.code, "RELEASED_LOCK")
        # 其它倒流操作同样被状态机拒绝
        for fn, kw in [
            (store.weigh_gross, {}),
            (store.finish_loading, {}),
            (store.customer_confirm, {"sign": True}),
            (store.release_gate, {}),
        ]:
            with self.assertRaises(store.DomainError):
                fn(o, **kw) if kw else fn(o)
        self.assertEqual(store.get_order(o)["status"], "RELEASED")


class TestRefundIdempotency(DomainTestBase):
    def _released_order(self):
        o = self.make_order(2000)
        self.drive(o, wet=2000)
        store.release_gate(o)
        return o

    def test_refund_request_idempotent(self):
        o = self._released_order()
        r1 = store.request_refund(o, 100.0, "zhao", "短重退差")
        r2 = store.request_refund(o, 100.0, "zhao", "重复点击")
        self.assertEqual(r1, r2)  # 同一幂等键返回原复核单

    def test_refund_requires_different_role(self):
        o = self._released_order()
        rid = store.request_refund(o, 100.0, "zhao", "短重退差")  # 收银发起
        # 收银自己/同角色不能复核
        with self.assertRaises(store.DomainError) as cm:
            store.approve_refund(rid, "zhao")
        self.assertEqual(cm.exception.code, "ROLE_CONFLICT")
        # 主管复核通过，只打一次款
        out = store.approve_refund(rid, "sun")
        self.assertFalse(out["idempotent"])
        self.assertEqual(store.get_order(o)["refunded_amount"], 100.0)
        # 重复审批：幂等返回，不二次打款
        out2 = store.approve_refund(rid, "sun")
        self.assertTrue(out2["idempotent"])
        self.assertEqual(out2["receipt"], out["receipt"])
        self.assertEqual(store.get_order(o)["refunded_amount"], 100.0)

    def test_full_refund_of_unreleased_cancels_and_releases_lock(self):
        o = self.make_order(2000)
        store.check_in(o, "沪A·F001", "三轴罐车")
        store.weigh_tare(o)  # 锁 2000kg
        paid = store.get_order(o)["paid_amount"]
        rid = store.request_refund(o, paid, "zhao", "客户取消")
        store.approve_refund(rid, "sun")
        fresh = store.get_order(o)
        self.assertEqual(fresh["status"], "CANCELLED")
        self.assertEqual(fresh["locked_stock_kg"], 0)


class TestManualWeightReview(DomainTestBase):
    def test_manual_weight_two_person_and_audit(self):
        o = self.make_order(2000)
        self.drive(o, wet=2000)
        orig_tare = store.get_order(o)["tare_kg"]
        rid = store.request_manual_weight(o, "tare", orig_tare - 120, "li", "地磅校准偏差")
        # 同角色（司磅）不能复核
        with self.assertRaises(store.DomainError) as cm:
            store.approve_manual_weight(rid, "li")
        self.assertEqual(cm.exception.code, "ROLE_CONFLICT")
        # 主管通过后净重随之重算，原值保留在审计事件
        store.approve_manual_weight(rid, "sun")
        ev = db.get_db(self.db_path).execute(
            "SELECT * FROM weight_events WHERE order_code=? AND kind='MANUAL'", (o,)).fetchone()
        self.assertEqual(ev["prev_value_kg"], orig_tare)
        self.assertEqual(ev["reviewer"], "sun")
        self.assertEqual(store.get_order(o)["tare_kg"], orig_tare - 120)


if __name__ == "__main__":
    unittest.main()
