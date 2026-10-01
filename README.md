# 博物馆展柜温度暴露审定服务

对博物馆展柜在指定**审定窗口**内采集到的温度读数进行暴露审定：按时间排序后，
以“每条读数只覆盖到下一条读数”的采样保持语义切分区段，识别 **正常 / 超限 / 未知**
区段，计算**最长连续超限时长**，并给出审定结论；支持读数幂等提交、修订替换、
**采样仪器按审定窗口批量补传（整批事务、互换时刻、重放不产生新修订）**、
**误采作废与恢复（作废留存修订：原因/操作时间/前一版本；更高修订号恢复）**、
证书版本化签发（快照不可变），支持馆员对窗口四项审定规则做
**版本化修订**（初始规则为第 1 版，连续修订号 + 非空原因，立即重算、
既有证书快照不变），并可对同一审定窗口的两张已签发证书生成
**修订影响报告**（只读快照比对：规则四项前后值、读数新增/变更/移除、
时间线状态变化半开区间、结论与最长连续超限时长变化）。

- 技术栈：Python 3.11 · FastAPI · SQLite（WAL 模式）· Pydantic v2
- 无外部服务依赖，`docker compose up -d` 一键启动并自动初始化数据库

---

## 1. 审定规则

设审定窗口为 `[start_at, end_at]`，允许温度闭区间 `[min, max]`，
最长连续超限时长 `T_exceed`，最大采样间隔 `T_gap`。

### 区段语义

1. 读数按时间戳升序排列。第 `i` 条读数覆盖半开区间 `[t_i, t_{i+1})`，
   区间内温度取读数 `i` 的温度（采样保持，直到下一条读数）。
2. 相邻读数间隔 **严格大于** `T_gap` 时，该区间整段记为 **未知**；
   恰好等于 `T_gap` 仍算有效。
3. 窗口起点到首条读数、末条读数之后到窗口终点这两段**边界无读数**区域，记为 **未知**。
4. 有效区间内：温度超出 `[min, max]`（等于边界不算超限）记为 **超限**，否则 **正常**。
5. 相邻且状态相同的区段会合并输出。

> **关于窗口终点读数**：读数提交的窗口是闭区间 `[start_at, end_at]`，
> 允许恰好在 `end_at` 时刻提交一条读数，它只作为“覆盖终止采样”
> （结束上一条读数的覆盖，自身区段时长为 0）。这样当首读在起点、
> 末读在终点、所有采样间隔合法且温度全部在区间内时，可以得到没有
> 未知区段的“合格”结论；输出的温度区段始终是半开 `[start_at, end_at)`。

### 结论判定（按优先级）

| 条件 | 结论 | `verdict` |
|---|---|---|
| 最长连续超限时长 **严格大于** `T_exceed` | 不合格 | `fail` |
| 未超限（或恰好等于限值），但存在未知区段 | 待定 | `pending` |
| 无超限超时、无未知区段 | 合格 | `pass` |

“最长连续超限”指相邻的超限区段时长之和，被正常/未知区段打断后重新累计。
即使存在未知区段，只要超限超时，结论仍为不合格。

### 读数提交、幂等与修订

读数以 `(reading_id, revision)` 标识，同一窗口内：

- **相同 ID + 相同修订号重复提交**：内容（时间戳、温度）完全一致 → 幂等返回
  （`effect: idempotent`）；内容不一致 → **409 内容冲突**。
- **更高修订号**：替换该 ID 的当前读数并立即重算（`effect: updated`），
  旧修订号在历史表中标记为 `superseded`。
- **旧修订号**（低于当前修订号）→ **409 拒绝**。
- 同一窗口内不同读数不允许同一时间戳 → **422 拒收**。
- 时间戳超出 `[start_at, end_at]` → **422 拒收**；所有时间戳必须带时区。

### 批量补传（采样仪器）

`POST /api/v1/windows/{id}/readings/batch` 在**单个写事务**内整批提交一个
**非空**读数批次（请求体 `{"readings": [...]}`，单项结构与单条提交完全相同）：

