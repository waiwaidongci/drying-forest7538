"""精确有理数凸包覆盖判定。

纯整数/有理数运算，不依赖浮点，也不枚举训练试件的组合。问题写成
    A λ = b,  λ >= 0
其中 A 的列是 (训练点, D)、b = (待判点, D)。最后一维使 Σλ = 1，
于是可行当且仅当待判点是训练点集的凸组合。用第一阶段单纯形精确求解：

* 可行：基本解里至多 m 个非零 λ（m = 维数 + 1 = 5），即至多五件试件；
* 不可行：返回对偶证书 y，且 y·(训练点, D) <= 0 < y·(待判点, D)，
  化为整数线性界限后，全部训练试件满足「系数·x <= 常数」，待判点严格超出。

行交换、判定入基、Bland 反循环规则和最小比值出基都只用整数比较，边界
（待判点落在凸包边界上）按可行处理。
"""

import math
from fractions import Fraction


class LPError(RuntimeError):
    """精确线性规划无法完成（基奇异、主元超限等）。"""


def _det(matrix):
    """Bareiss 分数消元求整数矩阵行列式，全程整数。"""
    n = len(matrix)
    if n == 1:
        return matrix[0][0]
    if n == 2:
        return matrix[0][0] * matrix[1][1] - matrix[0][1] * matrix[1][0]
    a = [row[:] for row in matrix]
    sign = 1
    previous = 1
    for k in range(n - 1):
        if a[k][k] == 0:
            swap = None
            for i in range(k + 1, n):
                if a[i][k] != 0:
                    swap = i
                    break
            if swap is None:
                return 0
            a[k], a[swap] = a[swap], a[k]
            sign = -sign
        pivot = a[k][k]
        for i in range(k + 1, n):
            for j in range(k + 1, n):
                a[i][j] = (a[i][j] * pivot - a[i][k] * a[k][j]) // previous
        previous = pivot
        for i in range(k + 1, n):
            a[i][k] = 0
    return sign * a[n - 1][n - 1]


def _adjugate(matrix):
    """整数矩阵的伴随矩阵（所有元素为整数）。"""
    n = len(matrix)
    adj = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if n == 1:
                adj[j][i] = 1
                continue
            minor = [row[:j] + row[j + 1:] for index, row in enumerate(matrix) if index != i]
            cofactor = _det(minor)
            if (i + j) % 2:
                cofactor = -cofactor
            adj[j][i] = cofactor
    return adj


def _sign(value):
    return 1 if value >= 0 else -1


