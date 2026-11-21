"""独立 oracle：不导入被测求解器，穷举至多 5 件训练试件核验覆盖/分离。

用法（stdlib 即可）：python3 oracle_coverage.py
随机生成小规模整数点集，用精确 Fraction 高斯消元穷举子集判断凸包成员，
再调用后端求解器，逐例核对：
  * 判“覆盖”时，权重非负、件数≤5、和恰为 1、加权逐坐标等于待判点；
  * 判“分离”时，全部训练点满足 c·x ≤ C，且待判点 c·x > C。
"""
import itertools
import random
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from src.services.convex import integer_bound, solve_membership, verify_covered, verify_separated  # noqa: E402


def brute_feasible(points, query, scale):
    n, m = len(points), 5
    cols = [list(p) + [scale] for p in points]
    target = list(query) + [scale]

    def solve(subset):
        k = len(subset)
        table = [[Fraction(cols[j][i]) for j in subset] + [Fraction(target[i])] for i in range(m)]
        row, pivots = 0, []
        for column in range(k):
            pivot = next((i for i in range(row, m) if table[i][column] != 0), None)
            if pivot is None:
                continue
            table[row], table[pivot] = table[pivot], table[row]
            lead = table[row][column]
            table[row] = [value / lead for value in table[row]]
            for i in range(m):
                if i != row and table[i][column] != 0:
                    factor = table[i][column]
                    table[i] = [a - factor * b for a, b in zip(table[i], table[row])]
            pivots.append(column)
            row += 1
            if row == m:
                break
        for i in range(row, m):
            if all(table[i][c] == 0 for c in range(k)) and table[i][k] != 0:
                return None
        weights = [Fraction(0)] * k
        for i, column in enumerate(pivots):
            weights[column] = table[i][k]
        return None if any(w < 0 for w in weights) else weights

    for size in range(1, min(5, n) + 1):
        for subset in itertools.combinations(range(n), size):
            if solve(subset) is not None:
                return True
    return False


def main(trials=600, seed=20261121):
    rng = random.Random(seed)
    for trial in range(trials):
        n = rng.randint(1, 9)
        scale = rng.choice([1, 10, 100, 1000])
        points = [tuple(rng.randint(-10 ** 6, 10 ** 6) for _ in range(4)) for _ in range(n)]
        if rng.random() < 0.6:
            size = rng.randint(1, min(4, n))
            subset = rng.sample(range(n), size)
            weights = [rng.randint(1, 9) for _ in subset]
            total = sum(weights)
            query = tuple(sum(w * points[j][i] for w, j in zip(weights, subset)) // total for i in range(4))
        else:
            query = tuple(rng.randint(-10 ** 6, 10 ** 6) for _ in range(4))
        expected = brute_feasible(points, query, scale)
        verdict = solve_membership(points, query, scale)
        assert (verdict[0] == 'covered') == expected, (trial, verdict[0], expected)
        if verdict[0] == 'covered':
            entries = [(i, w.numerator, w.denominator) for i, w in verdict[1]]
            assert len(entries) <= 5
            assert verify_covered(entries, points, query), trial
            assert sum(w for _, w in verdict[1]) == 1, trial
        else:
            coefficients, constant = integer_bound(verdict[1])
            assert verify_separated(coefficients, constant, points, query, scale), trial
    print(f'oracle ok: {trials} trials, seed={seed}')


if __name__ == '__main__':
    main()