- 每项沿用现有 `reading_id`、`revision`、`measured_at`、`temperature`，
  以及单条提交的时区、窗口闭区间边界、幂等/修订/冲突语义；
  **同批不得重复 `reading_id`**（否则 **422**）。
- 同刻唯一按**批次应用后的最终状态**校验，而不是按写入顺序逐条校验：
  因此允许两条读数都以更高修订号**互换采样时刻**（`a`、`b` 互相改到对方
  原时刻），不会因中间状态误拒；最终状态下仍同刻则 **422 拒收**。
- 任一项冲突（**409**：同 ID+修订号内容冲突、旧修订号覆盖）或无效
  （**422**：窗口外、缺时区、最终状态同刻、批内重复 ID 等）→
  **整批拒收**：活动读数与修订历史均不改变（沿用现有冲突与校验错误语义）。
- 成功时**一次提交全部变化**，响应逐项给出 `created` / `updated` /
  `idempotent` 及最终读数（顺序与请求一致），并返回**按最终状态一次性重算**
  的审定结果 `current_evaluation`；**同批重放不产生新修订**（全部
  `idempotent`，历史表不新增行）。
- 批量补传与单条提交、证书签发互不影响：已签发证书快照不可变，补传后
  再次签发形成新版本，旧版本内容保持不变。

### 误采作废与恢复

馆员确认某条记录为误采时，
`POST /api/v1/windows/{id}/readings/{reading_id}/void`
凭**递增修订号 + 非空原因**作废该 ID 的当前读数：

- 请求体 `{"revision": <高于当前修订号>, "reason": "<非空原因>"}`；
  作废本身作为该 ID 的一条修订（`status=voided`）**留存原因、操作时间
  （`voided_at`）及前一版本**（被作废读数的修订号/时刻/温度随作废行保存，
  响应 `void.previous` 返回），可经
  `GET /api/v1/windows/{id}/readings/{reading_id}/revisions` 查阅全部修订历史。
- 作废生效后当前审定**立即剔除该读数并在同一事务内重算**（响应含
  `current_evaluation`）；该读数不再出现在活动读数列表，其原时刻随之释放，
  可被其它读数使用。
- **幂等与冲突**（沿用现有 409 语义）：
  - 相同作废修订号 + 相同原因重放 → 幂等（`effect: idempotent`，不产生新修订）；
  - 相同作废修订号但原因不同 → **409**；作废修订号不高于当前修订号 → **409**；
  - 读数从未存在 → **404**；已作废的读数不能再次以新修订号作废 → **409**；
  - 原因为空或只含空白 → **422**。
- **恢复**：对该 ID 单条或批量提交**更高修订号**的读数即恢复
  （`effect: restored`），恢复同样遵守窗口闭区间边界与（批量）最终状态
  同刻唯一约束；**旧版提交不得复活**——不高于作废修订号的提交一律 **409**，
  批量中任一项冲突仍整批回滚（作废状态不变）。
- 作废/恢复与证书：已签发证书快照保持原样；作废或恢复后再次签发形成
  新版本，修订影响报告将该读数呈现为**移除（removed）**或**再出现（added）**。

### 窗口规则版本化修订

馆员可修订审定窗口的四项规则（允许温度下/上限、最长连续超限时长、
最大采样间隔）。**窗口的初始规则视为第 1 版**（建窗时自动留痕，
`before` 为 `null`，原因记为“窗口初始规则（第 1 版）”），通过
`POST /api/v1/windows/{id}/rule-revisions` 提交**窗口标识、目标规则修订号、
四项完整新值与非空原因**：

```json
{
  "revision": 2,
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 19.5,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600,
  "reason": "换季：收窄允许温度带"
}
```

- **生效**：`revision` 必须等于“当前版 + 1”（必须连续、不得跳号）。
  成功后窗口当前规则立即更新，**窗口起止时间与读数保持不变**，并在同一
  事务内**按当前活动读数立即重算**（响应含 `current_evaluation`）；
  修订留存**前/后四项规则值、原因与操作时间**（`revised_at`），
  `GET /api/v1/windows/{id}/rule-revisions` 可查全部规则版本（升序）。
