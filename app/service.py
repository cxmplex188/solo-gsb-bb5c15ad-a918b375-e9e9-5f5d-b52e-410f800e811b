"""混样批次的原始样本占比计算与配方变更预演。

全程使用 fractions.Fraction 做精确有理数运算，避免浮点误差；
Fraction 会自动约分，分子/分母即为最简形式。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from fractions import Fraction

# 配方图：kinds[批次 id] = "sample" | "pool"；recipes[混样 id] = [(原料批次 id, 份数), ...]
Graph = tuple[dict[int, str], dict[int, list[tuple[int, int]]]]


def _load_graph(conn: sqlite3.Connection) -> Graph:
    kinds = {row["id"]: row["kind"] for row in conn.execute("SELECT id, kind FROM batches")}
    recipes: dict[int, list[tuple[int, int]]] = {}
    for row in conn.execute("SELECT pool_id, ingredient_id, portions FROM pool_recipes"):
        recipes.setdefault(row["pool_id"], []).append((row["ingredient_id"], row["portions"]))
    return kinds, recipes


def _expander(
    kinds: dict[int, str], recipes: dict[int, list[tuple[int, int]]]
) -> Callable[[int], dict[int, Fraction]]:
    """基于给定配方图构造递归展开函数（带记忆化）。

    - 每一层按“投入份数 / 该层总份数”分摊占比；
    - 同一原始样本经多条路径到达时，占比相加合并；
    - 配方边一律由后创建的批次指向先创建的批次（id 递减），是有向无环图，递归必然终止。
    """
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

    return expand


def batch_composition(conn: sqlite3.Connection, batch_id: int) -> dict[int, Fraction]:
    """递归展开批次，返回 {原始样本 id: 占比}。"""
    kinds, recipes = _load_graph(conn)
    return _expander(kinds, recipes)(batch_id)


def downstream_batches(conn: sqlite3.Connection, batch_id: int) -> list[int]:
    """沿配方引用关系（原料 → 引用它的混样）找出全部下游批次。

    返回含目标批次自身在内的 id 列表，按 id 升序；多路径汇合的批次只出现一次。
    """
    consumers: dict[int, list[int]] = {}
    for row in conn.execute("SELECT pool_id, ingredient_id FROM pool_recipes"):
        consumers.setdefault(row["ingredient_id"], []).append(row["pool_id"])

    seen = {batch_id}
    stack = [batch_id]
    while stack:
        for consumer in consumers.get(stack.pop(), []):
            if consumer not in seen:
                seen.add(consumer)
                stack.append(consumer)
    return sorted(seen)


def recipe_preview(
    conn: sqlite3.Connection, target_id: int, new_recipe: dict[int, int]
) -> dict[int, dict[int, tuple[Fraction, Fraction]]]:
    """在内存中把 target 的配方替换为 new_recipe（{原料批次 id: 份数}），不修改数据库。

    返回 {受影响批次 id: {原始样本 id: (原占比, 预演占比)}}；
    仅在一侧出现的样本另一侧按 0 计，调用方据此得到有符号差值。
    新配方的原料均比目标批次更早创建（由接口层校验），替换后配方图仍无环。
    """
    kinds, recipes = _load_graph(conn)
    original_expand = _expander(kinds, recipes)

    preview_recipes = dict(recipes)
    preview_recipes[target_id] = sorted(new_recipe.items())
    preview_expand = _expander(kinds, preview_recipes)

    result: dict[int, dict[int, tuple[Fraction, Fraction]]] = {}
    for bid in downstream_batches(conn, target_id):
        original = original_expand(bid)
        preview = preview_expand(bid)
        result[bid] = {
            sample_id: (
                original.get(sample_id, Fraction(0)),
                preview.get(sample_id, Fraction(0)),
            )
            for sample_id in original.keys() | preview.keys()
        }
    return result
