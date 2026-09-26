# Sample Split Service

可追溯、不超分的样本分装库存服务。Python 3.12 / FastAPI 纯后端，SQLite 文件持久化，Docker Compose 一键运行。

## 快速开始

```bash
docker compose up --build      # 服务监听 http://localhost:8000
```

本地开发（Python 3.12+）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # DATABASE_PATH 环境变量可指定 SQLite 文件，默认 data/lab.db
pytest                                 # 运行全部测试（含双连接并发竞争）
```

## 数据模型与不变量

- `tubes`：**当前状态**（余额 `balance_ul`、修订号 `revision`），唯一可变的表。`initial_ul` 为登记/创建时冻结的初始量，由触发器保证不可改写。
- `splits` / `split_children` / `lineage_edges` / `consumptions`：**历史事实**，只插入；数据库触发器拒绝任何 UPDATE/DELETE，历史不可改写。
- `idempotency_keys` / `consumption_idempotency_keys`：请求键 → 规范化正文哈希 + 原始响应，与对应变更同事务写入；分装与耗用各占独立键空间。
- 不变量：任意时刻 `Σ 所有管的余额 + Σ 累计耗用 == Σ 冻结的初始量`（分装只在母管与子管之间搬运微升量，耗用是体积离开台账的唯一出口）。
- 旧库升级：首次启动时为 `tubes` 补 `initial_ul` 列并**恢复一次后冻结**——根管取"现余额＋其直接分装出量之和"（旧库没有耗用），子管取谱系边记录的分装量；绝不把现余额冒充初始量。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/tubes` | 登记初始样本管 `{id, balance_ul}`（正整数微升，上限 2^63-1），修订号从 0 开始、初始量冻结；回执即本次创建提交的状态（余额=登记量、修订号=0），不回读库，不受并发分装影响 |
| GET | `/tubes` | 列出全部管的当前余额、修订号与冻结初始量 |
| GET | `/tubes/{id}` | 单管当前余额、修订号、初始量、母管 id |
| POST | `/splits` | 分装（见下） |
| POST | `/consumptions` | 耗用（见下） |
| GET | `/tubes/{id}/consumptions` | 该管的全部耗用凭证 |
| GET | `/tubes/{id}/conservation` | 以该管为根的子树守恒复核（见下） |
| GET | `/tubes/{id}/ancestry` | 从根到该管的祖先链，每一跳附原始分装记录；整条链在单个读事务快照中读取，各级余额保证来自同一时刻，分装记录中的 `expected_revision` 标明它消费的母管修订号 |
| GET | `/tubes/{id}/splits` | 该管作为母管的全部历史分装记录 |
| GET | `/healthz` | 健康检查 |

### POST /splits

```json
{
  "parent_id": "master-001",
  "expected_revision": 0,
  "request_key": "req-42",
  "children": [{"id": "aliquot-a", "amount_ul": 300}, {"id": "aliquot-b", "amount_ul": 150}]
}
```

- `children` 1–20 支，id 在请求内唯一且不得与任何现存管重复，`amount_ul` 为正整数微升。
- 子管总量不得超过母管当前余额（可以恰好分完，母管余额归 0）。
- **单事务**：母管扣减、子管建立、谱系边、修订号 +1、幂等记录在同一事务提交，失败整体回滚。

### POST /consumptions

```json
{
  "tube_id": "aliquot-a",
  "expected_revision": 0,
  "request_key": "req-77",
  "amount_ul": 120,
  "purpose": "qc-assay"
}
```

- 实验室领走部分样本用于检测后，台账必须解释这部分体积的去向：耗用**不可逆**，扣减的体积不再作为可继续分装的余额。
- `amount_ul` 为正整数微升，不得超过管当前余额（可以恰好耗完，余额归 0）；`purpose` 为非空用途（纯空白视为空）。
- **单事务**：余额扣减、修订号 +1、耗用凭证追加、幂等记录在同一事务提交；余额不足、修订不符或写入异常均整体回滚，不留下半条凭证，失败不占请求键。
- 耗用凭证只插入，触发器拒绝任何 UPDATE/DELETE。

