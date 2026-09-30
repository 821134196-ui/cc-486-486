"""一条命令启动：python3 -m mortar

初始化 SQLite、写入超量/断磅/拒签等虚构样例，并启动本地 Web（含过号释放后台任务）。
可选：
  python3 -m mortar seed      仅重建样例数据
  python3 -m mortar test      运行内置测试
"""
import sys

from .config import HOST, PORT
from .db import init_db


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "serve"
    if cmd == "seed":
        from .seed import seed
        ids = seed()
        print("样例数据已重建：")
        for k, v in ids.items():
            print(f"  {k}: {v}")
        return 0
    if cmd == "test":
        import unittest
        from .tests import test_services
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromModule(test_services)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    init_db()
    from .web import run
    run(host=HOST, port=PORT, do_seed=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
