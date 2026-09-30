"""模拟适配器：地磅读数、料仓余量、付款回执、磅单图片。

全部返回虚构数据。地磅支持"中断"开关，用于断磅样例。
"""
from __future__ import annotations

import random
from pathlib import Path

from .config import IMG_DIR
from .db import get_conn

# 测试可切换的数据库路径 / 图片目录
_DB_PATH = None
_IMG_DIR = None


def set_db_path(path) -> None:
    global _DB_PATH
    _DB_PATH = path


def set_image_dir(path) -> None:
    global _IMG_DIR
    _IMG_DIR = Path(path)
    _IMG_DIR.mkdir(parents=True, exist_ok=True)


def get_img_dir() -> Path:
    return _IMG_DIR or IMG_DIR


class ScaleError(RuntimeError):
    """地磅中断/不可用。"""


class MockScale:
    """模拟地磅。outage=True 时读数中断。"""

    def __init__(self) -> None:
        self.outage = False
        self._drift = 0.0  # 测试/样例可注入偏差

    def read(self, plate: str) -> float:
        if self.outage:
            raise ScaleError("地磅与仪表通讯中断，读数不可用")
        # 由车辆备案皮重 + 注入偏差给出稳定读数（调用方按当前阶段解释）
        conn = get_conn(_DB_PATH)
        try:
            row = conn.execute(
                "SELECT rated_tare FROM vehicles WHERE plate=?", (plate,)
            ).fetchone()
        finally:
            conn.close()
        base = row["rated_tare"] if row else 10.0
        return round(base + self._drift + random.uniform(-0.03, 0.03), 3)

    def read_gross(self, plate: str, total_load: float) -> float:
        """二次过磅：备案皮重 + 累计装载量 + 固定漂移 + 随机抖动。"""
        if self.outage:
            raise ScaleError("地磅与仪表通讯中断，读数不可用")
        conn = get_conn(_DB_PATH)
        try:
            row = conn.execute(
                "SELECT rated_tare FROM vehicles WHERE plate=?", (plate,)
            ).fetchone()
        finally:
            conn.close()
        base = row["rated_tare"] if row else 10.0
        return round(base + total_load + self._drift + random.uniform(-0.03, 0.03), 3)


class MockSiloGateway:
    """料仓控制系统：原子扣减。库存不足时返回 False，由服务层重新分配。"""

    def try_deduct(self, conn, silo_code: str, qty: float) -> bool:
        cur = conn.execute(
            "UPDATE silos SET stock = ROUND(stock - ?, 3) WHERE code=? AND stock >= ?",
            (qty, silo_code, qty),
        )
        return cur.rowcount == 1


class MockPaymentGateway:
    """付款回执适配器。"""

    def confirm(self, order_no: str, amount: float) -> dict:
        # 虚构回执号
        receipt = f"PAY-{order_no}-{int(amount) % 10000:04d}"
        return {"ok": True, "receipt": receipt, "amount": amount}


class MockImageCapture:
    """在本地目录生成占位的车辆/磅单图片（1x1 PNG，文件名可审计）。"""

    _PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6360000002000100ffff03000006000557bfabd400"
        "00000049454e44ae426082"
    )

    def capture(self, kind: str, plate_or_no: str, tag: str = "") -> str:
        safe = plate_or_no.replace(" ", "_").replace("/", "-")
        fname = f"{kind}_{safe}_{tag}.png" if tag else f"{kind}_{safe}.png"
        path = get_img_dir() / fname
        path.write_bytes(self._PNG)
        return fname


# 单例，Web 与种子共用
scale = MockScale()
silo_gw = MockSiloGateway()
payment_gw = MockPaymentGateway()
image_capture = MockImageCapture()
