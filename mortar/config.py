"""全局路径与常量。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("MORTAR_DATA", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "mortar.db"
IMG_DIR = DATA_DIR / "images"

HOST = os.environ.get("MORTAR_HOST", "0.0.0.0")
PORT = int(os.environ.get("MORTAR_PORT", "8000"))

# 呼号后车辆未入场的宽限秒数，超时由进程内任务判定过号并释放预约
GRACE_SECONDS = int(os.environ.get("MORTAR_GRACE", "45"))
# 后台扫描间隔
SWEEP_INTERVAL = 3.0

# ---- 角色 ----
ROLE_DISPATCHER = "dispatcher"      # 调度/门岗
ROLE_OPERATOR = "operator"          # 磅房操作员
ROLE_SUPERVISOR = "supervisor"      # 主管（改重复核）
ROLE_FINANCE = "finance"            # 财务（退款发起）
ROLE_FINANCE_MGR = "finance_mgr"    # 财务主管（退款复核）
ROLE_CUSTOMER = "customer"          # 客户（签收/拒签）

ROLE_NAMES = {
    ROLE_DISPATCHER: "调度员",
    ROLE_OPERATOR: "磅房操作员",
    ROLE_SUPERVISOR: "当班主管",
    ROLE_FINANCE: "财务专员",
    ROLE_FINANCE_MGR: "财务主管",
    ROLE_CUSTOMER: "客户",
}

# 皮重相对该车型备案皮重的允许偏差
TARE_TOLERANCE = 0.12
# 默认实验室含水含水率（%）
DEFAULT_MOISTURE_PCT = 3.0
