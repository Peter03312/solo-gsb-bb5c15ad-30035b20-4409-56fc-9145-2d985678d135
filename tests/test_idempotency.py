"""批次幂等键测试。

契约要点：
- 键可选；带键首次成功后持久化键与完整成功结果；
- 同键同内容重试直接回放原结果（200 + Idempotency-Replayed: true），
  即使标注随后变化也不重新校验修订、不重新渲染、不产生第二批版本；
- 同键不同内容 -> 409 idempotency_conflict，原批次不受影响；
- 任一校验/原图读取/成品落盘失败不占用键、不产生部分版本，可原键重试；
- 并发同键重试最多发布一次；
- 不带键的请求行为与旧版本一致（每次都发布新版本）。
"""
import os
import sqlite3
import threading

import pytest
from PIL import Image as PILImage
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.test_api import png_bytes, upload  # noqa: E402,F401  复用构造器与助手


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _annotate(client, doc_id, revision, rects=None):
    rects = rects if rects is not None else [
        {"x": 1, "y": 1, "w": 5, "h": 5, "reason": "敏感"}
    ]
    return client.put(
        f"/documents/{doc_id}/annotations",
        json={"revision": revision, "rects": rects},
    )


def _batch(client, pairs, key=None):
    body = {
        "documents": [
            {"doc_id": d, "expected_revision": r} for d, r in pairs
        ]
    }
    if key is not None:
        body["idempotency_key"] = key
    return client.post("/publication-batches", json=body)


def _db(client):
    settings = client.app.state.settings
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return settings, conn


def _two_docs(client):
    a = upload(client, png_bytes(color=(200, 10, 10))).json()["doc_id"]
    b = upload(client, png_bytes(color=(10, 200, 10))).json()["doc_id"]
    _annotate(client, a, 0)  # a -> revision 1
    return a, b


def _versions(client, doc_id):
    pubs = client.get(f"/documents/{doc_id}/publications").json()["publications"]
    return [p["version"] for p in pubs]


# ---------------- 回放：同键同内容 ----------------

def test_idempotent_retry_replays_original_result(client):
    a, b = _two_docs(client)
    key = "archive-batch-2026-10-05-0001"

    r1 = _batch(client, [(a, 1), (b, 0)], key=key)
    assert r1.status_code == 201, r1.text
    first = r1.json()
    assert r1.headers.get("idempotency-replayed") != "true"

    # 同键同内容重试：回放原结果（同批次标识、同版本、同摘要），不产生第二批
    r2 = _batch(client, [(a, 1), (b, 0)], key=key)
    assert r2.status_code == 200, r2.text
    assert r2.headers["idempotency-replayed"] == "true"
    replay = r2.json()
    assert replay == first

    # 批次查询与公开读取仍只有第一批产物
    got = client.get(f"/publication-batches/{first['batch_id']}")
    assert got.status_code == 200
    assert _versions(client, a) == [1]
    assert _versions(client, b) == [1]
    settings, db = _db(client)
    assert db.execute(
        "SELECT COUNT(*) c FROM publication_batches").fetchone()["c"] == 1
    assert db.execute(
        "SELECT COUNT(*) c FROM batch_idempotency_keys").fetchone()["c"] == 1
    db.close()


def test_replay_does_not_recheck_revisions_after_annotations_change(client):
    a, b = _two_docs(client)
    key = "key-rev-bind"

    first = _batch(client, [(a, 1), (b, 0)], key=key)
    assert first.status_code == 201
    frozen = first.json()

    # 标注随后变化：a 推进到 revision 2
    _annotate(client, a, 1,
              rects=[{"x": 20, "y": 20, "w": 5, "h": 5, "reason": "新标注"}])
    # 用原键、原内容（expected_revision 仍是 1）重试：
    # 不重新校验当前修订号，也不重新渲染，原样回放
    r2 = _batch(client, [(a, 1), (b, 0)], key=key)
    assert r2.status_code == 200
    assert r2.json() == frozen
    assert r2.json()["documents"][0]["annotation_revision"] == 1
    # 没有新版本产生
    assert _versions(client, a) == [1]
    assert _versions(client, b) == [1]
    # 原成品摘要保持不变
    img = client.get(f"/publications/{a}/1/image").content
    import hashlib
    assert hashlib.sha256(img).hexdigest() == frozen["documents"][0]["output_sha256"]


