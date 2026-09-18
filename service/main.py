"""服务入口：组合存储、领域服务与 HTTP 层。

环境变量：
- PORT       监听端口，默认 8000
- LINEAGE_DB 事件日志文件路径，默认 $DATA_DIR/lineage.jsonl（DATA_DIR 默认 ./data）
"""
from .api import run

__all__ = ["run"]

if __name__ == "__main__":
    run()
