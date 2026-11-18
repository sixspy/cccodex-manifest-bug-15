# 空指纹复现单（manifest-bug-15）

> 本单独立成档，记录“空指纹从页面溜进库、去重扫库吃空串、落库脏数据”的原始缺陷、
> 复现路径与修复后的对照结论。修复前后均可用同一步骤复现/回归。

## 一、缺陷现象

- 页面登记对象时，内容指纹留空或只填空白，后端 `register_object` 不校验直接落库
  （原代码注释 `# silent: empty digest accepted`）。
- `landing_objects.content_hash` 仅 `NOT NULL`，空串 `''` 能绕过，数据库层不设防。
- 去重扫库按 `content_hash` 归并，空串被当成一个“合法指纹”参与去重，污染对账与库存。
- 并发灌数时登记无显式写锁，同键/空串存在双成功风险。

## 二、稳定复现步骤（修复前）

1. 登录 `admin / 123456`，打开任一“收集中”批次。
2. `POST /api/batches/{id}/objects`，body：
   ```json
   {"object_key": "raw/empty.parquet", "content_hash": "   ", "byte_size": 1}
   ```
   修复前返回 `200` 且库内出现 `content_hash=''` 的脏行。
3. 直接查库：
   ```sql
   SELECT id, object_key, content_hash FROM landing_objects WHERE trim(content_hash)='';
   ```
   修复前能查到脏行。
4. 去重复扫把空串当指纹吃进归并，错位/重复统计失真。

复现脚本：`repro_empty_fingerprint.py`（独立目录，用 `python -I` 运行，临时库，不碰业务库）。

```bash
python -I repro_empty_fingerprint.py
```

## 三、修复后的对照结论（同一脚本回归）

| 检查点 | 修复前 | 修复后 |
|--------|--------|--------|
| 空串/纯空白指纹登记 | 200 落库 | 422 白话拦截「内容指纹为空，禁止登记…」 |
| 短于水线（<6 位）指纹 | 落库 | 422 拦截并提示水线 |
| `landing_objects` 空串行 | 可存在 | 表级 `CHECK(length(trim(content_hash))>0)`，插空串即 IntegrityError |
| 历史空串脏行 | 残留 | 迁移自动占位 + 落复检凭证，强制复检 |
| 去重复扫空串 | 吃进归并 | 拦截计数 `empty_blocked_count`，不参与去重、不落库 |
| 并发同键/空串 | 可能双成功 | `BEGIN IMMEDIATE` 写锁，仅 1 条成功、其余 409/422 |

## 四、处置轨（互斥，后端一次裁定）

- **撤回轨 `track=withdraw`**：先撤登记（对象置「已撤回」、指纹标撤回记号），再写
  `withdrawal_flows` 撤回流水，逼同键重填真实指纹。
- **占位复检轨 `track=placeholder`**：原子写 `PLACEHOLDER-PENDING-RECHECK:` 占位指纹，
  状态置「待复检」，同事务落 `recheck_vouchers` 凭证，强制复检通过才恢复「已登记」。
- 两轨互斥：已落待复检凭证则撤回轨 409；已写撤回流水则占位轨 409；重复占位 409。

## 五、四处同查（页面拦截与后台校验同一判定）

1. 登记附页：表单提交前用 `/api/meta.waterline` 同一水线先拦。
2. 校验水线：`app/services/rules.py` 单一判定源，前后端共用。
3. 空串列表（空串治理台）：捞出待处置/待复检修，亮撤回或占位芯片及凭证号。
4. 去重编排台：对照同一凭证号复扫，空串/占位/撤回记号一律拦截计数。

> 半修只改前端水线提示、不改后台校验会直接失败：后台 `validate_fingerprint` 与
> 表级 CHECK 是硬闸，页面判定偏了以后台白话报错停下。
