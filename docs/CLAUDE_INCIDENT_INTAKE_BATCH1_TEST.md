# 统一事件受理 · 第一批交付与验收

日期：2026-11-04。对应实施说明 `docs/CLAUDE_UNIFIED_INCIDENT_INTAKE_AND_REWARD_PLAN.md` 第 11 节第一批：

> 统一 Submission 和 incident_intake 服务；接通三个输入来源；先保存待核对事件；持久任务；真实 ID 回执；原有记录与匹配页面建立关联。

## 1. 本批已实现

- **统一受理服务** `core/incident_intake.py`：手动、听写、语音助手、事件中心表单都调用同一个 `IncidentIntake.submit()`。
- **三种来源结构一致**：同一套回执字段、同一张 `incident_submissions` 来源表、同一套事件状态。来源只记录 `manual / dictation / voice_assistant / incident_form`，身份、区域与权限一律取自认证上下文。
- **先保存、后匹配**：事件、来源、时间线、待办任务在**一个短事务**内写入；`RewardPenaltyMatcher` 由持久任务 worker 在事务外执行。
- **重试不重复**：`incident_submissions` 对 `(actor, submission_id)` 唯一。同一提交重试返回同一 `incident_id`；同一编号携带不同内容报版本冲突，不静默覆盖。
- **匹配失败不丢记录**：匹配异常写回“匹配失败，后台重试中”，事件与来源保留；退避重试，超过次数标记 `dead`，事件仍在。
- **持久任务**：任务在 SQLite `incident_tasks` 表中，带租约、attempt、下一次重试时间、安全错误类别；进程重启后继续处理，租约到期自动回收。
- **真实 ID 回执**：聊天与事件中心都显示 `INC-YYYYMMDD-XXXXXX`，并给出待核对状态与匹配状态。事务未成功时明确提示“事件未保存”，不冒充已记录。
- **关联既有页面**：事件台账显示来源、版本、奖惩匹配状态；奖惩匹配记录通过 `reward_penalty_links` 关联到同一事件编号；事件中心研判结果回填同一事件。

第一批**不包含**：补充/更正版本、事件合并、普通聊天自动发现事件、统计报表（按实施说明属于第二、三批）。

## 2. 修改文件

| 文件 | 变化 |
|---|---|
| `core/incident_intake.py`（新增） | 规范化 `Submission`、`IncidentIntake`、持久任务 worker、租约/退避、回执与权限查询 |
| `migrations/incidents/0002_incident_intake.sql`（新增） | 增量迁移：只新增关系表，不修改 0001 基线 |
| `core/reward_penalty_engine.py` | 增加 `ruleset_version` / `matcher_version`，用于任务去重与匹配批次留痕 |
| `core/reward_penalty_store.py` | 匹配记录通过 `reward_penalty_links` 关联事件，不重建旧表 |
| `app.py` | 接入三种来源、现场记录开关、隐藏来源标记、事件中心先保存、奖惩入口统一受理、台账显示来源/匹配状态、启动 worker |
| `frontend/incident_record.js`（新增） | “现场记录”开关与来源标记；不携带身份字段 |
| `frontend/voice_input.js` | 听写最终结果填入后标记来源；人工编辑后标记 `edited` |
| `frontend/voice_dialogue.js` | 语音助手自动提交前标记 `voice_assistant` |
| `tests/test_incident_intake.py`（新增） | 13 项自动测试 |

## 3. 数据关系

全部位于现有 `data/incidents.db`，未新建平行事件中心：

```text
incidents(id = INC-...)
  ├─ incident_state(incident_id)          当前版本 / 登记状态 / 匹配状态 / 来源
  ├─ incident_submissions(actor, submission_id UNIQUE)
  │     来源渠道、来源版本(是否编辑)、操作类型、脱敏叙述快照
  ├─ incident_actions(incident_id)        登记受理、研判完成、人工决定、匹配完成
  ├─ incident_tasks(dedupe_key UNIQUE)    reward_match 待办：租约/attempt/退避
  └─ incident_match_batches(incident_id, incident_version, ruleset, matcher)
        候选条款、匹配状态、规则版本留痕
reward_penalty_records ── reward_penalty_links(record_id, incident_id) ── incidents
```

