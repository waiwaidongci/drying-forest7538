"""HTTP 与恢复流程测试：真 FastAPI + 临时 SQLite，不依赖外部 Redis/Postgres。

用法：python3 test_coverage_http.py
覆盖：受理/幂等重放/冲突、权限、未完成研究拒绝、整批核对前隐藏证明、
      worker 崩溃恢复、陈旧占用回收、重复处理不重复入账。
"""
import os
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TMP = Path(tempfile.mkdtemp(prefix='gb0044-'))
os.environ['DATABASE_URL'] = 'sqlite:///' + str(TMP / 'test.db')
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6399/0'
os.environ['ARTIFACT_DIR'] = str(TMP / 'artifacts')
os.environ['SEED_DEMO'] = 'false'
os.environ['JWT_SECRET'] = 'gb0044-test'
sys.path.insert(0, str(ROOT / 'backend'))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

from src.config.database import Base, Session, engine  # noqa: E402
from src.main import app  # noqa: E402
from src.models import audit, coverage as coverage_models, note, record, run, source, user  # noqa: E402,F401
from src.models.coverage import CoverageItem  # noqa: E402
from src.models.run import Run  # noqa: E402
from src.models.user import User  # noqa: E402
from src.services import coverage  # noqa: E402
from src.services.algorithm import calculate  # noqa: E402
from src.utils.security import hash_password, token_for  # noqa: E402
from src.utils.serialization import now  # noqa: E402

Base.metadata.create_all(engine)


def build_fixture():
    rng = random.Random(42)
    records = []
    for i in range(60):
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
        tokens = {}
        for role in ('admin', 'analyst', 'viewer'):
            account = User(email=role + '@gb0044.demo', password_hash=hash_password('pass'), role=role)
            db.add(account)
            db.flush()
            tokens[role] = token_for(account)
        completed = Run(source_id=1, name='已完成研究', state='completed',
                        parameters={'trees': 20, 'depth': 4},
                        snapshot={'source': {'id': 1, 'name': 's', 'filename': 'f.csv'}, 'records': records},
                        result=result, created_at=now())
        queued = Run(source_id=1, name='未完成研究', state='queued', parameters={},
                     snapshot={'source': {}, 'records': records}, created_at=now())
        db.add_all([completed, queued])
        db.commit()
        return tokens, completed.id, queued.id, records, result['train_indices']


def main():
    tokens, run_id, queued_id, records, train_indices = build_fixture()
    client = TestClient(app)
    analyst = {'Authorization': 'Bearer ' + tokens['analyst']}
    viewer = {'Authorization': 'Bearer ' + tokens['viewer']}
    sample = records[train_indices[0]]['values']
    body = {
        'request_key': 'http-0001',
        'items': [
            {'label': '原样训练点', 'moisture': str(sample['moisture']), 'thickness': str(sample['thickness']),
             'temperature': str(sample['temperature']), 'airflow': str(sample['airflow'])},
            {'label': '远超范围', 'moisture': '99.00', 'thickness': '30.00', 'temperature': '180.00', 'airflow': '20.00'},
        ],
    }
    response = client.post(f'/runs/{run_id}/coverage', json=body, headers=analyst)
    assert response.status_code == 200 and response.json()['state'] == 'queued', response.text
    batch_id = response.json()['id']

    # 整批核对齐之前，任何接口都不返回单条证明。
    detail = client.get(f'/runs/{run_id}/coverage/{batch_id}', headers=viewer).json()
    assert detail['viewable'] is False and all(item['proof'] is None for item in detail['items'])

    # 幂等重放：同请求编号同内容返回同一批次；内容不同则 409。
    replay = client.post(f'/runs/{run_id}/coverage', json=body, headers=analyst)
    assert replay.status_code == 200 and replay.json()['id'] == batch_id
    changed = {'request_key': 'http-0001', 'items': [{'label': '换了', 'moisture': '1', 'thickness': '1', 'temperature': '1', 'airflow': '1'}]}
    assert client.post(f'/runs/{run_id}/coverage', json=changed, headers=analyst).status_code == 409

    # 权限与输入边界。
    assert client.post(f'/runs/{run_id}/coverage', json=body, headers=viewer).status_code == 403
    assert client.get(f'/runs/{run_id}/coverage').status_code == 401
    assert client.post(f'/runs/{queued_id}/coverage', json=body, headers=analyst).status_code == 409
    extra = {'request_key': 'http-0002', 'items': [{'label': 'x', 'moisture': '1', 'thickness': '1', 'temperature': '1', 'airflow': '1', 'minutes': '5'}]}
    assert client.post(f'/runs/{run_id}/coverage', json=extra, headers=analyst).status_code == 422

    # 模拟“存好了单条证明但没来得及更新进度”的中断：先手动认领一条，再整体恢复。
    with Session() as db:
        claimed = coverage.claim_item(db, batch_id, 'crashed-worker')
    with Session() as db:
        assert coverage.recover_running_items(db, 'crashed-worker') == 1
        assert coverage.process_batch(db, batch_id, 'worker-2') == 'verified'

    detail = client.get(f'/runs/{run_id}/coverage/{batch_id}', headers=viewer).json()
    assert detail['viewable'] is True
    assert detail['basis_count'] == len(train_indices)
    kinds = [item['proof']['kind'] for item in detail['items']]
    assert kinds == ['covered', 'separated'], kinds
    covered = detail['items'][0]['proof']['weights']
    assert len(covered) == 1 and covered[0]['fraction'] == '1/1'
    separated = detail['items'][1]['proof']
    assert separated['kind'] == 'separated'
    assert separated['bound'].endswith('> ' + str(separated['constant']))

    # 独立复核分离界限：全部训练试件满足，待判点严格超出（Decimal 精确比较）。
    from decimal import Decimal
    features = ['moisture', 'thickness', 'temperature', 'airflow']
    coefficients = separated['coefficients']
    constant = Decimal(separated['constant'])

    def value(values):
        return sum(Decimal(str(values[f])) * c for f, c in zip(features, coefficients))

    assert all(value(records[i]['values']) <= constant for i in train_indices)
    query = {'moisture': '99.00', 'thickness': '30.00', 'temperature': '180.00', 'airflow': '20.00'}
    assert value(query) > constant

    # 重复处理不重复入账。
    with Session() as db:
        assert coverage.process_batch(db, batch_id, 'worker-3') == 'verified'
        assert len(db.scalars(select(CoverageItem).where(CoverageItem.batch_id == batch_id)).all()) == 2

    # 重复提交仍只一个批次。
    assert len(client.get(f'/runs/{run_id}/coverage', headers=analyst).json()) == 1
    print('http flow ok:', TMP)


if __name__ == '__main__':
    main()
