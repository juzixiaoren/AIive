"""测试 OutboxWorker（发件箱工作器）模块。

覆盖任务的入队、处理、重试、死信队列及批量处理功能。
"""

from aiive.db.models import OutboxJob
from aiive.worker.outbox_worker import OutboxWorker


class TestOutboxWorker:
    """测试 OutboxWorker 的任务生命周期管理。"""

    def test_enqueue_creates_pending_job(self, db_session):
        """入队操作应创建状态为 pending 的任务。"""
        worker = OutboxWorker(lambda: db_session)
        job = worker.enqueue(
            db_session, "test_job", {"key": "value"}, trace_id="trace-1"
        )
        db_session.flush()

        assert job.operation_id
        assert job.job_type == "test_job"
        assert job.status == "pending"
        assert job.payload == {"key": "value"}

    def test_process_one_executes_handler(self, db_session):
        """process_one 应正确执行注册的处理器。"""
        results = []

        def handler(db, payload, trace_id):
            results.append(payload)

        worker = OutboxWorker(lambda: db_session)
        worker.register_handler("echo", handler)
        worker.enqueue(db_session, "echo", {"msg": "hello"})
        db_session.flush()

        count = worker.process_one(db_session)
        assert count == 1
        assert len(results) == 1
        assert results[0] == {"msg": "hello"}

    def test_process_one_marks_completed(self, db_session):
        """成功处理后的任务应标记为 completed。"""
        def handler(db, p, t):
            pass

        worker = OutboxWorker(lambda: db_session)
        worker.register_handler("done", handler)
        worker.enqueue(db_session, "done", {})
        db_session.flush()
        worker.process_one(db_session)

        job = db_session.query(OutboxJob).first()
        assert job.status == "completed"

    def test_retry_on_failure(self, db_session):
        """处理器失败时应增加重试计数并保持 pending 状态。"""
        def fail_handler(db, p, t):
            raise RuntimeError("boom")

        worker = OutboxWorker(lambda: db_session)
        worker.register_handler("fail", fail_handler)
        worker.enqueue(db_session, "fail", {})
        db_session.flush()
        worker.process_one(db_session)

        job = db_session.query(OutboxJob).first()
        assert job.status == "pending"
        assert job.retry_count == 1

    def test_deadletter_after_max_retries(self, db_session):
        """超过最大重试次数后任务应进入 deadletter 状态。"""
        def fail_handler(db, p, t):
            raise RuntimeError("boom")

        worker = OutboxWorker(lambda: db_session)
        worker.register_handler("fail", fail_handler)
        worker.enqueue(db_session, "fail", {})
        db_session.flush()

        for _ in range(3):
            # 每次重新获取
            db_session.expire_all()
            worker.process_one(db_session)

        job = db_session.query(OutboxJob).first()
        assert job.status == "deadletter"

    def test_unknown_job_type_deadletter(self, db_session):
        """未注册的任务类型应直接进入 deadletter。"""
        worker = OutboxWorker(lambda: db_session)
        worker.enqueue(db_session, "no_handler", {})
        db_session.flush()
        worker.process_one(db_session)

        job = db_session.query(OutboxJob).first()
        assert job.status == "deadletter"

    def test_process_all_with_multiple_jobs(self, db_session):
        """process_all 应批量处理所有待处理任务。"""
        def handler(db, p, t):
            pass

        worker = OutboxWorker(lambda: db_session)
        worker.register_handler("t", handler)
        for i in range(5):
            worker.enqueue(db_session, "t", {"i": i})
        db_session.flush()

        processed = worker.process_all(db_session, max_jobs=10)
        assert processed == 5

        jobs = db_session.query(OutboxJob).all()
        for j in jobs:
            assert j.status == "completed"