def test_multiple_retries_all_replay_same_result(client):
    a, b = _two_docs(client)
    key = "key-repeat"
    first = _batch(client, [(a, 1), (b, 0)], key=key).json()
    for _ in range(3):
        r = _batch(client, [(a, 1), (b, 0)], key=key)
        assert r.status_code == 200
        assert r.json() == first
    assert _versions(client, a) == [1]


def test_replay_result_body_is_identical_json(client):
    a, b = _two_docs(client)
    key = "key-body"
    r1 = _batch(client, [(a, 1), (b, 0)], key=key)
    r2 = _batch(client, [(a, 1), (b, 0)], key=key)
    assert r2.content == r1.content  # 完整成功结果逐字节回放


# ---------------- 冲突：同键不同内容 ----------------

def test_same_key_different_content_conflicts_and_preserves_original(client):
    a, b = _two_docs(client)
    c = upload(client, png_bytes(color=(10, 10, 200))).json()["doc_id"]
    key = "key-conflict"

    first = _batch(client, [(a, 1), (b, 0)], key=key)
    assert first.status_code == 201
    frozen = first.json()

    # 1) 修订号不同
    r = _batch(client, [(a, 0), (b, 0)], key=key)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "idempotency_conflict"
    assert detail["existing_batch_id"] == frozen["batch_id"]

    # 2) 文档集合不同
    r = _batch(client, [(a, 1), (c, 0)], key=key)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "idempotency_conflict"

    # 3) 顺序不同（键绑定"按顺序排列"的文档 ID）
    r = _batch(client, [(b, 0), (a, 1)], key=key)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "idempotency_conflict"

    # 冲突不影响原批次：仍是唯一批次，仍是 v1
    assert client.get(
        f"/publication-batches/{frozen['batch_id']}").status_code == 200
    assert _versions(client, a) == [1]
    assert _versions(client, b) == [1]
    assert _versions(client, c) == []

    # 原键原内容仍可正常回放
    replay = _batch(client, [(a, 1), (b, 0)], key=key)
    assert replay.status_code == 200
    assert replay.json() == frozen


def test_conflict_does_not_publish_when_stale_check_would_also_fail(client):
    # 同键不同内容即使恰好当前修订也不匹配，仍按冲突处理（先查重后校验）
    a, b = _two_docs(client)
    key = "key-conflict-precedence"
    _batch(client, [(a, 1), (b, 0)], key=key)
    _annotate(client, a, 1)  # a -> revision 2
    # 不同内容（b 的修订不同）且 a 的修订也已过期：冲突优先，不发布
    r = _batch(client, [(a, 2), (b, 1)], key=key)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "idempotency_conflict"
    assert _versions(client, a) == [1]
    assert _versions(client, b) == [1]


# ---------------- 失败不占用键 ----------------

def test_failed_validation_does_not_consume_key(client):
    a, b = _two_docs(client)
    missing = "0" * 32
    key = "key-fail-validation"

    r = _batch(client, [(a, 1), (missing, 0)], key=key)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "document_not_found"

    settings, db = _db(client)
    assert db.execute(
        "SELECT COUNT(*) c FROM batch_idempotency_keys"
        " WHERE idempotency_key=?", (key,)).fetchone()["c"] == 0
    assert db.execute(
        "SELECT COUNT(*) c FROM publication_batches").fetchone()["c"] == 0
    db.close()

    # 原键重试（修正内容后）正常成功，版本从 1 开始
    r = _batch(client, [(a, 1), (b, 0)], key=key)
    assert r.status_code == 201
    assert [d["version"] for d in r.json()["documents"]] == [1, 1]


