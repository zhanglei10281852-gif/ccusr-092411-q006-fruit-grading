"""TraceSystem:组装全部服务的门面,演示与测试共用的入口。"""
from __future__ import annotations

from . import db
from .clock import Clock
from .services.audit import AuditService
from .services.evidence import EvidenceService
from .services.inspection import InspectionService
from .services.orders import OrderService
from .services.registry import RegistryService
from .services.release import ReleaseService
from .services.ruling import RulingService
from .services.sampling import SamplingService


class TraceSystem:
    """批次分级追溯系统。

    用法::

        system = TraceSystem()                # 内存库
        system = TraceSystem("trace.db")      # 文件库(支持并发连接)
    """

    def __init__(self, db_path: str = ":memory:", clock=None):
        self.conn = db.connect(db_path)
        db.init_db(self.conn)
        self.clock = clock or Clock()
        self.registry = RegistryService(self.conn, self.clock)
        self.evidence = EvidenceService(self.conn, self.clock)
        self.sampling = SamplingService(self.conn)
        self.orders = OrderService(self.conn, self.clock)
        self.rulings = RulingService(self.conn, self.clock)
        self.releases = ReleaseService(self.conn, self.clock, self.evidence, self.orders)
        self.inspections = InspectionService(
            self.conn, self.clock, self.sampling, self.orders, self.releases
        )
        self.audit = AuditService(self.conn, self.evidence)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "TraceSystem":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