- **幂等重放**：`revision` 等于当前版，且**四项规则值与原因均一致** →
  幂等返回（`effect: idempotent`），不留新记录。
- **拒绝（均不留新记录）**：
  - 同号但四项值任一不一致、或原因不一致 → **409 同号冲突**；
  - `revision` 小于当前版（旧号）→ **409**；
  - `revision` 大于“当前版 + 1”（跳号）→ **409**；
  - 数值非有限（`NaN`/`Infinity`）、温度下限大于上限、
    两个时长 ≤ 0、原因为空或只含空白 → **422**；
  - 窗口不存在 → **404**。
- **与签发/证书的关系**：修订前已签发证书的**快照原样保留**（快照内
  `window` 带 `rule_revision`，旧快照按第 1 版解读）；**后续签发采用
  新规则**形成新版本。窗口详情与列表新增 `rule_revision` 字段
  （当前规则修订号；既有窗口、读数、证书接口字段含义不变，仅新增字段
  与新端点）。
- 规则版本化上线前创建的旧库窗口，服务启动时自动按其当前规则**回填
  第 1 版**，可直接提交第 2 版；重复启动不会重复回填。

### 签发与并发

- 签发把**当前已提交状态**（窗口参数 + 全部活动读数 + 审定结果）固化为不可变快照，
  形成一个证书版本（`version` 从 1 递增）。
- 读数修订后再次签发产生**新版本**；旧版本证书与快照永久保留、内容不变。
- 数据库开启 **WAL**，写事务使用 `BEGIN IMMEDIATE` + `busy_timeout`：
  签发（读）与修订（写）并发时，以事务为边界取同一已提交状态——
  修订先提交则纳入本次签发，否则等签发完成后再生效，不会出现脏读或快照撕裂。

---

## 2. 启动

### Docker Compose（推荐，一键启动）

```bash
docker compose up -d --build
docker compose logs -f          # 查看日志
docker compose down             # 停止
```

启动后自动建表初始化（幂等）。SQLite 数据保存在命名卷 `audit-data`
（容器内 `/data/audit.db`），容器重建不丢数据。

健康检查：

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### 本地直接运行（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUDIT_DB_PATH` | `./audit.db` | SQLite 数据库文件路径；Compose 中为 `/data/audit.db` |
| `AUDIT_DB_TIMEOUT` | `10` | 写锁等待秒数（对应 SQLite `busy_timeout` 毫秒数 ×1000） |
| `LOG_LEVEL` | `info` | 日志级别（透传给运行环境） |

### 地址与端口

- 服务地址：`http://localhost:8000`（Compose 映射 `8000:8000`，可在
  `docker-compose.yml` 的 `ports` 修改宿主机端口）
- Swagger 交互文档：<http://localhost:8000/docs>
- OpenAPI Schema：<http://localhost:8000/openapi.json>
- 健康检查：`GET /health`

---

## 3. HTTP API

所有请求/响应均为 JSON；时间戳必须是带时区的 ISO-8601（如
`2026-10-01T00:00:00Z` 或 `2026-10-01T08:00:00+08:00`，统一按时刻处理并以 UTC 输出）。

### 窗口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/windows` | 创建审定窗口（窗口倒置/温度区间倒置返回 422）；初始规则自动记为第 1 版 |
| `GET` | `/api/v1/windows?limit=&offset=` | 窗口列表（分页） |
| `GET` | `/api/v1/windows/{id}` | 窗口详情：当前规则修订号 + 活动读数 + 当前审定 + 最新证书 |
| `GET` | `/api/v1/windows/{id}/evaluation` | 只看当前审定结果 |
| `POST` | `/api/v1/windows/{id}/rule-revisions` | 规则版本化修订：目标修订号（当前版+1 生效）+ 四项完整新值 + 非空原因，响应含立即重算结果；同号同值同原因重放幂等 |
| `GET` | `/api/v1/windows/{id}/rule-revisions` | 该窗口全部规则修订历史（第 1 版为初始规则，升序） |