def test_stale_revision_failure_does_not_consume_key(client):
    a, b = _two_docs(client)
    key = "key-fail-stale"

    r = _batch(client, [(a, 0), (b, 0)], key=key)  # a 当前为 1
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "stale_revision"

    # 键未被占用，同键同内容此时仍会再次校验（而非回放）
    r = _batch(client, [(a, 0), (b, 0)], key=key)
    assert r.status_code == 409

    # 修正后用原键成功
    r = _batch(client, [(a, 1), (b, 0)], key=key)
    assert r.status_code == 201
    # 之后同键同内容才是回放
    assert _batch(client, [(a, 1), (b, 0)], key=key).status_code == 200


def test_original_unreadable_failure_does_not_consume_key(client):
    a, b = _two_docs(client)
    settings, db = _db(client)
    row_b = db.execute(
        "SELECT original_path FROM documents WHERE id=?", (b,)).fetchone()
    db.close()
    os.unlink(row_b["original_path"])

    key = "key-fail-original"
    r = _batch(client, [(a, 1), (b, 0)], key=key)
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "original_unavailable"

    settings, db = _db(client)
    assert db.execute(
        "SELECT COUNT(*) c FROM batch_idempotency_keys"
        " WHERE idempotency_key=?", (key,)).fetchone()["c"] == 0
    db.close()
    # a 的半成品成品也被清理
    assert not os.path.exists(
        os.path.join(settings.publications_dir, a, "v1.png"))

    # 修复原图后用原键重试成功
    PILImage.new("RGB", (40, 30), (10, 200, 10)).save(row_b["original_path"], "PNG")
    r = _batch(client, [(a, 1), (b, 0)], key=key)
    assert r.status_code == 201
    assert [d["version"] for d in r.json()["documents"]] == [1, 1]