def solve_membership(points, query, scale, max_pivots=200000):
    """判断 query 是否属于 points 的凸包。

    参数都是共享同一十进制分母 `scale` 的整数坐标（最后一项是 scale 的
    倍数，用来固定权重和为 1）。返回：

    * ('covered', [(index, Fraction), ...])：非负权重，至多 len(query) 个，
      权重和恰为 1，且加权点与 query 逐坐标相等；
    * ('separated', (Fraction, ...))：长度 len(query) 的向量 y（最后一维为
      常数项），满足对每个训练点 y·(p, scale) <= 0，且 y·(query, scale) > 0。

    两侧都不做浮点近似；不可行时严格分离，可行时包含边界。
    """
    n = len(points)
    m = len(query) + 1
    columns = [tuple(point) + (scale,) for point in points]
    target = list(query) + [scale]
    # 第一阶段要求右端非负；按行乘以符号，最后把对偶证书的符号还原。
    signs = [_sign(value) for value in target]
    right = [signs[i] * target[i] for i in range(m)]
    scaled = [tuple(signs[i] * column[i] for i in range(m)) for column in columns]

    basis = [n + i for i in range(m)]
    basis_set = set(basis)
    seen = set()
    bland = False
    pivots = 0

    while True:
        pivots += 1
        if pivots > max_pivots:
            raise LPError('精确单纯形主元次数超出上限')
        matrix = [[0] * m for _ in range(m)]
        for k, column_index in enumerate(basis):
            if column_index < n:
                column = scaled[column_index]
                for i in range(m):
                    matrix[i][k] = column[i]
            else:
                matrix[column_index - n][k] = 1
        determinant = _det(matrix)
        if determinant == 0:
            raise LPError('单纯形基奇异')
        adj = _adjugate(matrix)
        if determinant < 0:
            determinant = -determinant
            adj = [[-value for value in row] for row in adj]
        artificial_cost = [0 if column_index < n else 1 for column_index in basis]
        dual_numerator = [sum(artificial_cost[i] * adj[i][k] for i in range(m)) for k in range(m)]
        basic_numerator = [sum(adj[i][k] * right[k] for k in range(m)) for i in range(m)]
        phase_one = sum(basic_numerator[i] for i in range(m) if basis[i] >= n)

        if phase_one == 0:
            weights = []
            for i, column_index in enumerate(basis):
                if column_index < n and basic_numerator[i] != 0:
                    if basic_numerator[i] < 0:
                        raise LPError('基本解出现负权重')
                    weights.append((column_index, Fraction(basic_numerator[i], determinant)))
            return ('covered', weights)

        # 选入基变量：先用最大检验数；出现重复基后改用 Bland 规则保证终止。
        entering = None
        best = None
        for j in range(n + m):
            if j in basis_set:
                continue
            if j < n:
                column = scaled[j]
                reduced = sum(dual_numerator[k] * column[k] for k in range(m))
                if reduced <= 0:
                    continue
                score = reduced
            else:
                if dual_numerator[j - n] <= determinant:
                    continue
                score = dual_numerator[j - n] - determinant
            if bland:
                entering = j
                break
            if best is None or score > best:
                best = score
                entering = j
        if entering is None:
            certificate = tuple(Fraction(dual_numerator[k] * signs[k], determinant) for k in range(m))
            return ('separated', certificate)

        entering_column = scaled[entering] if entering < n else tuple(
            1 if i == entering - n else 0 for i in range(m)
        )
        direction = [sum(adj[i][k] * entering_column[k] for k in range(m)) for i in range(m)]
        leaving = None
        best_numerator = None
        best_denominator = None
        for i in range(m):
            if direction[i] <= 0:
                continue
            numerator = basic_numerator[i]
            denominator = direction[i]
            if (
                leaving is None
                or numerator * best_denominator < best_numerator * denominator
                or (numerator * best_denominator == best_numerator * denominator and basis[i] < basis[leaving])
            ):
                leaving = i
                best_numerator = numerator
                best_denominator = denominator
        if leaving is None:
            raise LPError('第一阶段目标无界')
        basis_set.discard(basis[leaving])
        basis[leaving] = entering
        basis_set.add(entering)
        if not bland:
            signature = tuple(basis)
            if signature in seen:
                bland = True
            else:
                seen.add(signature)


def integer_bound(certificate):
    """把对偶证书化为整数系数线性界限 c·x <= C。

    证书分量是有理数；乘以各分母的最小公倍数后得到整数，再约去公因子。
    返回 (coefficients, constant)：系数与常数都是整数，训练点全部满足
    sum(coefficients[k] * x[k]) <= constant，待判点严格超出。
    """
    multiplier = 1
    for value in certificate:
        multiplier = multiplier // math.gcd(multiplier, value.denominator) * value.denominator
    scaled = [int(value * multiplier) for value in certificate]
    coefficients = scaled[:-1]
    constant = -scaled[-1]
    divisor = 0
    for value in coefficients + [constant]:
        divisor = math.gcd(divisor, abs(value))
    if divisor > 1:
        coefficients = [value // divisor for value in coefficients]
        constant //= divisor
    return coefficients, constant


def verify_covered(weights, points, query):
    """精确复核覆盖权重：非负、至多五件、和为一、加权结果逐坐标相等。

    坐标都是同一整数尺度下的整数；weights 元素为 (index, numerator, denominator)。
    """
    if not weights or len(weights) > 5:
        return False
    total = Fraction(0)
    parsed = []
    for item in weights:
        index, numerator, denominator = item
        if not 0 <= index < len(points):
            return False
        if denominator <= 0 or numerator <= 0:
            return False
        weight = Fraction(numerator, denominator)
        total += weight
        parsed.append((index, weight))
    if total != 1:
        return False
    for coordinate in range(len(query)):
        if sum(weight * points[index][coordinate] for index, weight in parsed) != query[coordinate]:
            return False
    return True


def verify_separated(coefficients, constant, points, query, scale):
    """精确复核分离界限：全部训练点不超界，待判点严格超出。

    坐标按同一分母 scale 缩放为整数，界限在原始小数坐标下为
    sum(coefficients[k] * x[k]) <= constant。
    """
    if len(coefficients) != len(query):
        return False
    right = constant * scale
    for point in points:
        if sum(coefficients[k] * point[k] for k in range(len(query))) > right:
            return False
    return sum(coefficients[k] * query[k] for k in range(len(query))) > right