- `incidents.task_id` 继续保留 `UNIQUE NOT NULL` 兼容旧约束：登记时写入 `intake-<随机>`；事件中心模型任务建立后通过 `link_task()` 指向真实任务，旧的人工确认/结案仍能更新任务状态。
- 登记状态、处置状态、匹配状态分开维护。匹配完成不等于事件结案，也不等于处罚通过。

## 4. 幂等与事务

**提交事务**（`IncidentIntake.submit`，`BEGIN IMMEDIATE`）：

1. 校验 `submission_id`、长度、来源、操作类型；
2. 安全检查（命中 block/review 直接拒绝，不保存原文）；
3. 脱敏后写入 `incidents` + `incident_state` + `incident_submissions` + `incident_actions` + `incident_tasks`；
4. 提交后返回回执。任一步失败整体回滚——测试用触发器模拟中途失败，验证事件/来源/时间线/任务均为 0 条。

**幂等**：`UNIQUE(actor, submission_id)`。重复提交比较 `text_sha256_12`：一致返回原回执并标记 `idempotent`，不一致抛 `SubmissionConflict`。相同文字不是全局唯一键：不同 submission_id 仍可登记独立事件。

**匹配任务**：`dedupe_key = reward_match:<incident>:<version>:<ruleset>:<matcher>` 唯一。任务先领取（条件 UPDATE + 租约），匹配在事务外执行；结果按 `(incident_id, incident_version, ruleset, matcher)` 唯一写入批次，重试不会生成重复批次或重复时间线。`incident_version` 变化时旧批次保留，不覆盖新版本。

**重启恢复**：worker 只轮询数据库；`claim_task` 会把租约过期的 `processing` 任务重新排队。空闲轮询先做只读探测，不持续占用写锁。

## 5. 使用入口

1. **手动 / 听写**：输入框下方「现场记录」→ 下一条消息登记为待核对事件；或在消息开头说/写“帮我记录……”。听写填入后再修改，来源仍为听写并标记“已编辑”。
2. **语音助手**：唤醒后说“帮我记录：14:20 H1 入口有人未登记进入管制区”。只有明确记录口令才入台账；普通提问、ASR 中间结果、TTS 播报、取消的草稿不会登记。
3. **事件中心表单**：先保存待核对事件，再运行多 Agent 研判；研判失败时事件仍可查，并提示“已保存为待核对”。
4. **奖惩匹配（事件中心）**：提交后先登记事件，再即时匹配；匹配失败显示“已登记、失败待重试”，不丢失事件。
5. **查询与复核**：事件中心台账按权限显示本人/团队事件，含来源、版本、匹配状态；主管可确认处置、退回补充、确认结案。

## 6. 数据库迁移

已新增 `migrations/incidents/0002_incident_intake.sql`（全部 `CREATE TABLE IF NOT EXISTS`，可重复执行）。

```powershell
python tools/migrate.py status
python tools/migrate.py up
```

本次执行前已用 SQLite 备份接口备份：`backups/incidents-pre-intake-20261104-085540.db`。
迁移后核对：`incidents`、`incident_actions`、`reward_penalty_records` 数量不变（5 / 8 / 27），新增 5 张关系表。

> 说明：这 5 条事件中有 4 条是 08:48 应用层冒烟验证时写入真实库的测试事件（`INC-20261104-8344BD`、`674613`、`47FCCF`、`DFAB11`，创建人为 `system` / `zhangsan`），并非真实业务数据；真实旧数据只有 `INC-20261103-6245D8` 一条。正式验收前如需干净台账，应先备份再按这 4 个编号清理（涉及 `incidents`、`incident_actions`、`incident_state`、`incident_submissions`、`incident_tasks`、`incident_match_batches`）。

## 7. 自动测试结果

新增 `tests/test_incident_intake.py`（13 项，全部通过）：