def test_artifact_write_failure_does_not_consume_key(client, monkeypatch):
    a, b = _two_docs(client)
    settings, db = _db(client)
    db.close()

    real_save = PILImage.Image.save
    calls = {"n": 0}

    def flaky_save(self, fp, *args, **kwargs):
        if isinstance(fp, str) and "/v" in fp and fp.endswith(".png"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated disk failure")
        return real_save(self, fp, *args, **kwargs)

    monkeypatch.setattr(PILImage.Image, "save", flaky_save)
    key = "key-fail-artifact"
    r = _batch(client, [(a, 1), (b, 0)], key=key)
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "artifact_write_failed"

    _, db = _db(client)
    assert db.execute(
        "SELECT COUNT(*) c FROM batch_idempotency_keys"
        " WHERE idempotency_key=?", (key,)).fetchone()["c"] == 0
    assert db.execute(
        "SELECT COUNT(*) c FROM publication_batches").fetchone()["c"] == 0
    db.close()
    # 磁盘上不残留半成品
    assert not os.path.exists(
        os.path.join(settings.publications_dir, a, "v1.png"))
    assert not os.path.exists(
        os.path.join(settings.publications_dir, b, "v1.png"))


def test_pre_transaction_validation_does_not_consume_key(client):
    a = upload(client, png_bytes()).json()["doc_id"]
    key = "key-fail-shape"
    # 数量不足（进事务前拒绝）
    r = _batch(client, [(a, 0)], key=key)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_batch_size"
    # 重复（进事务前拒绝）
    r = _batch(client, [(a, 0), (a, 0)], key=key)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "duplicate_document_in_batch"
    settings, db = _db(client)
    assert db.execute(
        "SELECT COUNT(*) c FROM batch_idempotency_keys"
        " WHERE idempotency_key=?", (key,)).fetchone()["c"] == 0
    db.close()


# ---------------- 并发：同键最多发布一次 ----------------

def test_concurrent_same_key_retries_publish_once(client):
    n = 4
    ids = [upload(client, png_bytes(color=(c, 10, 10))).json()["doc_id"]
           for c in range(50, 50 + n)]
    pairs = [(d, 0) for d in ids]
    key = "key-concurrent-once"
    responses: list = []
    lock = threading.Lock()

    def fire():
        r = _batch(client, pairs, key=key)
        with lock:
            responses.append(r)

    threads = [threading.Thread(target=fire) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    created = [r for r in responses if r.status_code == 201]
    replayed = [r for r in responses if r.status_code == 200]
    assert len(created) == 1, [(r.status_code, r.text[:200]) for r in responses]
    assert len(replayed) == 7
    first = created[0].json()
    for r in replayed:
        assert r.headers["idempotency-replayed"] == "true"
        assert r.json() == first

    settings, db = _db(client)
    assert db.execute(
        "SELECT COUNT(*) c FROM publication_batches").fetchone()["c"] == 1
    assert db.execute(
        "SELECT COUNT(*) c FROM batch_idempotency_keys").fetchone()["c"] == 1
    for doc_id in ids:
        versions = [row["version"] for row in db.execute(
            "SELECT version FROM publications WHERE doc_id=? ORDER BY version",
            (doc_id,))]
        assert versions == [1]
    db.close()


def test_concurrent_same_key_different_content_one_wins_others_conflict(client):
    a, b = _two_docs(client)
    c = upload(client, png_bytes()).json()["doc_id"]
    key = "key-concurrent-conflict"
    responses: list = []
    lock = threading.Lock()

    # 一半线程发 (a,b)，一半发 (a,c)：只有一种内容能赢，另一种全部 409；
    # 绝不允许两种内容各自发布一批。
    def fire(pairs):
        r = _batch(client, pairs, key=key)
        with lock:
            responses.append((pairs, r))

    threads = ([threading.Thread(target=fire, args=([(a, 1), (b, 0)],))
                for _ in range(4)]
               + [threading.Thread(target=fire, args=([(a, 1), (c, 0)],))
                  for _ in range(4)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    created = [(p, r) for p, r in responses if r.status_code == 201]
    replayed = [(p, r) for p, r in responses if r.status_code == 200]
    conflicts = [(p, r) for p, r in responses if r.status_code == 409]
    assert len(created) == 1
    assert len(conflicts) == 4
    winning_pairs, winning_resp = created[0]
    winning_second_doc = winning_pairs[1][0]
    loser = c if winning_second_doc == b else b
    winner = winning_second_doc

    # 回放的必须是赢的同一种内容
    for p, r in replayed:
        assert p[1][0] == winner
        assert r.json() == winning_resp.json()
    # 冲突的全部是另一种内容
    for p, r in conflicts:
        assert p[1][0] == loser
        assert r.json()["detail"]["code"] == "idempotency_conflict"

    settings, db = _db(client)
    assert db.execute(
        "SELECT COUNT(*) c FROM publication_batches").fetchone()["c"] == 1
    assert db.execute(
        "SELECT COUNT(*) c FROM batch_idempotency_keys").fetchone()["c"] == 1
    assert _versions(client, winner) == [1]
    assert _versions(client, loser) == []
    db.close()


# ---------------- 不带键：完全兼容 ----------------

def test_requests_without_key_remain_unchanged(client):
    a, b = _two_docs(client)
    r1 = _batch(client, [(a, 1), (b, 0)])
    assert r1.status_code == 201
    assert r1.headers.get("idempotency-replayed") is None
    # 再发一次同样的请求（无键）：照常发布第二批版本
    r2 = _batch(client, [(a, 1), (b, 0)])
    assert r2.status_code == 201
    assert r2.headers.get("idempotency-replayed") is None
    assert r2.json()["batch_id"] != r1.json()["batch_id"]
    assert _versions(client, a) == [1, 2]
    assert _versions(client, b) == [1, 2]
    settings, db = _db(client)
    assert db.execute(
        "SELECT COUNT(*) c FROM batch_idempotency_keys").fetchone()["c"] == 0
    db.close()


def test_distinct_keys_publish_distinct_batches(client):
    a, b = _two_docs(client)
    r1 = _batch(client, [(a, 1), (b, 0)], key="key-one")
    r2 = _batch(client, [(a, 1), (b, 0)], key="key-two")
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["batch_id"] != r2.json()["batch_id"]
    assert _versions(client, a) == [1, 2]


def test_idempotency_key_length_bounds(client):
    a, b = _two_docs(client)
    r = _batch(client, [(a, 1), (b, 0)], key="")
    assert r.status_code == 422  # Pydantic min_length
    r = _batch(client, [(a, 1), (b, 0)], key="x" * 129)
    assert r.status_code == 422
    # 合法长度可用
    r = _batch(client, [(a, 1), (b, 0)], key="x" * 128)
    assert r.status_code == 201
