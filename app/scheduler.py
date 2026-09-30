"""进程内调度任务：周期性释放过号预约。守护线程，随进程退出。"""
from __future__ import annotations

import threading
from datetime import datetime

from . import store

_started = False
_stop = threading.Event()


def _loop(interval_sec: int) -> None:
    while not _stop.wait(interval_sec):
        try:
            released = store.sweep_no_shows()
            if released:
                print(f"[scheduler {datetime.now():%H:%M:%S}] 过号释放: {', '.join(released)}")
        except Exception as exc:  # 调度失败不能拖垮进程
            print(f"[scheduler] 扫描异常: {exc}")


def start(interval_sec: int = 30) -> None:
    global _started
    if _started:
        return
    _started = True
    t = threading.Thread(target=_loop, args=(interval_sec,), name="no-show-sweeper", daemon=True)
    t.start()
    print(f"[scheduler] 过号释放任务已启动，每 {interval_sec}s 扫描一次")


def stop() -> None:
    _stop.set()