创建窗口请求体：

```json
{
  "name": "展柜A-2026-10-01",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T01:00:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600
}
```

### 读数

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/windows/{id}/readings` | 提交/幂等重放/修订一条读数，响应含重算结果；对已作废 ID 提交更高修订号即恢复（`effect: restored`） |
| `POST` | `/api/v1/windows/{id}/readings/batch` | 采样仪器补传：整批提交非空读数批次（整批事务），逐项返回 effect 与最终读数，响应含按最终状态重算的结果；批内可含恢复项 |
| `POST` | `/api/v1/windows/{id}/readings/{reading_id}/void` | 误采作废：凭递增修订号 + 非空原因作废当前读数，作废修订留存（原因/操作时间/前一版本），响应含剔除后重算结果 |
| `GET` | `/api/v1/windows/{id}/readings` | 当前活动读数列表（按时间升序；已作废读数不在其中） |
| `GET` | `/api/v1/windows/{id}/readings/{reading_id}/revisions` | 该读数 ID 的全部修订历史（含作废记录：原因/操作时间/前一版本修订号），按修订号升序；从未存在返回 404 |

读数请求体：

```json
{ "reading_id": "r-01", "revision": 1,
  "measured_at": "2026-10-01T00:10:00Z", "temperature": 22.4 }
```

单条提交响应中的 `effect` 为 `created` / `updated` / `restored` / `idempotent`
（`restored` 表示以更高修订号恢复了已作废的读数）。

批量补传请求体与响应：

```json
// 请求：{ "readings": [ <读数>, <读数>, ... ] }（至少 1 项，同批 reading_id 唯一）
{
  "results": [
    { "effect": "created", "reading": { "reading_id": "a", "revision": 1, "...": "..." } },
    { "effect": "updated",   "reading": { "reading_id": "b", "revision": 2, "...": "..." } },
    { "effect": "restored",  "reading": { "reading_id": "c", "revision": 3, "...": "..." } },
    { "effect": "idempotent","reading": { "reading_id": "d", "revision": 1, "...": "..." } }
  ],
  "current_evaluation": { "verdict": "pass", "...": "..." }
}
```

误采作废请求体与响应：

```json
// 请求：POST /api/v1/windows/1/readings/r-03/void
// { "revision": 2, "reason": "误采：该时刻传感器未校准" }
{
  "effect": "voided",
  "void": {
    "reading_id": "r-03", "revision": 2, "status": "voided",
    "reason": "误采：该时刻传感器未校准",
    "voided_at": "2026-10-01T08:00:00.000Z",
    "previous": { "revision": 1, "measured_at": "2026-10-01T00:30:00Z", "temperature": 20.5 }
  },
  "current_evaluation": { "verdict": "pass", "...": "..." }
}
```

### 审定结果结构

```json
{
  "verdict": "fail",
  "normal_segments":   [{"start":"...","end":"...","status":"normal",  "duration_seconds":60.0}],
  "exceeded_segments": [{"start":"...","end":"...","status":"exceeded","duration_seconds":420.0}],
  "unknown_segments":  [{"start":"...","end":"...","status":"unknown", "duration_seconds":3120.0}],
  "max_continuous_exceeded_seconds": 420.0,
  "max_exceed_seconds": 300.0,
  "has_exceeded": true,
  "has_unknown": true,
  "reading_count": 5
}
```

### 签发证书

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/windows/{id}/certificates/issue` | 签发（可带 `{"note":"..."}`，也可空 body） |
| `GET` | `/api/v1/windows/{id}/certificates` | 该窗口全部证书版本（按版本升序） |
| `GET` | `/api/v1/certificates/{id}` | 按证书主键获取，`snapshot` 为签发时不可变快照 |
| `GET` | `/api/v1/certificates/revision-report?old_certificate_id=&new_certificate_id=` | 对同一窗口两张已签发证书生成修订影响报告 |

