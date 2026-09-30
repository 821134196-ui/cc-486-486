"""模拟外部适配器：地磅、料仓料位、付款回执。全部返回虚构数据。

地磅可被置为“中断”状态（SCALE_DOWN），期间读数抛出 ScaleOffline；
料仓料位由数据库库存驱动；付款回执为本地生成的假凭证号。
"""
from __future__ import annotations

import threading
from datetime import datetime


class AdapterError(RuntimeError):
    pass


class ScaleOffline(AdapterError):
    """地磅中断，无法取得稳定读数。"""


class ScaleAdapter:
    """模拟地磅：稳定读数带微小抖动，可切换在线/中断。"""

    def __init__(self) -> None:
        self._online = True
        self._stable = False
        self._bias = 0.0
        self._lock = threading.Lock()
        self.read_count = 0

    @property
    def online(self) -> bool:
        with self._lock:
            return self._online

    def set_online(self, online: bool) -> None:
        with self._lock:
            self._online = online

    def set_stable(self, stable: bool) -> None:
        """稳定模式：读数严格等于期望值，供测试复算净重。"""
        with self._lock:
            self._stable = stable

    def set_bias(self, kg: float) -> None:
        """注入仪表系统偏差（kg），用于制造“皮重异常”样例；正常为 0。"""
        with self._lock:
            self._bias = kg

    def read(self, expected_kg: float, jitter_kg: float = 2.0) -> float:
        """根据期望重量返回虚构仪表读数。中断时抛 ScaleOffline。"""
        with self._lock:
            online = self._online
            stable = self._stable
            bias = self._bias
            self.read_count += 1
        if not online:
            raise ScaleOffline("地磅通讯中断，仪表无稳定读数（SCALE_DOWN）")
        if stable:
            return round(float(expected_kg) + bias, 1)
        # 确定性“抖动”：基于读数次数，避免使用随机数，样例可复现
        delta = ((self.read_count * 37) % 7) - 3  # -3..3 kg
        return round(float(expected_kg) + delta * (jitter_kg / 3.0) * 0 + delta + bias, 1)

    def ticket_no(self) -> str:
        return f"WB-{datetime.now().strftime('%Y%m%d')}-{self.read_count:05d}"


class SiloAdapter:
    """料仓料位适配器：库存来自 DB，此处只负责格式化料位读数。"""

    def level_pct(self, stock_kg: int, capacity_kg: int) -> float:
        if capacity_kg <= 0:
            return 0.0
        return max(0.0, min(100.0, stock_kg / capacity_kg * 100))

    def probe(self, silo_id: str, stock_kg: int, capacity_kg: int) -> dict:
        return {
            "silo": silo_id,
            "stock_kg": stock_kg,
            "capacity_kg": capacity_kg,
            "level_pct": round(self.level_pct(stock_kg, capacity_kg), 1),
            "probe_at": datetime.now().strftime("%H:%M:%S"),
        }


class PaymentAdapter:
    """模拟付款渠道：返回虚构回执，永远成功（演示用）。"""

    def confirm(self, order_code: str, amount: float) -> dict:
        receipt = f"PAY-FAKE-{order_code[-4:]}-{datetime.now():%H%M%S}"
        return {
            "ok": True,
            "receipt": receipt,
            "amount": round(amount, 2),
            "channel": "MOCK-SETTLE",
            "paid_at": datetime.now().isoformat(timespec="seconds"),
        }

    def refund(self, order_code: str, amount: float) -> dict:
        receipt = f"REF-FAKE-{order_code[-4:]}-{datetime.now():%H%M%S}"
        return {
            "ok": True,
            "receipt": receipt,
            "amount": round(amount, 2),
            "channel": "MOCK-REFUND",
            "refunded_at": datetime.now().isoformat(timespec="seconds"),
        }


# 单例（进程内模拟硬件）
scale = ScaleAdapter()
silo_probe = SiloAdapter()
payment = PaymentAdapter()
