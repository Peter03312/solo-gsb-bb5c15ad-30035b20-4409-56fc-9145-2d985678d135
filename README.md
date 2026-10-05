# 档案影像脱敏发布服务

档案员上传 PNG/JPEG 原图，在 **EXIF 旋正后的像素坐标系** 上标注敏感矩形及原因；
发布时把当前标注矩形不可逆涂实为纯黑，生成剥离元数据的独立 PNG，并固化
原图摘要、标注修订号与成品摘要。已发布成品不随后续标注变化而改变；
公开读取只暴露发布版与遮盖信息，永不返回原图。

技术栈：Python 3.11+ · FastAPI · Pillow · SQLite（WAL）

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

- 服务地址：http://localhost:8000
- 交互式 API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

数据持久化在名为 `redaction-data` 的卷中（SQLite 库、原图、发布成品）。

## 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## 配置

| 环境变量        | 默认值   | 说明                                       |
| --------------- | -------- | ------------------------------------------ |
| `DATA_DIR`      | `./data`（容器内 `/data`） | SQLite 库、原图、发布成品的存放根目录 |
| `MAX_UPLOAD_MB` | `25`     | 单个上传文件大小上限（MB）                 |

监听地址/端口通过 uvicorn 参数（`--host`/`--port`）或 Compose 的 `ports` 映射调整。
首次启动时应用自动建目录、建表（初始化无需手工步骤）。

## API 一览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/documents` | 上传原图（multipart 字段名 `file`），返回 `doc_id` 与旋正后宽高 |
| GET  | `/documents/{doc_id}` | 文档元数据与发布列表 |
| GET  | `/documents/{doc_id}/annotations` | 当前标注修订号与矩形 |
| PUT  | `/documents/{doc_id}/annotations` | 整体替换标注，须携带当前修订号 |
| POST | `/documents/{doc_id}/publish` | 按当前标注发布新版本（涂实+去元数据 PNG） |
| GET  | `/documents/{doc_id}/publications` | 发布版本列表 |
| POST | `/publication-batches` | **同批次发布 2～20 份文档**（原子：全成功或整批失败）；请求体可选 `idempotency_key`（全局唯一幂等键） |
| GET  | `/publication-batches/{batch_id}` | 按批次标识查询清单（顺序与请求一致） |
| GET  | `/publications/{doc_id}/{version}` | 公开读取：版本元数据+遮盖坐标与原因 |
| GET  | `/publications/{doc_id}/{version}/image` | 公开读取：发布版 PNG |

## 调用示例（curl）

```bash
BASE=http://localhost:8000

# 1) 上传原图 -> 记下 doc_id、width/height（EXIF 旋正后的坐标系）
DOC=$(curl -s -F "file=@scan.jpg" $BASE/documents)
DOC_ID=$(echo "$DOC" | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])")

# 2) 标注敏感矩形（x,y 为左上角，w/h 为宽高，坐标基于旋正后图像）
curl -s -X PUT $BASE/documents/$DOC_ID/annotations \
  -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":120,"y":80,"w":200,"h":40,"reason":"含身份证号"}]}'
# -> {"doc_id":"...","revision":1,...}

# 2b) 携带过期修订号会被拒绝：HTTP 409 {"detail":{"code":"stale_revision","current_revision":1,...}}

# 3) 发布 -> 固化 annotation_revision、original_sha256、output_sha256
curl -s -X POST $BASE/documents/$DOC_ID/publish
# -> {"version":1,"annotation_revision":1,"output_sha256":"...",...}

# 4) 公开读取（不含原图）
curl -s $BASE/publications/$DOC_ID/1          # 版本元数据 + 遮盖坐标/原因
curl -s -OJ $BASE/publications/$DOC_ID/1/image # 发布版 PNG
```

完整可运行脚本：`examples/demo.sh`（单份流程）、`examples/demo_batch.sh`
（批次发布流程）与 `examples/demo_idempotent_batch.sh`（幂等批次发布流程）；
先启动服务，再执行 `bash examples/demo_idempotent_batch.sh`。

## 同批次发布（2～20 份，整批原子）

