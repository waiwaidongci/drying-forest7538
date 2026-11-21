"""新工况覆盖批量检查。

依据永远是研究任务创建时冻结的快照：训练试件取自已冻结结果的训练索引，
受理后追加的任何样本都不会进入本次判定。判定用精确十进制整数坐标，交由
convex.solve_membership 求凸组合权重或分离界限，全程不枚举组合。

受理、逐条计算、整批核对三段各自幂等：请求编号唯一，条目抢占用条件更新，
证明与状态同一事务落库，中断遗留的占用按租约退回，全部证明复核通过后
批次才置为可查看。
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from ..constants.enums import FEATURES
from ..models.coverage import CoverageBatch, CoverageItem
from ..models.run import Run
from ..services.audit import log
from ..services.convex import (
    LPError,
    integer_bound,
    solve_membership,
    verify_covered,
    verify_separated,
)
from ..utils.serialization import now

MAX_DECIMAL_PLACES = 24
MAX_SIGNIFICANT_DIGITS = 60
MAX_MAGNITUDE = 12  # 整数位上限，避免无理据的巨数拖垮精确求解
STALE_SECONDS = 120
ITEM_STATES = ('pending', 'running', 'done', 'failed')


def _parts(value, label):
    """把十进制值拆成 (sign, digits, exponent)，全程不经过上下文舍入。"""
    if isinstance(value, bool):
        raise ValueError(f'{label}不是数值')
    if isinstance(value, Decimal):
        decimal = value
    else:
        try:
            decimal = Decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(f'{label}必须是十进制数值')
    if not decimal.is_finite():
        raise ValueError(f'{label}必须是有限数值')
    sign, digits, exponent = decimal.as_tuple()
    trimmed = list(digits)
    while len(trimmed) > 1 and trimmed[-1] == 0:
        trimmed.pop()
        exponent += 1
    if not any(trimmed):
        return 0, (0,), 0
    significant = len(trimmed)
    if significant > MAX_SIGNIFICANT_DIGITS:
        raise ValueError(f'{label}有效位数超过 {MAX_SIGNIFICANT_DIGITS} 位')
    if exponent + significant > MAX_MAGNITUDE:
        raise ValueError(f'{label}超出可精确判定的量级')
    return sign, tuple(trimmed), exponent


def _digits_value(digits):
    number = 0
    for digit in digits:
        number = number * 10 + digit
    return number


def canonical(value, label='数值'):
    """规范十进制串。"""
    return _canonical(_parts(value, label))


def _canonical(parts):
    sign, digits, exponent = parts
    number = ''.join(str(digit) for digit in digits)
    if exponent >= 0:
        text = number + '0' * exponent
        return ('-' if sign else '') + text
    point = len(number) + exponent
    if point > 0:
        text = number[:point] + '.' + number[point:]
    else:
        text = '0.' + '0' * (-point) + number
    return ('-' if sign else '') + text


def _decimal_places(value, label='数值'):
    sign, digits, exponent = _parts(value, label)
    return max(0, -exponent)


def _record_text(value, label):
    """把冻结记录中的数值还原成记录时的十进制串（float 走最短往返表示）。"""
    text = repr(value) if isinstance(value, float) else str(value)
    return canonical(text, label)


def _scale_digits(basis_rows, item_values):
    places = 0
    for row in basis_rows:
        for text in row:
            places = max(places, _decimal_places(text, '训练试件'))
    for values in item_values:
        for feature in FEATURES:
            places = max(places, _decimal_places(values[feature], feature))
    if places > MAX_DECIMAL_PLACES:
        raise ValueError(f'记录小数位超过 {MAX_DECIMAL_PLACES} 位，无法精确整数化')
    return places


def _scaled(text, feature, scale_digits):
    """把十进制串精确放大为整数；放大后必须仍是整数，否则拒绝而不是四舍五入。"""
    sign, digits, exponent = _parts(text, feature)
    number = _digits_value(digits)
    power = exponent + scale_digits
    if power >= 0:
        number *= 10 ** power
    else:
        divisor = 10 ** (-power)
        if number % divisor:
            raise ValueError(f'{feature}无法按本次精度整数化')
        number //= divisor
    return -number if sign else number


def _scaled_row(row, scale_digits):
    return [_scaled(text, feature, scale_digits) for feature, text in zip(FEATURES, row)]


def _format_ratio(numerator, denominator, digits=12):
    """把分数写成十进制近似串；分数本身才是精确结果。"""
    sign = '-' if numerator < 0 else ''
    numerator = abs(numerator)
    whole, remainder = divmod(numerator, denominator)
    if remainder == 0:
        return f'{sign}{whole}'
    decimals = []
    for _ in range(digits):
        remainder *= 10
        digit, remainder = divmod(remainder, denominator)
        decimals.append(str(digit))
        if remainder == 0:
            break
    fraction = ''.join(decimals).rstrip('0')
    return f'{sign}{whole}' + (f'.{fraction}' if fraction else '')


def _format_scaled(number, scale_digits):
    """把按 10^scale_digits 放大的整数还原成十进制串。"""
    if scale_digits == 0:
        return str(number)
    sign = '-' if number < 0 else ''
    number = abs(number)
    text = str(number).rjust(scale_digits + 1, '0')
    whole, fraction = text[:-scale_digits], text[-scale_digits:].rstrip('0')
    return sign + whole + (f'.{fraction}' if fraction else '')


def frozen_basis(run):
    """取该研究冻结快照里的训练试件，返回 (references, decimal_rows, digest)。"""
    if run.state != 'completed' or not run.result:
        raise HTTPException(409, '只有已完成的研究才能做覆盖检查')
    indices = run.result.get('train_indices')
    if not isinstance(indices, list) or not indices:
        raise HTTPException(409, '该结果没有可用的训练试件索引')
    records = (run.snapshot or {}).get('records')
    if not isinstance(records, list):
        raise HTTPException(409, '任务快照缺少试件记录')
    references, rows, canonical_rows = [], [], []
    for index in indices:
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(records):
            raise HTTPException(409, '训练试件索引与冻结快照不一致')
        record = records[index]
        values = record.get('values') or {}
        try:
            row = [_record_text(values[feature], feature) for feature in FEATURES]
        except (KeyError, ValueError):
            raise HTTPException(409, '冻结快照的训练试件缺少四项输入')
        references.append(str(record.get('reference', f'#{index}')))
        rows.append(row)
        canonical_rows.append([references[-1], row])
    payload = json.dumps(canonical_rows, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    return references, rows, hashlib.sha256(payload.encode()).hexdigest()



def _bound_text(coefficients, constant):
    """界限用记录原值书写：系数·输入 > 常数（常数已是原值单位下的整数）。"""
    pieces = []
    for coefficient, feature in zip(coefficients, FEATURES):
        if coefficient == 0:
            continue
        magnitude = f'{abs(coefficient)}·{feature}'
        if not pieces:
            pieces.append(('−' if coefficient < 0 else '') + magnitude)
        else:
            pieces.append(('−' if coefficient < 0 else '+') + ' ' + magnitude)
    left = ' '.join(pieces) if pieces else '0'
    return f'{left} > {constant}'


def prepare_basis(basis_rows, scale_digits):
    """把冻结训练试件一次性化成本次精度下的整数坐标。"""
    return [_scaled_row(row, scale_digits) for row in basis_rows]


def _cover_entry(reference, index, weight):
    return {
        'reference': reference,
        'index': index,
        'numerator': weight.numerator,
        'denominator': weight.denominator,
        'fraction': f'{weight.numerator}/{weight.denominator}',
        'decimal': _format_ratio(weight.numerator, weight.denominator),
    }


def build_proof(references, points, scale, values, scale_digits):
    """对单条工况给出覆盖权重或分离界限，并立即精确复核。

    points 是 prepare_basis 的结果；若工况恰为某件训练试件，直接给 1 的权重，
    免得同一顶点写出不必要的组合。
    """
    query = _scaled_row([values[feature] for feature in FEATURES], scale_digits)
    for index, point in enumerate(points):
        if point == query:
            return {
                'kind': 'covered',
                'features': FEATURES,
                'weights': [_cover_entry(references[index], index, Fraction(1))],
                'weight_sum': '1',
                'reference_count': 1,
                'note': '与训练试件逐坐标相同，权重取一。',
                'scale_digits': scale_digits,
            }
    verdict = solve_membership(points, query, scale)
    if verdict[0] == 'covered':
        entries = [
            _cover_entry(references[index], index, weight)
            for index, weight in sorted(verdict[1], key=lambda pair: pair[0])
        ]
        if not verify_covered(
            [(entry['index'], entry['numerator'], entry['denominator']) for entry in entries], points, query
        ):
            raise LPError('覆盖权重复核未通过')
        return {
            'kind': 'covered',
            'features': FEATURES,
            'weights': entries,
            'weight_sum': '1',
            'reference_count': len(entries),
            'note': '非负权重之和精确为一；至多五件训练试件。',
            'scale_digits': scale_digits,
        }
    coefficients, constant = integer_bound(verdict[1])
    if not verify_separated(coefficients, constant, points, query, scale):
        raise LPError('分离界限复核未通过')
    query_value = sum(coefficients[k] * query[k] for k in range(len(query)))
    # 界限以记录十进制原值书写：系数·x > 常数，常数按同精度放回的整数表示。
    return {
        'kind': 'separated',
        'features': FEATURES,
        'coefficients': coefficients,
        'constant': constant,
        'bound': _bound_text(coefficients, constant),
        'excess': _format_scaled(query_value - constant * scale, scale_digits),
        'note': '全部训练试件在不等式左侧不超过常数；待判工况严格超出，故与全部训练试件可线性分离。',
        'scale_digits': scale_digits,
    }


def verify_proof(proof, references, points, scale, values, scale_digits):
    """从冻结依据重新核对已存证明。"""
    if not isinstance(proof, dict) or proof.get('kind') not in ('covered', 'separated'):
        return False
    try:
        query = _scaled_row([values[feature] for feature in FEATURES], scale_digits)
        if proof['kind'] == 'covered':
            triples = []
            for entry in proof.get('weights') or []:
                index = entry.get('index')
                if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(references):
                    return False
                if entry.get('reference') != references[index]:
                    return False
                triples.append((index, int(entry['numerator']), int(entry['denominator'])))
            return verify_covered(triples, points, query)
        coefficients = proof.get('coefficients')
        constant = proof.get('constant')
        if not isinstance(coefficients, list) or len(coefficients) != len(FEATURES):
            return False
        if not isinstance(constant, int) or isinstance(constant, bool):
            return False
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in coefficients):
            return False
        return verify_separated(coefficients, constant, points, query, scale)
    except (KeyError, TypeError, ValueError):
        return False


def _summary(batch, counts):
    return {
        'id': batch.id,
        'run_id': batch.run_id,
        'request_key': batch.request_key,
        'state': batch.state,
        'scale_digits': batch.scale_digits,
        'basis_digest': batch.basis_digest,
        'basis_count': batch.basis_count,
        'item_count': batch.item_count,
        'created_by': batch.created_by,
        'created_at': batch.created_at,
        'updated_at': batch.updated_at,
        'completed_at': batch.completed_at,
        'verified_at': batch.verified_at,
        'error': batch.error,
        'progress': {state: counts.get(state, 0) for state in ITEM_STATES},
        'viewable': batch.state == 'verified',
    }


def _counts(db, batch_id):
    counts = {state: 0 for state in ITEM_STATES}
    for state, total in db.execute(
        select(CoverageItem.state, func.count()).where(CoverageItem.batch_id == batch_id).group_by(CoverageItem.state)
    ):
        counts[state] = total
    return counts


def _compare(text, whole_limit):
    """精确比较十进制串与整数上限：返回 -1/0/1。"""
    sign, digits, exponent = _parts(text, '数值')
    number = _digits_value(digits)
    if exponent >= 0:
        value = number * 10 ** exponent
        scaled_limit = whole_limit
    else:
        # 比较 number / 10^-exp 与 whole_limit：交叉相乘，全程整数。
        value = number
        scaled_limit = whole_limit * 10 ** (-exponent)
    if sign:
        return -1 if value else 0
    return 1 if value > scaled_limit else (0 if value == scaled_limit else -1)


def _normalize_items(data):
    items = []
    for condition in data.items:
        label = condition.label.strip()
        if not label:
            raise HTTPException(422, '工况名称不能为空')
        values = {}
        for feature in FEATURES:
            try:
                values[feature] = canonical(getattr(condition, feature), feature)
            except ValueError as error:
                raise HTTPException(422, f'{label}：{error}')
        if _compare(values['moisture'], 0) <= 0 or _compare(values['moisture'], 100) > 0:
            raise HTTPException(422, f'{label}：初始湿度需在 (0,100]')
        if min(_compare(values['thickness'], 0), _compare(values['temperature'], 0),
               _compare(values['airflow'], 0)) <= 0:
            raise HTTPException(422, f'{label}：厚度、设定温度、风量需为正')
        items.append({'label': label, 'values': values})
    return items


def _payloads_match(stored, items):
    if len(stored) != len(items):
        return False
    return all(
        row.label == item['label'] and row.values == item['values'] for row, item in zip(stored, items)
    )


def create_batch(db, user, run, data):
    references, basis_rows, digest = frozen_basis(run)
    items = _normalize_items(data)
    try:
        scale_digits = _scale_digits(basis_rows, [item['values'] for item in items])
    except ValueError as error:
        raise HTTPException(422, str(error))

    existing = db.scalar(
        select(CoverageBatch).where(CoverageBatch.run_id == run.id, CoverageBatch.request_key == data.request_key)
    )
    if existing:
        return _replay(db, existing, items)

    batch = CoverageBatch(
        run_id=run.id,
        request_key=data.request_key,
        state='queued',
        scale_digits=scale_digits,
        basis_digest=digest,
        basis_count=len(references),
        item_count=len(items),
        created_by=user.email,
        created_at=now(),
        updated_at=now(),
        completed_at='',
        verified_at='',
        error='',
    )
    db.add(batch)
    try:
        db.flush()
        for position, item in enumerate(items):
            db.add(
                CoverageItem(
                    batch_id=batch.id,
                    position=position,
                    label=item['label'],
                    values=item['values'],
                    state='pending',
                    attempts=0,
                    claimed_at='',
                    error='',
                )
            )
        log(db, user, 'coverage.queued', batch.id, f'{len(items)} 条工况 · 依据 {len(references)} 件训练试件')
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(CoverageBatch).where(CoverageBatch.run_id == run.id, CoverageBatch.request_key == data.request_key)
        )
        if not existing:
            raise
        return _replay(db, existing, items)
    return summary(db, batch.id)


def _replay(db, existing, items):
    stored = db.scalars(
        select(CoverageItem).where(CoverageItem.batch_id == existing.id).order_by(CoverageItem.position)
    ).all()
    if not _payloads_match(stored, items):
        raise HTTPException(409, '请求编号已用于另一批工况，请换一个请求编号')
    return summary(db, existing.id)


def summary(db, batch_id):
    batch = db.get(CoverageBatch, batch_id)
    if not batch:
        raise HTTPException(404, '未找到覆盖检查批次')
    return _summary(batch, _counts(db, batch.id))


def list_batches(db, run):
    batches = db.scalars(
        select(CoverageBatch).where(CoverageBatch.run_id == run.id).order_by(CoverageBatch.id.desc())
    ).all()
    return [_summary(batch, _counts(db, batch.id)) for batch in batches]


def batch_detail(db, run, batch_id):
    batch = db.get(CoverageBatch, batch_id)
    if not batch or batch.run_id != run.id:
        raise HTTPException(404, '未找到覆盖检查批次')
    detail = _summary(batch, _counts(db, batch.id))
    items = db.scalars(
        select(CoverageItem).where(CoverageItem.batch_id == batch.id).order_by(CoverageItem.position)
    ).all()
    viewable = batch.state == 'verified'
    detail['items'] = [
        {
            'position': item.position,
            'label': item.label,
            'values': item.values,
            'state': item.state,
            # 整批核对齐之前不返回任何单条证明。
            'proof': item.proof if viewable else None,
            'error': item.error,
        }
        for item in items
    ]
    return detail


def claim_item(db, batch_id, owner=''):
    """用条件更新抢占最早一条 pending 工况，抢不到说明被别人处理。

    返回条目时带 attempts 作为本次占用的令牌；证明写回必须同时匹配此令牌，
    因此租约过期后旧进程的迟到写入会被拒绝，不会覆盖新进程的结果。
    """
    while True:
        candidate = db.scalar(
            select(CoverageItem.id)
            .where(CoverageItem.batch_id == batch_id, CoverageItem.state == 'pending')
            .order_by(CoverageItem.position)
            .limit(1)
        )
        if candidate is None:
            return None
        result = db.execute(
            update(CoverageItem)
            .where(CoverageItem.id == candidate, CoverageItem.state == 'pending')
            .values(
                state='running',
                claimed_at=now(),
                claimed_by=str(owner)[:120],
                attempts=CoverageItem.attempts + 1,
                error='',
            )
        )
        db.commit()
        if result.rowcount == 1:
            item = db.get(CoverageItem, candidate)
            db.refresh(item)
            return item


def _stale_cutoff(seconds=STALE_SECONDS):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def reset_stale_items(db, batch_id=None, seconds=STALE_SECONDS):
    statement = update(CoverageItem).where(
        CoverageItem.state == 'running', CoverageItem.claimed_at < _stale_cutoff(seconds)
    )
    if batch_id is not None:
        statement = statement.where(CoverageItem.batch_id == batch_id)
    result = db.execute(statement.values(state='pending', claimed_at='', claimed_by=''))
    db.commit()
    return result.rowcount


def recover_running_items(db, owner=None):
    """worker 启动时调用：退回未完成占用。

    指定 owner 时只退回本进程自己的遗留占用（上一个同名进程必然已退出），
    其他进程的占用交给租约过期处理，避免误抢仍在运行的同伴。
    """
    statement = update(CoverageItem).where(CoverageItem.state == 'running')
    if owner is not None:
        statement = statement.where(CoverageItem.claimed_by == str(owner)[:120])
    result = db.execute(statement.values(state='pending', claimed_at='', claimed_by=''))
    db.commit()
    return result.rowcount


def _fail(db, batch, message):
    db.execute(
        update(CoverageBatch)
        .where(CoverageBatch.id == batch.id)
        .values(state='failed', error=message[:2000], updated_at=now())
    )
    log(db, 'worker', 'coverage.failed', batch.id, message[:500])
    db.commit()


def process_batch(db, batch_id, owner=''):
    """幂等处理一个批次：逐条认领并落证明，最后整批复核。

    证明写入以 (id, running, attempts) 为条件，只有当前占用者能落库；
    重试不会覆盖已被别人完成的条目，重复调用也不会重复计入进度。
    """
    batch = db.get(CoverageBatch, batch_id)
    if not batch or batch.state in ('verified', 'failed'):
        return batch.state if batch else 'missing'
    run = db.get(Run, batch.run_id)
    if not run:
        _fail(db, batch, '依据任务已不存在')
        return 'failed'
    try:
        references, basis_rows, digest = frozen_basis(run)
    except HTTPException as error:
        _fail(db, batch, str(error.detail))
        return 'failed'
    if digest != batch.basis_digest or len(references) != batch.basis_count:
        _fail(db, batch, '冻结训练试件与受理时依据不一致')
        return 'failed'
    scale = 10 ** batch.scale_digits
    try:
        points = prepare_basis(basis_rows, batch.scale_digits)
    except ValueError as error:
        _fail(db, batch, str(error))
        return 'failed'

    reset_stale_items(db, batch.id)
    db.execute(
        update(CoverageBatch)
        .where(CoverageBatch.id == batch.id, CoverageBatch.state == 'queued')
        .values(state='running', updated_at=now())
    )
    db.commit()

    while True:
        item = claim_item(db, batch.id, owner)
        if item is None:
            break
        token = item.attempts
        try:
            proof = build_proof(references, points, scale, item.values, batch.scale_digits)
        except (LPError, ValueError) as error:
            result = db.execute(
                update(CoverageItem)
                .where(
                    CoverageItem.id == item.id,
                    CoverageItem.state == 'running',
                    CoverageItem.attempts == token,
                )
                .values(state='failed', error=str(error)[:2000], claimed_at='', claimed_by='')
            )
        else:
            result = db.execute(
                update(CoverageItem)
                .where(
                    CoverageItem.id == item.id,
                    CoverageItem.state == 'running',
                    CoverageItem.attempts == token,
                )
                .values(state='done', proof=proof, error='', claimed_at='', claimed_by='')
            )
        if result.rowcount:
            db.execute(update(CoverageBatch).where(CoverageBatch.id == batch.id).values(updated_at=now()))
        db.commit()

    return finalize(db, batch.id)


def finalize(db, batch_id):
    """全部条目完成后逐份复核证明；整批对齐才置为可查看。"""
    db.expire_all()  # 证明由条件更新写入，复核前丢弃身份映射里的旧值
    batch = db.get(CoverageBatch, batch_id)
    if not batch or batch.state in ('verified', 'failed'):
        return batch.state if batch else 'missing'
    counts = _counts(db, batch.id)
    if counts['pending'] or counts['running']:
        return batch.state
    if counts['failed'] or counts['done'] != batch.item_count:
        _fail(db, batch, '存在未完成的工况证明')
        return 'failed'

    run = db.get(Run, batch.run_id)
    if not run:
        _fail(db, batch, '依据任务已不存在')
        return 'failed'
    try:
        references, basis_rows, digest = frozen_basis(run)
        scale = 10 ** batch.scale_digits
        points = prepare_basis(basis_rows, batch.scale_digits)
    except (HTTPException, ValueError) as error:
        detail = getattr(error, 'detail', None) or str(error)
        _fail(db, batch, str(detail))
        return 'failed'
    if digest != batch.basis_digest:
        _fail(db, batch, '冻结训练试件与受理时依据不一致')
        return 'failed'
    for item in db.scalars(select(CoverageItem).where(CoverageItem.batch_id == batch.id)).all():
        if not verify_proof(item.proof, references, points, scale, item.values, batch.scale_digits):
            _fail(db, batch, f'第 {item.position + 1} 条工况的证明复核未通过')
            return 'failed'

    stamp = now()
    result = db.execute(
        update(CoverageBatch)
        .where(CoverageBatch.id == batch.id, CoverageBatch.state != 'verified')
        .values(state='verified', completed_at=stamp, verified_at=stamp, updated_at=stamp)
    )
    db.commit()
    if result.rowcount:
        log(db, 'worker', 'coverage.verified', batch.id, f'{batch.item_count} 条工况全部核对')
        db.commit()
    return 'verified'


def incomplete_batches(db):
    return db.scalars(
        select(CoverageBatch.id).where(CoverageBatch.state.in_(('queued', 'running'))).order_by(CoverageBatch.id)
    ).all()


COVERAGE_QUEUE = 'coverage:queue'


def enqueue(batch_id):
    """入队失败不影响受理：worker 的周期扫描会重新拾起未完成批次。"""
    from redis import Redis
    from ..config.settings import REDIS_URL

    try:
        Redis.from_url(REDIS_URL).rpush(COVERAGE_QUEUE, batch_id)
        return True
    except Exception:
        return False
