"""混样批次的原始样本占比计算。

全程使用 fractions.Fraction 做精确有理数运算，避免浮点误差；
Fraction 会自动约分，分子/分母即为最简形式。
"""
from __future__ import annotations

import sqlite3
from fractions import Fraction


def batch_composition(conn: sqlite3.Connection, batch_id: int) -> dict[int, Fraction]:
    """递归展开批次，返回 {原始样本 id: 占比}。

    - 每一层按“投入份数 / 该层总份数”分摊占比；
    - 同一原始样本经多条路径到达时，占比相加合并；
    - 新批次只能引用已创建的批次，引用关系天然是有向无环图，递归必然终止。
    """
    kinds = {row["id"]: row["kind"] for row in conn.execute("SELECT id, kind FROM batches")}

    recipes: dict[int, list[tuple[int, int]]] = {}
    for row in conn.execute("SELECT pool_id, ingredient_id, portions FROM pool_recipes"):
        recipes.setdefault(row["pool_id"], []).append((row["ingredient_id"], row["portions"]))

    memo: dict[int, dict[int, Fraction]] = {}

    def expand(bid: int) -> dict[int, Fraction]:
        if bid in memo:
            return memo[bid]
        if kinds[bid] == "sample":
            memo[bid] = {bid: Fraction(1)}
            return memo[bid]
        items = recipes.get(bid, [])
        total = sum(portions for _, portions in items)
        result: dict[int, Fraction] = {}
        for ingredient_id, portions in items:
            share = Fraction(portions, total)
            for sample_id, frac in expand(ingredient_id).items():
                result[sample_id] = result.get(sample_id, Fraction(0)) + share * frac
        memo[bid] = result
        return result

    return expand(batch_id)
