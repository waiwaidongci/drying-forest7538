"""并发与中断恢复测试：临时 SQLite，直接调用 service 层。

用法：python3 test_coverage_recovery.py
覆盖：
  1. 单条证明已落库但进度未更新时中断，重跑能补齐并 verified；
  2. 两个 worker 并发处理同一批次，不重复入账、恰有一个 verified；
  3. 陈旧占用（租约过期）被回收，旧令牌的迟到写入被拒绝；
  4. 一位 worker 的陈旧回收不会抢走另一位新近占用；
  5. 证明被篡改或依据摘要变化时，整批拒绝置为可查看（fail-closed）。
"""
import os
import random
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TMP = Path(tempfile.mkdtemp(prefix='gb0044-recovery-'))
os.environ['DATABASE_URL'] = 'sqlite:///' + str(TMP / 'recovery.db')
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6399/0'
os.environ['ARTIFACT_DIR'] = str(TMP / 'artifacts')
sys.path.insert(0, str(ROOT / 'backend'))

from sqlalchemy import select, update  # noqa: E402

from src.config.database import Base, Session, engine  # noqa: E402
from src.models import audit, coverage as coverage_models, note, record, run, source, user  # noqa: E402,F401
from src.models.coverage import CoverageBatch, CoverageItem  # noqa: E402
from src.models.run import Run  # noqa: E402
from src.schemas.coverage import CoverageInput  # noqa: E402
from src.services import coverage  # noqa: E402
from src.services.algorithm import calculate  # noqa: E402
from src.utils.serialization import now  # noqa: E402

Base.metadata.create_all(engine)


class Analyst:
    email = 'analyst@gb0044.demo'
    role = 'analyst'


def build_run():
    rng = random.Random(7)
    records = []
    for i in range(50):
        moisture = round(rng.uniform(20, 60), 2)
        thickness = round(rng.uniform(1, 8), 2)
        temperature = round(rng.uniform(50, 100), 2)
        airflow = round(rng.uniform(1, 5), 2)
        minutes = round(max(5, 12 + 1.6 * moisture + 8 * thickness - 0.45 * temperature - 3 * airflow + rng.gauss(0, 3)), 2)
        records.append({'id': i + 1, 'source_id': 1, 'reference': f'HX-{i + 1:04d}',
                        'values': {'moisture': moisture, 'thickness': thickness, 'temperature': temperature,
                                   'airflow': airflow, 'minutes': minutes}})
    result = calculate(records, {'trees': 20, 'depth': 4})
    with Session() as db:
        instance = Run(source_id=1, name='恢复测试研究', state='completed',
                       parameters={'trees': 20, 'depth': 4},
                       snapshot={'source': {'id': 1}, 'records': records}, result=result, created_at=now())
        db.add(instance)
        db.commit()
        return instance.id


def make_batch(run_id, key, size=6, sentinel=False):
    items = [{'label': f'c{i}', 'moisture': f'{25 + i}.5', 'thickness': '4', 'temperature': '70', 'airflow': '3'}
             for i in range(size)]
    if sentinel:
        items.append({'label': '超界', 'moisture': '99', 'thickness': '30', 'temperature': '180', 'airflow': '20'})
    with Session() as db:
        summary = coverage.create_batch(db, Analyst(), db.get(Run, run_id),
                                        CoverageInput.model_validate({'request_key': key, 'items': items}))
        return summary['id']


def test_resume_after_partial_progress(run_id):
    batch_id = make_batch(run_id, 'resume-001')
    with Session() as db:
        batch = db.get(CoverageBatch, batch_id)
        references, rows, _ = coverage.frozen_basis(db.get(Run, run_id))
        points = coverage.prepare_basis(rows, batch.scale_digits)
        scale = 10 ** batch.scale_digits
        items = db.scalars(select(CoverageItem).where(CoverageItem.batch_id == batch_id).order_by(CoverageItem.position)).all()
        for item in items[:2]:
            proof = coverage.build_proof(references, points, scale, item.values, batch.scale_digits)
            db.execute(update(CoverageItem).where(CoverageItem.id == item.id).values(state='done', proof=proof))
        db.execute(update(CoverageBatch).where(CoverageBatch.id == batch_id).values(state='running'))
        db.commit()
        assert coverage._counts(db, batch_id)['done'] == 2
    with Session() as db:
        assert coverage.process_batch(db, batch_id, 'resumed') == 'verified'
        assert coverage._counts(db, batch_id)['done'] == 6


