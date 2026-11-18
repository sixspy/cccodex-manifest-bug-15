# cc-manifest-lab

CC_Codex 二期（26.11）**数据工程**产品线：**湖区落地清单台**（Litestar + Preact CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**湖区落地批次 → 对象指纹登记 → 清单闭合 → 可见性对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数仓与数据湖（落地清单 / 对象登记闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、Litestar、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，侧栏），靛青竖纹 + 亮青强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 湖区落地批次列表（收集中 / 已闭合）
2. 向批次登记对象键与内容指纹
3. 闭合清单（对象数固化，拒绝空批）
4. 可见性对账报告（闭合批 vs 开放批 vs 孤儿对象）

## 空指纹治理（manifest-bug-15）

空指纹曾可经页面登记直接落库、去重扫库吃空串。修复后四层设防，页面拦截与后台校验同一判定：

- **校验水线**（`app/services/rules.py`，单一判定源）：指纹去空白后至少 6 位，空串/短串/占位串前后端同拦；规则经 `GET /api/meta` 的 `waterline` 下发，页面不另写一套。
- **登记硬闸**：服务层 `validate_fingerprint` + 表级 `CHECK(length(trim(content_hash))>0)`，出错回白话（422）。
- **空串处置双轨（互斥，后端一次裁定）**：
  - 撤回轨 `POST /api/empty-objects/{id}/dispose {"track":"withdraw"}`：撤登记 → 写 `withdrawal_flows` 撤回流水 → 逼同键重填。
  - 占位复检轨 `{"track":"placeholder"}`：原子写 `PLACEHOLDER-PENDING-RECHECK:` 占位指纹 → 落 `recheck_vouchers` 凭证 → `POST .../recheck` 强制复检通过才收口。
  - 两轨互斥：已有待复检凭证则撤回 409，已撤回则占位 409，重复占位 409。
- **去重编排台** `POST /api/dedup-runs`：空串/撤回记号/占位指纹一律拦截计数（`empty_blocked_count`），不参与去重、不落库，扫库留痕并对照同一凭证号。
- **并发写锁**：登记/处置走 `BEGIN IMMEDIATE` 单事务，并发灌同键仅 1 条成功，其余 409。
- **旧库迁移**：历史空串行迁移时自动占位并补复检凭证，强制复检。

四处同查：登记附页、校验水线、空串列表（空串治理台）、去重编排台。
复现单与隔离复现脚本见 `bug_repro/`（`python -I bug_repro/repro_empty_fingerprint.py`）。

预留切口（未做，供后续功能题）：清单换版生效窗、闭合作废回开放、批量失败闭互斥、跨区依赖冻结、清单回灌等。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-manifest-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:6900/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **6900**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
