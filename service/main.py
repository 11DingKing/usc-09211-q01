"""项目服务入口。

运行：python3 -m service.main
环境变量：HOST（默认 127.0.0.1）、PORT（默认 8000）、DATA_DIR（默认 data，
事件日志写入 $DATA_DIR/events.jsonl；置空则仅内存运行）。
"""
import os
from http.server import ThreadingHTTPServer
from pathlib import Path

from .domain import Domain
from .httpapi import make_handler
from .store import EventStore

# 供测试与冒烟检查使用的内存态默认处理器
Handler = make_handler(Domain(EventStore()))


def run(host: str | None = None, port: int | None = None, data_dir: str | None = None):
    """启动服务。data_dir 为事件日志目录；None 时读取 DATA_DIR，缺省 data。"""
    host = host or os.environ.get("HOST", "127.0.0.1")
    port = int(port or os.environ.get("PORT", "8000"))
    if data_dir is None:
        data_dir = os.environ.get("DATA_DIR", "data")
    store = EventStore(Path(data_dir) / "events.jsonl") if data_dir else EventStore()
    domain = Domain(store)
    server = ThreadingHTTPServer((host, port), make_handler(domain))
    print(f"天地课堂联播台已启动：http://{host}:{port}（数据目录：{data_dir or '仅内存'}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