证书含 `id`、`window_id`、`version`、`verdict`、`verdict_label_zh`
（合格/待定/不合格）、`issued_at`、`note`、`snapshot`。`snapshot.window`
带签发时的规则修订号 `rule_revision`（规则版本化之前的旧证书按第 1 版解读）。

### 修订影响报告

输入**同一审定窗口**的两张**已签发**证书主键（旧版在前、新版在后），服务只读取
两证各自的**不可变快照**，不读取当前活动读数，输出：

- `rules_diff`：两证快照各自的规则修订号（`rule_revision_before/after`，
  规则版本化之前签发的旧快照按第 1 版计）与**四项规则的前后值**——
  `before`/`after` 各含规则修订号与四项规则值，`changed_fields` 为
  `allowed_temp_min` / `allowed_temp_max` /
  `max_continuous_exceed_seconds` / `max_sample_interval_seconds` 的子集，
  `changes` 逐项给出中文名、前后值、增量 `delta` 与单位（°C / s），
  `rules_changed` 标识是否有规则值变化。**即使审定结论未变，跨规则版本
  的规则前后值也在此明确可见。**
- `readings_diff`：按 `reading_id` 对齐两版快照读数。
  - `added`：新版新增读数（含 `revision`/`measured_at`/`temperature`）；
  - `changed`：两版都存在但读数有变化的项，含 `before`/`after` 全量值与
    `changed_fields`（`revision` / `measured_at` / `temperature` 子集，
    采样时间按时刻比较，时区表示不同不视为变化）；
  - `removed`：新版消失的读数（两证签发之间该读数被误采作废；恢复后再次
    签发则体现为 `added`）；
- `timeline.changes`：**分别用两版快照各自的窗口参数与读数**重建半开覆盖
  区段（与第 1 节同一条采样保持/间隔超限/边界空白规则；跨规则版本时
  两证各按各的温度区间与时长限值判定），取两版**全部区段边界
  的并集**切分后，只保留**状态发生变化**的半开区间，每个区间给出：
  - `start`/`end`/`duration_seconds`（半开 `[start, end)`）；
  - `before`/`after`：变化前后 `status`（`normal`/`exceeded`/`unknown`）
    及该侧证据 `evidence`——
    - 已知区段：`{"reading_id", "revision"}`（采样保持该区段的读数）；
    - 未知区段含 `unknown_reason`：`sample_gap`（相邻读数间隔**严格超过**
      最大采样间隔，附 `left_*`/`right_*` 间隔两侧读数 ID 与修订号）或
      `boundary_gap`（边界无读数空白，附 `boundary_side`：`leading` 首读
      之前并给 `right_*` 首条读数、`trailing` 末读之后并给 `left_*` 末条
      读数、`empty` 整窗无读数两侧均无引用）。
  - 相邻且变化前后**状态与证据均相同**的区间自动合并（被无变化区间隔开的
    不合并）。
- `summary`：审定结论变化（`verdict_before/after`、中文标签、
  `verdict_direction` 改善/恶化/持平）、最长连续超限时长前后值与增量
  `max_continuous_exceeded_seconds_delta`、时间线变化区间数，以及一段
  中文汇总结论 `conclusion_zh`（其中单独一行说明**规则版本与四项规则
  前后值变化**；规则值无变化时也会注明两证规则版本与“四项规则值无变化”）。

> 时间线只输出**状态**变化；若未知原因由“边界空白”变为“采样间隔超限”
> （或反之）但两侧都仍为 `unknown`，该区间状态未变、不进入 `timeline.changes`，
> 但该区间在两版快照中对应的未知来源可经上面的证据规则重建。

请求与拒绝约定：

- 查询参数 `old_certificate_id`、`new_certificate_id` 均为证书主键、必填正整数；
- 任一证书不存在 → `404`；
- 两 ID 相同、两证不属于同一窗口、或旧版 `version` 不严格小于新版
  （版本未按先后排列）→ `422`，响应 `detail` 说明具体原因；