| 验收点 | 测试 |
|---|---|
| 三种来源结构一致 | `test_three_sources_share_one_structure` |
| 同一提交重试只产生一份事件 | `test_retry_returns_same_incident_without_duplicates` |
| 同编号不同内容报冲突 | `test_same_submission_id_with_different_text_is_a_conflict` |
| 匹配抛异常事件仍在、状态准确 | `test_matcher_failure_keeps_event_and_retries` |
| 匹配成功写入批次且重复执行不重复 | `test_matcher_success_persists_batch_and_is_idempotent` |
| 事务中途失败无半套记录 | `test_mid_transaction_failure_leaves_no_half_records` |
| 红线拦截不落原文 | `test_redline_block_persists_nothing` |
| 无权用户不能读他人事件 | `test_other_user_cannot_read_receipt` |
| 过短描述不登记 | `test_short_or_empty_text_is_not_registered` |
| 口令识别保守、普通问题不登记 | `test_record_command_detection_is_conservative` |
| 租约到期可重试 | `test_expired_lease_is_reclaimed` |
| 旧版本晚完成不能覆盖新结果 | `test_attach_analysis_only_updates_matching_version` |
| 奖惩记录关联事件 | `test_reward_store_links_to_incident` |

全量回归：`.venv\Scripts\python.exe -m unittest discover -s tests -t .` → **437 tests OK**。
另有应用层冒烟验证（未纳入单元测试）：语音助手口令登记、听写记录模式登记、事件中心模型失败仍保存、奖惩表单关联事件，均通过。

## 8. 真实手机验收方法

> 自动测试使用虚构文本与本地规则引擎，不代表真实语音识别与事实抽取效果。以下步骤需要在手机上实际执行并回传结果。

前置：服务已重启；`.env` 中 `LUX_VOICE_DIALOGUE_ENABLED=true`；手机与电脑登录同一账号；记录手机型号、浏览器、网络。

### A. 语音助手登记（扬声器、Wi‑Fi 各一次）

1. 打开「语音助手」→ 说「小安小安」→ 听到「我在，请说」。
2. 说：「帮我记录，14点20分，H1 一楼入口，有一名供应商未登记，保安已劝阻。」
3. 期望：回答必须包含 `INC-2026...` 与“待核对”；不得说“匹配已完成”。
4. 打开事件中心，确认台账出现同一编号，来源显示“语音助手”，匹配状态为待处理/候选。

### B. 听写登记

1. 点「语音输入」，说同样的现场描述，结束录音。
2. 打开「现场记录」后点发送。
3. 期望：生成新的 `INC-...`，来源显示“语音听写”。若在发送前手动改过文字，回执/来源记录应标记“已编辑”。

### C. 手输登记

1. 输入「帮我记录：15:10，北门消防通道堆放货物，已通知清理」并发送。
2. 期望：生成第三个事件，来源“手动输入”。

### D. 查询与复核

1. 主管账号打开事件中心 → 「刷新事件台账」。
2. 期望：能看到三台来源不同、结构一致的事件；编号可复制。
3. 对其中一条执行「确认处置」并填写责任人；再执行「确认结案」并填写说明。
4. 期望：状态依次变为处置中/已结案，操作留痕；普通账号不能执行主管操作。

### E. 重复与失败

1. 连续两次发送完全相同的“帮我记录”文本：第二次应沿用原编号或明确提示重复，不应出现两份相同来源记录。
2. 在事件中心提交一条描述，若模型不可用：仍应显示事件编号与“已保存为待核对”，而不是“未保存”。
3. 重启服务后打开事件中心：此前登记的事件仍在；未完成的匹配任务应继续处理（页面匹配状态从“待处理”变为候选/无匹配）。

### F. 不应登记的情况

1. 普通提问「供应商违规怎么处罚？」→ 只回答，不产生事件。
2. 唤醒后不说话、或取消录音 → 不产生事件。
3. 语音回答播放过程中 → 不产生事件。

### 回传模板

```
手机 __ / 浏览器 __ / 网络 __
A 语音助手编号 __ ；是否显示待核对 __ ；来源 __
B 听写编号 __ ；来源 __ ；编辑标记 __
C 手输编号 __ ；来源 __
D 三条是否同结构 __ ；主管确认/结案 __
E 重复提交结果 __ ；模型失败仍保存 __ ；重启恢复 __
F 普通提问/空录音/播放中是否误登记 __
问题与截图：
```

## 9. 未验证项与边界

- **补充/更正**：第一批未开放，明确提示“后续批次开放”。因此“追问补充→纠正”需要在第二批完成后验收；当前不要用新事件覆盖旧结论。
- **多人重复上报合并**：未实现，保留各自提交与来源。
- **真实语音识别与事实抽取**：未在真机执行，需按第 8 节回传。
- 普通对话自动发现事件（B 方案）未启用，避免误记录。
- 规则相关性分数仍不是处罚概率；候选不代表处罚决定。