档案员把一批**已标注**影像作为同一批次公开。请求携带 2～20 个**不同**文档 ID
及各自**预期标注修订号**：仅当全部文档都存在、且预期修订号仍与服务端当前修订号
匹配时，才在同一写事务内为每份文档按当前标注生成去元数据、纯黑涂实的独立发布版。
任一文档无效、修订过期、原图无法读取或成品写入失败，**整批失败**：不留下任何可
公开的部分版本，不占用任何版本号，也不生成批次标识。批次内渲染与落盘与同时发生
的标注修改、单份发布、其他批次发布互斥串行；已发布版本仍不可变，单份发布与公开
读取的响应及行为保持兼容（批次产物与单份产物共用同一张 `publications` 表、
同一个 `/publications/...` 公开入口）。

```bash
BASE=http://localhost:8000
curl -sS -X POST $BASE/publication-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "documents": [
      {"doc_id": "DOC_ID_1", "expected_revision": 1},
      {"doc_id": "DOC_ID_2", "expected_revision": 1},
      {"doc_id": "DOC_ID_3", "expected_revision": 0}
    ]
  }'
# 担心网络中断丢失响应？在请求体加 "idempotency_key": "<UUID>"，
# 详见下一节「幂等批次发布」。

成功 `201`：返回批次标识、创建时间，以及按**请求顺序**排列的各文档结果
（版本、固化的标注修订号、原图/成品摘要、遮盖数量）：

```json
{
  "batch_id": "7bc8e9008700469fb1c896990b34e620",
  "created_at": "2026-10-04T19:18:21+00:00",
  "count": 3,
  "documents": [
    {"position": 0, "doc_id": "DOC_ID_1", "version": 1,
     "annotation_revision": 1, "original_sha256": "…",
     "output_sha256": "…", "redaction_count": 1, "created_at": "…"},
    {"position": 1, "doc_id": "DOC_ID_2", "version": 1, "…": "…"},
    {"position": 2, "doc_id": "DOC_ID_3", "version": 1, "…": "…"}
  ]
}
```

按批次标识查询（清单顺序与原请求一致）：

```bash
curl -sS $BASE/publication-batches/7bc8e9008700469fb1c896990b34e620
```

每份文档的版本号仍在该文档维度内按 `MAX(version)+1` 连续分配，与单份发布
共用序列（先发过 v1 的文档，在成功批次里拿到 v2）。失败响应在统一错误体外
附带可定位信息：`position`（请求中的下标，从 0 起）、`doc_id`，修订过期时
还附 `current_revision`/`supplied_revision`：

```bash
# HTTP 409
# {"detail":{"code":"stale_revision","message":"…",
#   "position":1,"doc_id":"DOC_ID_2",
#   "current_revision":2,"supplied_revision":1}}
```


## 幂等批次发布（可选 `idempotency_key`）

批量发布网络中断时，成功响应可能丢失；直接重发同一请求又可能生成第二批版本。
为此 `POST /publication-batches` 的请求体支持一个**可选**字段 `idempotency_key`
（长度 1～128 的字符串，建议传 UUID，由调用方保证全局唯一）。

- **键与内容绑定**：键绑定按请求顺序排列的 `(doc_id, expected_revision)` 列表
  （文档 ID、顺序、预期修订号任一不同即视为不同内容，服务端以内容指纹比对）。
- **首次成功**：整批成功时，在同一事务内持久化键与**完整成功结果**
  （批次标识、各文档版本、原图/成品摘要、遮盖数量、创建时间）。响应为 `201`。
- **同键同内容重试**：直接返回原批次标识、各文档版本与摘要，响应为 `200`，
  并带响应头 `Idempotency-Replayed: true`；响应体与首次成功**完全一致**。
  回放**不再校验当前修订号、不重新渲染**——即使标注随后已经变化，返回的仍是
  首次发布时固化的修订号与摘要，也不会产生任何新版本。
- **同键不同内容**：返回 `409 idempotency_conflict`（附 `idempotency_key`、
  `existing_batch_id`），**原批次不受任何影响**；要发布不同内容请换一把新键。
- **失败不占用键**：任一语义校验（数量/重复）、文档存在性、修订号校验、
  原图读取或成品落盘失败，事务整体回滚——键不落库、不产生部分版本、不占版本号，
  调用方可修正后用**同一把原键**重试（此时仍按当前状态正常校验与发布）。
- **并发重试**：键的查重/占位与批次写入在同一个 `BEGIN IMMEDIATE` 事务中，
  SQLite 写锁把并发同键请求串行化：最多只有一个请求真正发布（`201`），
  其余同内容请求全部回放（`200`），不同内容请求全部 `409`。
- **不带键**：不传 `idempotency_key`（或为 `null`）时行为与旧版本完全一致——
  相同请求每次都会发布新批次、新版本；单份发布、批次查询与公开读取契约均无变化。

```bash
BASE=http://localhost:8000
KEY=$(python3 -c 'import uuid;print(uuid.uuid4().hex)')   # 调用方生成全局唯一键