- 报告是只读 GET，不改变任何既有内容；既有证书结构、查询响应与审定规则
  保持不变（仅新增端点与响应模型；报告新增 `rules_diff` 字段，
  `summary.conclusion_zh` 增加一行规则版本说明，证书快照 `window`
  新增 `rule_revision`，窗口响应新增 `rule_revision`，均为增量字段）。

报告响应示例（fail → pass 整改）：

```json
{
  "window": {"id": 1, "rule_revision": 2, "...": "..."},
  "old_certificate": {"id": 1, "window_id": 1, "version": 1, "verdict": "fail", "...": "..."},
  "new_certificate": {"id": 2, "window_id": 1, "version": 2, "verdict": "pass", "...": "..."},
  "rules_diff": {
    "rule_revision_before": 1, "rule_revision_after": 2,
    "rules_changed": true, "changed_fields": ["allowed_temp_max"],
    "before": {"rule_revision": 1, "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
               "max_continuous_exceed_seconds": 300.0, "max_sample_interval_seconds": 600.0},
    "after":  {"rule_revision": 2, "allowed_temp_min": 18.0, "allowed_temp_max": 19.5,
               "max_continuous_exceed_seconds": 300.0, "max_sample_interval_seconds": 600.0},
    "changes": [{"field": "allowed_temp_max", "label_zh": "允许温度上限",
                 "before": 22.0, "after": 19.5, "delta": -2.5, "unit_zh": "°C"}]
  },
  "readings_diff": {
    "added": [], "removed": [],
    "changed": [{
      "reading_id": "b",
      "before": {"reading_id": "b", "revision": 1, "measured_at": "2026-10-01T00:01:00Z", "temperature": 23.0},
      "after":  {"reading_id": "b", "revision": 2, "measured_at": "2026-10-01T00:01:00Z", "temperature": 20.0},
      "changed_fields": ["revision", "temperature"]
    }],
    "added_count": 0, "removed_count": 0, "changed_count": 2
  },
  "timeline": {
    "max_sample_interval_seconds": 600.0,
    "change_count": 2,
    "changes": [{
      "start": "2026-10-01T00:01:00Z", "end": "2026-10-01T00:07:00Z",
      "duration_seconds": 360.0,
      "before": {"status": "exceeded", "evidence": {"reading_id": "b", "revision": 1}},
      "after":  {"status": "normal",   "evidence": {"reading_id": "b", "revision": 2}}
    }]
  },
  "summary": {
    "verdict_before": "fail", "verdict_after": "pass",
    "verdict_direction": "改善", "verdict_changed": true,
    "max_continuous_exceeded_seconds_before": 420.0,
    "max_continuous_exceeded_seconds_after": 0.0,
    "max_continuous_exceeded_seconds_delta": -420.0,
    "max_exceed_seconds_limit": 300.0,
    "timeline_change_count": 2,
    "conclusion_zh": "审定结论：不合格（fail）→ 合格（pass），改善。最长连续超限时长：420s → 0s（减少 420s；限值 300s）。……"
  }
}
```

### 错误约定

- `404`：窗口/证书不存在（修订影响报告中任一证书缺失）；
  作废或查询修订历史时读数从未存在
- `409`：同 ID+修订号内容冲突；旧修订号覆盖（单条与批量语义一致）；
  作废修订号原因冲突或不高于当前修订号；已作废读数再次以新修订号作废；
  以不高于作废修订号的提交尝试复活读数；
  规则修订同号但四项值/原因不一致、旧号提交、跳号提交
- `422`：无效窗口、窗口外读数、同刻不同读数（批量按最终状态判定）、
  批量请求为空批次/同批 `reading_id` 重复、缺时区/字段非法、
  作废原因为空或只含空白等；
  规则修订数值非有限、温度下限大于上限、时长非正、原因为空或只含空白；
  修订影响报告的两证书相同、跨审定窗口、版本未按先后排列也返回 422

