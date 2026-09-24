"""实验室混样批次追溯 API。

- POST /samples                            登记原始样本
- POST /pools                              创建混样批次（引用已有批次 + 正整数份数）
- GET  /batches                            列出全部批次
- GET  /batches/{batch_id}                 批次详情（混样含配方）
- GET  /batches/{batch_id}/composition     递归计算原始样本占比（约分后的分子/分母）
- POST /batches/{batch_id}/recipe-preview  预演配方变更对下游批次的影响（不修改数据库）
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status

from . import db
from .schemas import (
    AffectedBatchOut,
    BatchOut,
    ComponentOut,
    CompositionOut,
    PoolCreate,
    PreviewComponentOut,
    RecipeItemOut,
    RecipePreviewIn,
    RecipePreviewOut,
    SampleCreate,
)
from .service import batch_composition, recipe_preview


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # 启动时自动建表
    yield


app = FastAPI(
    title="实验室混样批次追溯 API",
    version="1.0.0",
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _fetch_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()


def _batch_out(conn: sqlite3.Connection, row: sqlite3.Row) -> BatchOut:
    recipe = None
    if row["kind"] == "pool":
        recipe = [
            RecipeItemOut(
                batch_id=r["ingredient_id"],
                batch_name=r["name"],
                portions=r["portions"],
            )
            for r in conn.execute(
                """
                SELECT pr.ingredient_id, b.name, pr.portions
                FROM pool_recipes pr
                JOIN batches b ON b.id = pr.ingredient_id
                WHERE pr.pool_id = ?
                ORDER BY pr.ingredient_id
                """,
                (row["id"],),
            )
        ]
    return BatchOut(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        created_at=row["created_at"],
        recipe=recipe,
    )


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/samples",
    response_model=BatchOut,
    status_code=status.HTTP_201_CREATED,
    summary="登记原始样本",
)
def create_sample(payload: SampleCreate, conn: sqlite3.Connection = Depends(get_db)) -> BatchOut:
    try:
        cur = conn.execute(
            "INSERT INTO batches (name, kind) VALUES (?, 'sample')", (payload.name,)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"批次名称已存在: {payload.name}"
        ) from None
    return _batch_out(conn, _fetch_batch(conn, cur.lastrowid))


@app.post(
    "/pools",
    response_model=BatchOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建混样批次",
)
def create_pool(payload: PoolCreate, conn: sqlite3.Connection = Depends(get_db)) -> BatchOut:
    # 同一批次在配方中重复出现时，份数合并
    merged: dict[int, int] = {}
    for item in payload.recipe:
        merged[item.batch_id] = merged.get(item.batch_id, 0) + item.portions

    ids = sorted(merged)
    placeholders = ", ".join("?" for _ in ids)
    found = {
        row["id"]
        for row in conn.execute(f"SELECT id FROM batches WHERE id IN ({placeholders})", ids)
    }
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}"
        )

    try:
        cur = conn.execute(
            "INSERT INTO batches (name, kind) VALUES (?, 'pool')", (payload.name,)
        )
        pool_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO pool_recipes (pool_id, ingredient_id, portions) VALUES (?, ?, ?)",
            [(pool_id, i, merged[i]) for i in ids],
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"批次名称已存在: {payload.name}"
        ) from None
    return _batch_out(conn, _fetch_batch(conn, pool_id))


@app.get("/batches", response_model=list[BatchOut], summary="列出全部批次")
def list_batches(conn: sqlite3.Connection = Depends(get_db)) -> list[BatchOut]:
    rows = conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
    return [_batch_out(conn, row) for row in rows]


@app.get("/batches/{batch_id}", response_model=BatchOut, summary="批次详情")
def get_batch(batch_id: int, conn: sqlite3.Connection = Depends(get_db)) -> BatchOut:
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"批次不存在: {batch_id}")
    return _batch_out(conn, row)


@app.get(
    "/batches/{batch_id}/composition",
    response_model=CompositionOut,
    summary="递归计算原始样本占比",
)
def get_composition(
    batch_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> CompositionOut:
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"批次不存在: {batch_id}")

    composition = batch_composition(conn, batch_id)
    names = {
        r["id"]: r["name"]
        for r in conn.execute(
            "SELECT id, name FROM batches WHERE id IN (%s)"
            % ", ".join("?" for _ in composition),
            list(composition),
        )
    }
    components = [
        ComponentOut(
            sample_id=sample_id,
            sample_name=names[sample_id],
            numerator=frac.numerator,
            denominator=frac.denominator,
        )
        for sample_id, frac in sorted(composition.items())
    ]
    return CompositionOut(
        batch_id=row["id"],
        batch_name=row["name"],
        kind=row["kind"],
        components=components,
    )


@app.post(
    "/batches/{batch_id}/recipe-preview",
    response_model=RecipePreviewOut,
    summary="预演配方变更对下游批次的影响（不修改数据库）",
)
def preview_recipe_change(
    batch_id: int, payload: RecipePreviewIn, conn: sqlite3.Connection = Depends(get_db)
) -> RecipePreviewOut:
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"批次不存在: {batch_id}")
    if row["kind"] != "pool":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"原始样本批次没有配方，无法预演配方变更: {batch_id}",
        )

    # 与创建混样一致：同一批次在配方中重复出现时，份数合并
    merged: dict[int, int] = {}
    for item in payload.recipe:
        merged[item.batch_id] = merged.get(item.batch_id, 0) + item.portions

    ids = sorted(merged)
    placeholders = ", ".join("?" for _ in ids)
    ingredient_names = {
        r["id"]: r["name"]
        for r in conn.execute(
            f"SELECT id, name FROM batches WHERE id IN ({placeholders})", ids
        )
    }
    missing = [i for i in ids if i not in ingredient_names]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}"
        )

    # 原料只能引用比目标批次更早创建的批次（id 随创建顺序递增），否则预演会在配方图中成环
    too_new = [i for i in ids if i >= batch_id]
    if too_new:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"替代配方只能引用比批次 {batch_id} 更早创建的批次: {too_new}",
        )

    affected = recipe_preview(conn, batch_id, merged)

    affected_ids = sorted(affected)
    affected_placeholders = ", ".join("?" for _ in affected_ids)
    batch_rows = {
        r["id"]: r
        for r in conn.execute(
            f"SELECT id, name, kind FROM batches WHERE id IN ({affected_placeholders})",
            affected_ids,
        )
    }
    sample_ids = sorted({sid for comps in affected.values() for sid in comps})
    sample_placeholders = ", ".join("?" for _ in sample_ids)
    sample_names = {
        r["id"]: r["name"]
        for r in conn.execute(
            f"SELECT id, name FROM batches WHERE id IN ({sample_placeholders})", sample_ids
        )
    }

    affected_out = []
    for bid in affected_ids:
        components = []
        for sample_id, (original, preview) in sorted(affected[bid].items()):
            delta = preview - original
            components.append(
                PreviewComponentOut(
                    sample_id=sample_id,
                    sample_name=sample_names[sample_id],
                    original_numerator=original.numerator,
                    original_denominator=original.denominator,
                    preview_numerator=preview.numerator,
                    preview_denominator=preview.denominator,
                    delta_numerator=delta.numerator,
                    delta_denominator=delta.denominator,
                )
            )
        affected_out.append(
            AffectedBatchOut(
                batch_id=bid,
                batch_name=batch_rows[bid]["name"],
                kind=batch_rows[bid]["kind"],
                components=components,
            )
        )

    return RecipePreviewOut(
        batch_id=row["id"],
        batch_name=row["name"],
        recipe=[
            RecipeItemOut(batch_id=i, batch_name=ingredient_names[i], portions=merged[i])
            for i in ids
        ],
        affected=affected_out,
    )