# 首次：201
curl -sS -D - -X POST $BASE/publication-batches \
  -H 'Content-Type: application/json' \
  -d "{
    \"idempotency_key\": \"$KEY\",
    \"documents\": [
      {\"doc_id\": \"DOC_ID_1\", \"expected_revision\": 1},
      {\"doc_id\": \"DOC_ID_2\", \"expected_revision\": 1},
      {\"doc_id\": \"DOC_ID_3\", \"expected_revision\": 0}
    ]
  }"
# HTTP/1.1 201 Created  （无 Idempotency-Replayed 头）

# 网络中断后原样重发同一请求：200 + Idempotency-Replayed: true，
# 批次标识、各文档版本与摘要与首次完全一致，不产生第二批版本
curl -sS -D - -X POST $BASE/publication-batches \
  -H 'Content-Type: application/json' \
  -d "{
    \"idempotency_key\": \"$KEY\",
    \"documents\": [
      {\"doc_id\": \"DOC_ID_1\", \"expected_revision\": 1},
      {\"doc_id\": \"DOC_ID_2\", \"expected_revision\": 1},
      {\"doc_id\": \"DOC_ID_3\", \"expected_revision\": 0}
    ]
  }"
# HTTP/1.1 200 OK
# idempotency-replayed: true

# 同键但改了某份文档的预期修订号（或换文档、换顺序）：409，原批次不变
# {"detail":{"code":"idempotency_conflict","idempotency_key":"…",
#   "existing_batch_id":"…","message":"…"}}
```

调用方处理建议：发布时生成一把键并随请求保存；收到任何响应不确定（超时、断连、
5xx）后，用**同一把键、同一份请求体**重试，直到得到 `201`/`200` 为止——
`200`（回放）与 `201`（首次成功）的响应体可按同一成功结果处理。

## 错误响应

统一格式：`{"detail": {"code": "...", "message": "..."}}`

| HTTP | code | 触发场景 |
| ---- | ---- | -------- |
| 400 | `empty_upload` | 上传空文件 |
| 400 | `image_decode_failed` | 字节无法解码为图像（含截断/损坏） |
| 400 | `empty_rectangle` | 矩形宽或高 ≤ 0 |
| 400 | `rectangle_out_of_bounds` | 矩形越出旋正后图像边界 |
| 400 | `reason_required` | 矩形未填写原因 |
| 404 | `document_not_found` | 文档不存在（批次场景附 `position`/`doc_id`） |
| 404 | `unknown_version` | 发布版本不存在 |
| 404 | `batch_not_found` | 批次标识不存在 |
| 400 | `invalid_batch_size` | 批次文档数不在 2～20（附 `count`） |
| 400 | `duplicate_document_in_batch` | 批次内文档 ID 重复（附 `position`/`doc_id`） |
| 409 | `stale_revision` | 标注修订号过期（响应附 `current_revision`；批次场景另附 `position`/`doc_id`/`supplied_revision`） |
| 409 | `idempotency_conflict` | 同一 `idempotency_key` 对应不同的文档 ID/顺序/预期修订号（附 `idempotency_key`/`existing_batch_id`）；原批次不受影响 |
| 422 | （请求体校验） | `idempotency_key` 长度不在 1～128 |
| 500 | `original_unavailable` | 存储的原图无法读取/解码（批次场景附 `position`/`doc_id`） |
| 500 | `artifact_write_failed` | 发布成品写入磁盘失败（附 `position`/`doc_id`/`version`） |
| 413 | `payload_too_large` | 超过上传大小上限 |
| 415 | `unsupported_media_type` | 非 PNG/JPEG（如 GIF） |

## 一致性设计

- **坐标系**：上传时以 `ImageOps.exif_transpose` 旋正，标注、涂实、发布全部在旋正后
  的像素坐标上进行，与查看器所见方向一致。
- **乐观锁**：`PUT /documents/{id}/annotations` 要求 `revision` 等于服务端当前修订号，
  在 `BEGIN IMMEDIATE` 事务内 check-and-set，过期即 409，不会丢失他人修改。
- **发布事务**：读取当前修订号与矩形、渲染涂实、写盘、插入发布记录在同一写事务中完成；
  与标注修改互斥串行，因此成品文件与记录必然对应同一修订。
- **批次原子性**：`POST /publication-batches` 在**一个** `BEGIN IMMEDIATE` 事务内
  按请求顺序先完成全部"存在性 + 修订号"校验，全部通过后才逐份渲染落盘并登记
  `publications`/`publication_batches`/`batch_publications`；与标注修改、单份发布、
  其他批次发布互斥。任一文档不存在、修订号过期、原图不可读、成品写盘失败，事务整体
  回滚并清理已落盘文件——既不产生批次标识，也没有任何 `publications` 行，因此
  `MAX(version)+1` 不会被失败批次推进（不占版本号、不留部分版本）。成功批次的每份
  版本与单份发布共用 `publications` 序列；批次清单以 `position` 固化请求次序。
- **批次幂等键**：携带 `idempotency_key` 时，键与按序排列的
  `(doc_id, expected_revision)` 内容指纹绑定；键行（`batch_idempotency_keys`，
  含完整成功响应 JSON）与批次及各 `publications` 行在**同一事务**插入，因此只有
  整批成功才占用键。重试在同一 `BEGIN IMMEDIATE` 事务开头先查键：命中且指纹相同则
  原样回放持久化结果（`200` + `Idempotency-Replayed: true`），不再校验当前修订、
  不再渲染落盘；指纹不同返回 `409 idempotency_conflict`，已提交批次不受影响。
  并发同键请求被写锁串行化——最多发布一次；任何校验/原图/落盘失败都随事务回滚，
  键不入库，可用原键重试。不带键的请求不写该表，行为与旧版本完全一致。
- **不可变性**：标注矩形按 `(doc_id, revision)` 追加存储、历史修订永不改写；
  发布版 PNG 落盘后不改动，后续标注变化不影响已发布成品（批次产物同样如此）。
- **脱敏**：矩形区域以纯黑不可逆涂实；发布图重建为干净像素容器后另存为 PNG，
  不携带 EXIF/ICC/文本等任何原图元数据；无标注时也生成去元数据的发布版。
- **公开面**：`/publications/...` 只返回发布版成品、遮盖坐标与原因及摘要值，
  原图没有任何对外读取入口。

## 目录结构

```
app/
  main.py     # FastAPI 路由与事务编排
  db.py       # SQLite 模式与事务助手（WAL + BEGIN IMMEDIATE）
  imaging.py  # 解码、EXIF 旋正、涂实、去元数据渲染
  models.py   # 请求体模型（含批次请求）
  config.py   # 环境变量配置
tests/test_api.py    # 用例：含 EXIF、乐观锁、错误码、不可变性、并发一致性
tests/test_batch.py  # 用例：批次成功/顺序、数量与重复、文档缺失/修订过期/
                     #   原图不可读/写盘失败的整批回滚、版本交错、不可变性、并发
tests/test_idempotency.py  # 用例：幂等键回放（标注变化后仍回放）、同键冲突、
                           #   各类失败不占键、并发同键最多发布一次、无键兼容
examples/demo.sh                # 单份端到端 curl 示例
examples/demo_batch.sh          # 批次发布端到端 curl 示例
examples/demo_idempotent_batch.sh  # 幂等批次发布端到端 curl 示例（含并发重发）
Dockerfile / docker-compose.yml
```

## 本轮变动（批次发布幂等键）

- `POST /publication-batches` 请求体新增**可选**字段 `idempotency_key`
  （1～128 字符串，建议 UUID）：首次成功在同一事务内持久化键、内容指纹与完整
  成功结果；同键同内容重试返回 `200` + 响应头 `Idempotency-Replayed: true`
  并原样回放（不重新校验修订、不重新渲染）；同键不同内容返回
  `409 idempotency_conflict`，原批次不受影响。
- 新增表 `batch_idempotency_keys`（`CREATE TABLE IF NOT EXISTS`，旧数据卷
  启动即自动可用，无需手工迁移）。
- 新增错误码 `idempotency_conflict`（附 `idempotency_key`、`existing_batch_id`）。
- 任一校验、原图读取或成品落盘失败都不占用键、不产生部分版本、不占版本号，
  可用原键重试；并发同键重试在 `BEGIN IMMEDIATE` 下最多发布一次。
- **兼容性**：不带键的请求行为与响应完全不变；既有批次、单份发布与公开读取的
  路由、请求与响应字段均未改动；启动方式（Docker Compose / uvicorn）与配置项
  保持可用。
- 新增 `examples/demo_idempotent_batch.sh` 与 `tests/test_idempotency.py`。