> 批量补传中任一项命中 409/422，整批回滚，错误 `detail` 指出具体原因；
> 不会出现“批内前几项已生效、后几项失败”的部分写入。

---

## 4. 调用示例

服务启动后：

```bash
# curl 全流程（创建窗口 → 提交/幂等/冲突/修订 → 审定 → 两次签发 → 修订影响报告 → 批量补传 → 误采作废与恢复 → 规则版本化修订与跨规则版本报告）
bash examples/curl-examples.sh

# Python 全流程（仅标准库，演示三种结论、拒收、修订、版本快照、修订影响报告、批量补传、误采作废与恢复、规则版本化修订与跨规则版本报告）
python3 examples/python_client.py
# 或指定地址：
BASE_URL=http://127.0.0.1:8000 python3 examples/python_client.py
```

修订影响报告最小示例（`OLD_ID`、`NEW_ID` 为同一窗口两张已签发证书主键）：

```bash
curl -sS "http://localhost:8000/api/v1/certificates/revision-report?old_certificate_id=$OLD_ID&new_certificate_id=$NEW_ID"
```

最小示例：

```bash
curl -sS http://localhost:8000/api/v1/windows \
  -H 'Content-Type: application/json' -d '{
    "start_at":"2026-10-01T00:00:00Z","end_at":"2026-10-01T00:10:00Z",
    "allowed_temp_min":18,"allowed_temp_max":22,
    "max_continuous_exceed_seconds":300,"max_sample_interval_seconds":600}'

curl -sS http://localhost:8000/api/v1/windows/1/readings \
  -H 'Content-Type: application/json' \
  -d '{"reading_id":"a","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1}'

curl -sS http://localhost:8000/api/v1/windows/1/evaluation
curl -sS -X POST http://localhost:8000/api/v1/windows/1/certificates/issue \
  -H 'Content-Type: application/json' -d '{"note":"首签"}'
```

批量补传最小示例（互换采样时刻：`a`、`b` 同时以更高修订号改到对方原时刻）：

```bash
# 先有 a@00:00 rev1、b@00:10 rev1，再整批互换时刻（按最终状态判同刻，顺序无关）
curl -sS http://localhost:8000/api/v1/windows/1/readings/batch \
  -H 'Content-Type: application/json' -d '{
    "readings":[
      {"reading_id":"a","revision":2,"measured_at":"2026-10-01T00:10:00Z","temperature":20.1},
      {"reading_id":"b","revision":2,"measured_at":"2026-10-01T00:00:00Z","temperature":20.2}
    ]}'
# 原样重放同一批次 -> results 全部 idempotent，不产生新修订
```

误采作废与恢复最小示例（承接上文窗口 1 与读数 `a` rev2）：

```bash
# 作废：更高修订号 + 非空原因；当前审定立即剔除该读数并重算
curl -sS http://localhost:8000/api/v1/windows/1/readings/a/void \
  -H 'Content-Type: application/json' \
  -d '{"revision":3,"reason":"误采：该时刻传感器未校准"}'
# 相同修订号+相同原因重放 -> effect=idempotent；原因不同/低修订号 -> 409；
# 读数从未存在 -> 404；已作废再以新修订号作废 -> 409

# 查阅该 ID 全部修订历史（含作废记录：原因/操作时间/前一版本）
curl -sS http://localhost:8000/api/v1/windows/1/readings/a/revisions

# 恢复：单条（或批量）提交更高修订号 -> effect=restored
curl -sS http://localhost:8000/api/v1/windows/1/readings \
  -H 'Content-Type: application/json' \
  -d '{"reading_id":"a","revision":4,"measured_at":"2026-10-01T00:10:00Z","temperature":20.1}'
# 旧版提交（revision <= 作废修订号）不得复活 -> 409

# 作废/恢复后再次签发形成新版本；修订影响报告将该读数呈现为 removed / added
curl -sS -X POST http://localhost:8000/api/v1/windows/1/certificates/issue \
  -H 'Content-Type: application/json' -d '{"note":"作废/恢复后重签"}'
```