def test_concurrent_workers(run_id):
    batch_id = make_batch(run_id, 'race-0001', size=12)
    outcomes = []

    def worker(name):
        for _ in range(4):
            with Session() as db:
                outcomes.append(coverage.process_batch(db, batch_id, name))
            time.sleep(0.03)

    threads = [threading.Thread(target=worker, args=(f'w{i}',)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with Session() as db:
        counts = coverage._counts(db, batch_id)
        items = db.scalars(select(CoverageItem).where(CoverageItem.batch_id == batch_id)).all()
        assert counts == {'pending': 0, 'running': 0, 'done': 12, 'failed': 0}, counts
        assert len(items) == 12
        assert all(item.proof for item in items)
        assert all(item.attempts == 1 for item in items), [item.attempts for item in items]
        # 先收尾的一方可能看到对方仍占用的条目而返回 running；最终必须 verified。
        assert set(outcomes) <= {'verified', 'running'}, outcomes
        assert 'verified' in outcomes
        assert db.get(CoverageBatch, batch_id).state == 'verified'


def test_stale_claim_and_late_write(run_id):
    batch_id = make_batch(run_id, 'stale-001', size=3)
    with Session() as db:
        item = coverage.claim_item(db, batch_id, 'worker-A')
        token = item.attempts
    with Session() as db:
        assert coverage.reset_stale_items(db, batch_id) == 0  # 新近占用不被抢
        db.execute(update(CoverageItem).where(CoverageItem.id == item.id).values(claimed_at='2000-01-01T00:00:00+00:00'))
        db.commit()
        assert coverage.reset_stale_items(db, batch_id) == 1
        reclaimed = coverage.claim_item(db, batch_id, 'worker-B')
        assert reclaimed.id == item.id and reclaimed.attempts == token + 1
        references, rows, _ = coverage.frozen_basis(db.get(Run, run_id))
        batch = db.get(CoverageBatch, batch_id)
        points = coverage.prepare_basis(rows, batch.scale_digits)
        proof = coverage.build_proof(references, points, 10 ** batch.scale_digits, reclaimed.values, batch.scale_digits)
        stale_write = db.execute(
            update(CoverageItem)
            .where(CoverageItem.id == item.id, CoverageItem.state == 'running', CoverageItem.attempts == token)
            .values(state='done', proof=proof)
        )
        db.commit()
        assert stale_write.rowcount == 0  # 旧令牌的迟到写入被拒
        # 当前占用者用自己的令牌落证明。
        current_write = db.execute(
            update(CoverageItem)
            .where(CoverageItem.id == item.id, CoverageItem.state == 'running',
                   CoverageItem.attempts == reclaimed.attempts)
            .values(state='done', proof=proof)
        )
        db.commit()
        assert current_write.rowcount == 1
    with Session() as db:
        assert coverage.process_batch(db, batch_id, 'worker-B') == 'verified'


def test_recovery_only_own_claims(run_id):
    batch_id = make_batch(run_id, 'owner-001', size=3)
    with Session() as db:
        coverage.claim_item(db, batch_id, 'worker-A')
    with Session() as db:
        assert coverage.recover_running_items(db, 'worker-B') == 0
        assert coverage.recover_running_items(db, 'worker-A') == 1
    with Session() as db:
        assert coverage.process_batch(db, batch_id, 'worker-C') == 'verified'


def test_tamper_and_basis_change(run_id):
    batch_id = make_batch(run_id, 'tamper-01', size=2, sentinel=True)
    with Session() as db:
        batch = db.get(CoverageBatch, batch_id)
        references, rows, _ = coverage.frozen_basis(db.get(Run, run_id))
        points = coverage.prepare_basis(rows, batch.scale_digits)
        scale = 10 ** batch.scale_digits
        for item in db.scalars(select(CoverageItem).where(CoverageItem.batch_id == batch_id)).all():
            proof = coverage.build_proof(references, points, scale, item.values, batch.scale_digits)
            if proof['kind'] == 'separated':
                proof['constant'] -= 1  # 让待判点不再严格超出
            db.execute(update(CoverageItem).where(CoverageItem.id == item.id).values(state='done', proof=proof))
        db.execute(update(CoverageBatch).where(CoverageBatch.id == batch_id).values(state='running'))
        db.commit()
    with Session() as db:
        assert coverage.process_batch(db, batch_id, 'worker') == 'failed'
        assert '复核未通过' in db.get(CoverageBatch, batch_id).error

    batch_id = make_batch(run_id, 'digest-01', size=2)
    with Session() as db:
        instance = db.get(Run, run_id)
        tampered = dict(instance.result)
        tampered['train_indices'] = tampered['train_indices'][:-1]
        instance.result = tampered
        db.commit()
    with Session() as db:
        assert coverage.process_batch(db, batch_id, 'worker') == 'failed'
        assert '依据不一致' in db.get(CoverageBatch, batch_id).error


def main():
    run_id = build_run()
    test_resume_after_partial_progress(run_id)
    test_concurrent_workers(run_id)
    test_stale_claim_and_late_write(run_id)
    test_recovery_only_own_claims(run_id)
    test_tamper_and_basis_change(run_id)
    print('recovery & concurrency ok:', TMP)


if __name__ == '__main__':
    main()