### GET /tubes/{id}/conservation

以该管为根的子树守恒复核：在**单个读快照**中列出全部后代当前余额、全部耗用凭证与冻结初始量，并验证

```
Σ 后代现存余额 + Σ 累计耗用 == 初始量   →   conserved: true
```

对根管而言初始量即登记量；对任意子树同样成立（子管初始量为其创建分装带入的量）。

### 幂等与并发

- 相同 `request_key` + 完全相同正文（JSON 语义相同，键序无关）→ 返回首次的原始结果，不重复扣减。
- 相同 `request_key` + 正文不同 → **409 REQUEST_KEY_CONFLICT**。
- 两个终端同时按同一旧修订号分装/耗用：`BEGIN IMMEDIATE` 在事务入口即取写锁，后到者看到修订号已变 → **412 REVISION_CONFLICT**，最多一笔成功。
- 失败的请求不占用请求键，修正后可用原键重发。

### 错误码

| HTTP | `error.code` | 场景 |
|---|---|---|
| 422 | `VALIDATION_ERROR` | 未知字段、非法量（0/负数/小数/字符串/超出 int64 范围）、子管数越界、请求内子管 id 重复、用途为空等 |
| 422 | `INSUFFICIENT_BALANCE` | 子管总量或耗用量超过母管/样本管余额 |
| 404 | `TUBE_NOT_FOUND` / `PARENT_NOT_FOUND` | 管不存在 |
| 409 | `TUBE_ALREADY_EXISTS` | 重复登记同一管 id |
| 409 | `CHILD_ID_EXISTS` | 子管 id 已被占用 |
| 409 | `REQUEST_KEY_CONFLICT` | 请求键被不同正文复用 |
| 412 | `REVISION_CONFLICT` | `expected_revision` 与当前修订号不符 |

错误体统一为 `{"error": {"code", "message", ...}}`。

## 测试

`pytest` 覆盖：登记与校验（含超出 int64 范围的体积返回 422 且不落库）、分装规则（余额不足/修订冲突/子管冲突各自不同的错误码）、幂等重试（含 JSON 键序打乱的重放）、**两个独立连接经真实 uvicorn 服务竞争同一母管**（2 并发与 8 并发均恰一笔成功）、同键并发重放只执行一次、重启后状态/幂等/谱系保持、逐层分装后的总量守恒、历史表触发器拒绝改写。

耗用与守恒复核（`tests/test_consumption.py`）：基本耗用与校验（余额不足/旧修订/空用途/越界量）、同键重放与异正文 409、失败不占键、逐层分装后各层耗用的守恒复核、耗用与分装竞争同一旧修订恰一笔成功、耗用凭证与冻结初始量的触发器保护、**写入异常整体回滚不留半条凭证**（库级故障触发器注入）、重启后凭证/幂等/复核保持、**旧库升级**（恢复并冻结初始量、旧幂等键与谱系原样可用、升级后继续分装与耗用仍守恒、重复启动不重复回填）、复核查询的单快照性（语句闸门交错验证）。

`tests/test_acceptance.py` 用语句闸门（`gated_server`，在真实 uvicorn + SQLite 文件上把指定 SQL 停在请求中途）确定性交错：登记 INSERT 提交后、响应生成前插入一笔分装，回执仍为修订号 0 与全额；谱系查询读到后代后依次分装后代与各级祖先，返回链仍是查询开始时的同一快照，守恒可复算、`expected_revision` 可对版，交错请求的重放返回首次结果。

## 项目结构

```
app/
  main.py      # 路由、事务编排、异常映射（create_app 工厂）
  db.py        # 连接、建表、不可变触发器
  schemas.py   # Pydantic 请求模型（extra=forbid，严格正整数）
  errors.py    # ApiError
tests/         # pytest 套件
Dockerfile     # python:3.12-slim
docker-compose.yml
```
