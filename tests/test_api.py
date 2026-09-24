"""API 端到端测试：每个用例使用独立的临时 SQLite 数据库。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_sample(client, name):
    r = client.post("/samples", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_pool(client, name, recipe):
    r = client.post("/pools", json={"name": name, "recipe": recipe})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def fractions_of(client, batch_id):
    r = client.get(f"/batches/{batch_id}/composition")
    assert r.status_code == 200, r.text
    return {
        c["sample_id"]: (c["numerator"], c["denominator"])
        for c in r.json()["components"]
    }


def test_single_sample_composition(client):
    s1 = make_sample(client, "S1")
    assert fractions_of(client, s1) == {s1: (1, 1)}


def test_pool_composition_is_reduced(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # 2:2 投入，结果应约分为 1/2 + 1/2
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 2},
        {"batch_id": s2, "portions": 2},
    ])
    assert fractions_of(client, p1) == {s1: (1, 2), s2: (1, 2)}


def test_multilevel_same_source_merged(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    # P2 = 1×P1 + 1×S1：S1 经两条路径到达，须合并为 1/2·1/2 + 1/2 = 3/4
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    assert fractions_of(client, p2) == {s1: (3, 4), s2: (1, 4)}


def test_deep_chain(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    # P3 = 3×P2 + 1×S2 → S1 = 3/4·3/4 = 9/16，S2 = 3/4·1/4 + 1/4 = 7/16
    p3 = make_pool(client, "P3", [
        {"batch_id": p2, "portions": 3},
        {"batch_id": s2, "portions": 1},
    ])
    assert fractions_of(client, p3) == {s1: (9, 16), s2: (7, 16)}


def test_duplicate_recipe_entries_merged(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s1, "portions": 2},
        {"batch_id": s2, "portions": 1},
    ]})
    assert r.status_code == 201
    body = r.json()
    assert body["recipe"] == [
        {"batch_id": s1, "batch_name": "S1", "portions": 3},
        {"batch_id": s2, "batch_name": "S2", "portions": 1},
    ]
    assert fractions_of(client, body["id"]) == {s1: (3, 4), s2: (1, 4)}


def test_empty_recipe_rejected(client):
    r = client.post("/pools", json={"name": "P1", "recipe": []})
    assert r.status_code == 422


def test_unknown_batch_rejected(client):
    s1 = make_sample(client, "S1")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1, "portions": 1},
        {"batch_id": 999, "portions": 1},
    ]})
    assert r.status_code == 400
    assert "999" in r.json()["detail"]


@pytest.mark.parametrize("portions", [0, -3, 1.5, 2.0, "2", True])
def test_non_positive_or_non_integer_portions_rejected(client, portions):
    s1 = make_sample(client, "S1")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1, "portions": portions},
    ]})
    assert r.status_code == 422


def test_query_unknown_batch_returns_404(client):
    assert client.get("/batches/4242/composition").status_code == 404
    assert client.get("/batches/4242").status_code == 404


def test_duplicate_name_rejected(client):
    make_sample(client, "S1")
    r = client.post("/samples", json={"name": "S1"})
    assert r.status_code == 409


def test_pool_can_only_reference_existing_batches(client):
    # 引用尚未创建的批次（未来的 id）同样被拒绝
    s1 = make_sample(client, "S1")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1 + 100, "portions": 1},
    ]})
    assert r.status_code == 400
