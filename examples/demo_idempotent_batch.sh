#!/usr/bin/env bash
# 幂等批次发布端到端示例：
#   上传 3 份原图 -> 各自标注 -> 带全局唯一幂等键发布
#   -> 同键同内容重试（回放原结果，不产生第二批版本，即使标注已变化）
#   -> 同键不同内容（409 冲突，原批次不受影响）
#   -> 失败不占用键（文档缺失 -> 修正后原键重试成功）
#   -> 并发同键重发（最多发布一次）-> 不带键请求仍每次发布
# 依赖：curl、python3（仅用于解析 JSON、生成示例图）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/idem_doc_a.png")
Image.new("RGB", (64, 48), (160, 90, 30)).save("/tmp/idem_doc_b.png")
Image.new("RGB", (64, 48), (90, 30, 160)).save("/tmp/idem_doc_c.png")
EOF

upload() {  # $1 = 文件路径 -> doc_id
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

echo "== 1. 上传三份原图 =="
A=$(upload /tmp/idem_doc_a.png)
B=$(upload /tmp/idem_doc_b.png)
C=$(upload /tmp/idem_doc_c.png)
echo "A=$A"; echo "B=$B"; echo "C=$C"

echo "== 2. 分别标注（A -> rev1，B -> rev1，C 保持 rev0）=="
curl -sS -X PUT "$BASE/documents/$A/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":4,"y":4,"w":12,"h":10,"reason":"身份证号"}]}' >/dev/null
curl -sS -X PUT "$BASE/documents/$B/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":40,"y":30,"w":10,"h":10,"reason":"人脸"}]}' >/dev/null

# 调用方生成的全局唯一键（建议 UUID）；键与按顺序排列的 (doc_id, expected_revision) 绑定
IDEM_KEY="batch-$(python3 -c 'import uuid;print(uuid.uuid4().hex)')"
echo "幂等键: $IDEM_KEY"

post_keyed() {  # $1=key $2..=文档ID:修订号（按顺序）
  local key="$1"; shift
  local docs
  docs=$(printf '%s\n' "$@" | python3 -c "
import sys,json
items=[]
for line in sys.stdin:
    d,r=line.strip().split(':')
    items.append({'doc_id':d,'expected_revision':int(r)})
print(json.dumps(items))")
  python3 -c "
import json,sys
print(json.dumps({'idempotency_key':sys.argv[1],'documents':json.loads(sys.argv[2])}))" \
    "$key" "$docs"
}

echo "== 3. 首次带键发布（201，完整成功结果被持久化）=="
RESP=$(curl -sS -D /tmp/idem_headers.txt -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "$(post_keyed "$IDEM_KEY" "$A:1" "$B:1" "$C:0")")
echo "$RESP" | python3 -m json.tool
grep -i '^idempotency-replayed' /tmp/idem_headers.txt || echo "(首次请求无 Idempotency-Replayed 头)"
BATCH_ID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['batch_id'])")
echo "首批批次标识: $BATCH_ID"

echo "== 4. 同键同内容重试（网络中断后重发的典型场景）=="
echo "    -> 200 + Idempotency-Replayed: true，原批次标识/版本/摘要逐字节回放"
curl -sS -D - -o /tmp/idem_retry.json -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "$(post_keyed "$IDEM_KEY" "$A:1" "$B:1" "$C:0")" \
  | grep -i -E '^HTTP|^idempotency-replayed'
python3 -c "
import json
first=json.loads('''$RESP''')
retry=json.load(open('/tmp/idem_retry.json'))
assert first == retry, '回放结果必须与首次完全一致'
print('回放结果与首次完全一致；批次标识:', retry['batch_id'])"

echo "== 5. 标注随后变化（A -> rev2），原键原内容再试：仍回放，不校验修订、不重渲染 =="
curl -sS -X PUT "$BASE/documents/$A/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":1,"rects":[{"x":50,"y":40,"w":5,"h":5,"reason":"新标注"}]}' >/dev/null
curl -sS -o /tmp/idem_after_change.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE/publication-batches" -H 'Content-Type: application/json' \
  -d "$(post_keyed "$IDEM_KEY" "$A:1" "$B:1" "$C:0")"
python3 -c "
import json
retry=json.load(open('/tmp/idem_after_change.json'))
assert retry['batch_id']=='$BATCH_ID'
assert retry['documents'][0]['annotation_revision']==1
print('仍回放首批（A 的固化修订号为 1，而非当前的 2），无新版本')"
curl -sS "$BASE/documents/$A" \
  | python3 -c "import sys,json;print('A 现有版本:',[p['version'] for p in json.load(sys.stdin)['publications']])"

echo "== 6. 同键不同内容 -> 409 idempotency_conflict，原批次不受影响 =="
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "$(post_keyed "$IDEM_KEY" "$A:2" "$B:1" "$C:0")"   # A 的预期修订号不同
# 调整顺序、换文档同样冲突；原批次查询仍正常
curl -sS -o /dev/null -w '换序重发: HTTP %{http_code}\n' -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "$(post_keyed "$IDEM_KEY" "$C:0" "$B:1" "$A:1")"
curl -sS -o /dev/null -w "原批次仍可查询: HTTP %{http_code} ($BATCH_ID)\n" \
  "$BASE/publication-batches/$BATCH_ID"

echo "== 7. 失败不占用键：带一个不存在的文档 -> 404，键未被占用 =="
MISSING="00000000000000000000000000000000"
RETRY_KEY="retry-$(python3 -c 'import uuid;print(uuid.uuid4().hex)')"
curl -sS -o /dev/null -w '含缺失文档: HTTP %{http_code}\n' -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "$(post_keyed "$RETRY_KEY" "$A:2" "$MISSING:0")"
echo "    修正后用同一把原键重试 -> 201（之前失败未占版本号）"
curl -sS -o /tmp/idem_recovered.json -w '原键重试: HTTP %{http_code}\n' \
  -X POST "$BASE/publication-batches" -H 'Content-Type: application/json' \
  -d "$(post_keyed "$RETRY_KEY" "$A:2" "$B:1")"
python3 -c "
import json
d=json.load(open('/tmp/idem_recovered.json'))
print('版本:',[x['version'] for x in d['documents']],'批次:',d['batch_id'])"

echo "== 8. 并发同键重发：8 路同时发，恰好 1 个 201、其余 200 回放 =="
RACE_KEY="race-$(python3 -c 'import uuid;print(uuid.uuid4().hex)')"
BODY="$(post_keyed "$RACE_KEY" "$B:1" "$C:0")"
for i in $(seq 1 8); do
  curl -sS -o "/tmp/idem_race_$i.json" -w "%{http_code}\n" \
    -X POST "$BASE/publication-batches" -H 'Content-Type: application/json' \
    -d "$BODY" &
done
wait | sort | uniq -c | sed 's/^/    状态码计数: /'
python3 -c "
import json,glob
bids={json.load(open(f))['batch_id'] for f in glob.glob('/tmp/idem_race_*.json')}
assert len(bids)==1, bids
print('8 路并发只产生一个批次:', bids.pop())"

echo "== 9. 不带键的请求保持旧行为：相同内容再发一次 -> 新批次、新版本 =="
curl -sS -o /tmp/idem_nokey.json -w 'HTTP %{http_code}\n' -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c "
import json
print(json.dumps({'documents':[{'doc_id':'$B','expected_revision':1},{'doc_id':'$C','expected_revision':0}]}))")"
python3 -c "
import json
d=json.load(open('/tmp/idem_nokey.json'))
print('无键新批次:', d['batch_id'], '版本:', [x['version'] for x in d['documents']])"