规则版本化修订最小示例（承接窗口 1；初始规则为第 1 版）：

```bash
# 第 2 版规则（当前版 1 + 1 才生效）：四项完整新值 + 非空原因；立即重算
curl -sS http://localhost:8000/api/v1/windows/1/rule-revisions \
  -H 'Content-Type: application/json' -d '{
    "revision": 2,
    "allowed_temp_min": 18.0, "allowed_temp_max": 19.5,
    "max_continuous_exceed_seconds": 300,
    "max_sample_interval_seconds": 600,
    "reason": "换季：收窄允许温度带"}'
# 原样重放（同号 + 同四项值 + 同原因）-> effect=idempotent，不留新记录；
# 同号值/原因不同、旧号、跳号 -> 409；
# 下限>上限、时长<=0、NaN/Infinity、空原因 -> 422；窗口不存在 -> 404

# 查阅全部规则版本（第 1 版为初始规则，before 为 null）
curl -sS http://localhost:8000/api/v1/windows/1/rule-revisions
```

---

## 5. 测试

```bash
pip install -r requirements.txt pytest
PYTHONPATH=. python -m pytest tests/ -q
```

- `tests/test_evaluation.py`：审定算法纯函数测试（未知/超限/合格、
  边界等于、时区归一、区段无缝拼接等）。
- `tests/test_report.py`：修订影响报告纯函数测试（读数差异、带证据
  区段、边界对齐、未知来源、区间合并、汇总方向）。
- `tests/test_api.py`：HTTP 集成测试（幂等、冲突、修订替换、拒收、
  证书版本快照、签发/修订并发串行化）。
- `tests/test_batch_api.py`：批量补传 HTTP 集成测试（整批创建/混合 effect、
  窗口外/同刻/重复 ID 整批原子回滚、409 冲突与旧修订号、活动表与历史表不变、
  更高修订号互换时刻及顺序无关、重放不产生新修订、已签发快照不变、时区归一）。
- `tests/test_void_api.py`：误采作废与恢复 HTTP 集成测试（作废剔除并立即重算、
  作废修订留存原因/操作时间/前一版本、同修订号同原因重放幂等、原因不同或
  低修订号 409、从未存在 404、已作废再次作废 409、空原因 422、单条/批量
  更高修订号恢复、恢复遵守窗口边界与最终状态同刻唯一、旧版提交不得复活、
  批量冲突整批回滚、证书快照不变与报告呈现 removed/added）。
- `tests/test_report_api.py`：修订影响报告 HTTP 测试（fail→pass、
  边界空白 vs 采样间隔超限、只依快照、跨窗口/倒序/同 ID/不存在拒绝）。
- `tests/test_rule_revision_api.py`：窗口规则版本化修订 HTTP 测试
  （初始第 1 版、连续修订号生效并立即重算、同号同值同原因幂等、
  同号值/原因冲突、旧号与跳号拒绝、非有限/区间倒置/非正时长/空原因 422、
  窗口 404、窗口起止与读数不变、旧证快照不变与后续签发采用新规则、
  跨规则版本报告分别依两证快照重建时间线并给出四项规则前后值、
  即使结论不变规则变化仍可见、旧库第 1 版回填幂等）。

## 6. 目录结构

```
app/
  config.py        # 环境变量配置
  database.py      # SQLite 连接、WAL、建表（含旧库补列迁移/规则第 1 版回填）、写事务（BEGIN IMMEDIATE）
  evaluation.py    # 核心审定算法（纯函数）
  report.py        # 修订影响报告（规则前后值、快照比对、时间线对齐，纯函数）
  models.py        # 结论枚举/常量
  schemas.py       # Pydantic 请求/响应模型（含规则修订）
  services.py      # 窗口、窗口规则版本化修订、读数（含误采作废/恢复）、审定、签发、修订影响报告业务逻辑
  main.py          # FastAPI 路由
examples/
  curl-examples.sh
  python_client.py
tests/
Dockerfile
docker-compose.yml
requirements.txt
```
