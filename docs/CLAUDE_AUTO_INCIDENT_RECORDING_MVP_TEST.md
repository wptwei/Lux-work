# 自动记录与自动奖惩匹配 · MVP 交付与验收

日期：2026-11-04。对应实施说明 `docs/CLAUDE_AUTO_INCIDENT_RECORDING_MVP.md`：

> 手动输入、语音听写、语音助手共用登记入口；保存后自动调用现有奖惩匹配引擎，把条款、依据和结果关联到同一事件；不加入主管复核或审批流程；重复提交不重复登记、匹配失败不丢记录、补充更正留痕并重新匹配。

## 1. 本批已实现

- **一个登记入口**：`IncidentIntake.submit()` 同时服务手动输入、语音听写、语音助手。三个来源使用同一套 `Submission`、同一张来源表、同一套回执字段和同一套状态。
- **保存即匹配**：事件、来源、操作时间线、版本账本和持久匹配任务在**一个短事务**内写入；事务提交后立即返回“已记录，编号 XXX，正在匹配奖惩条款”，随后在同一轮对话内执行匹配并更新结果。
- **结果回写同一事件**：匹配批次通过 `incident_id` + `incident_version` 关联；`RewardPenaltyStore` 按事件 upsert 一条展示记录，不产生孤儿结果。事件详情页显示当前条款、依据、来源页码和缺失条件，并保留历史批次。
- **无需主管审批**：新记录状态为“已记录”，处置状态为 `RECORDED`；没有“待主管确认”步骤，也没有审批按钮。保存和查看不以主管确认为前提。
- **重复提交不重复登记**：`UNIQUE(actor, submission_id)`，同编号同内容返回原事件并标记 `idempotent`；同编号不同内容报冲突；不同编号的相同文字不合并。
- **匹配失败不丢记录**：异常写回“匹配失败，后台重试中”，事件仍在；带租约、次数上限和退避的持久任务继续重试，超过上限标记 `dead`，仍可在详情页手动重新匹配。
- **补充/更正留痕**：追加新版本而不是覆盖；旧批次保留并显示“已被新版本取代”；新版本自动重新匹配；旧任务晚到不会覆盖当前版本。
- **结果表达克制**：候选条款标注“系统匹配建议·需核对”，相关性命中率明确“不代表适用概率”；不编造金额，不把“未匹配”说成“无违规”。
- **状态区分**：`candidate / needs_information / no_match / rules_missing / failed` 分别显示，规则库为空与匹配执行异常不混为一谈。

本批**不包含**：多人重复上报合并、普通聊天自动发现事件、处罚执行/审批、跨会话播报历史结果。

## 2. 修改文件

| 文件 | 变化 |
|---|---|
| `core/incident_intake.py` | 登记状态 `registered`、版本账本读写、补充/更正事务、按版本去重的匹配任务、匹配历史、详情与权限、失败重试 |
| `core/reward_penalty_engine.py` | 规则完整性校验与健康信息、`rules_missing`、匹配依据、缺失适用条件、页码标签、规则/匹配器版本 |
| `core/reward_penalty_store.py` | `upsert_for_incident()`：按事件维护一条可追溯记录并更新批次关联，不重复插入 |
| `core/incident_store.py` | 增加 `RECORDED = 已记录` 显示；保留既有 `PENDING_CONFIRMATION` 流程供事件中心多 Agent 表单使用 |
| `migrations/incidents/0003_incident_versions.sql`（新增） | 增量新增版本账本表，不修改 0001/0002 |
| `app.py` | 两阶段对话回执、自动匹配结果卡片、事件台账重排、事件详情、补充/更正表单、重新匹配、奖惩记录同步回调 |
| `tests/test_incident_intake.py` | 扩展版本、依据、规则缺失、重试、详情权限测试 |
| `tests/test_auto_incident_record.py`（新增） | 应用层：聊天登记、重复提交、补充、更正、详情、匹配失败 |

## 3. 数据关系

全部位于现有 `data/incidents.db`，复用事件中心和权限体系：

```text
incidents(id = INC-YYYYMMDD-XXXXXX)
  ├─ incident_state             登记状态(registered) / 处置状态(RECORDED) / 匹配状态 / 当前版本
  ├─ incident_submissions       来源、是否编辑、操作类型(report/supplement/correct)、脱敏正文快照
  ├─ incident_versions          版本账本：version、submission_id、operation、正文摘要、superseded
  ├─ incident_actions           登记受理 / 补充登记 / 更正登记 / 奖惩匹配完成 / 重新匹配
  ├─ incident_tasks             dedupe_key = reward_match:{incident}:{version}:{ruleset}:{matcher}
  └─ incident_match_batches     UNIQUE(incident_id, incident_version, ruleset_version, matcher_version)
reward_penalty_records ── reward_penalty_links(record_id, incident_id, match_batch_id) ── incidents
```

- `incidents.task_id` 仍保留旧的非空唯一约束，登记时写入占位值；事件详情/匹配不依赖它。
- 登记状态、处置状态、匹配状态分开：匹配完成不等于事件结案，也不等于处罚生效。

## 4. 关键流程

### 4.1 登记（三种来源）

1. 前端在发送前把来源写入隐藏字段 `intake_hint`（渠道、`submission_id`、`conversation_id`、是否编辑、是否记录模式）；身份、区域和权限始终由服务端认证上下文决定。
2. 服务端识别明确记录意图（“帮我记录/帮我登记/现场记录……”），或听写/语音助手的记录模式；普通提问不登记。
3. 安全检查通过后，在 `BEGIN IMMEDIATE` 事务内写入事件、来源、版本 v1、时间线和匹配任务。
4. 提交后返回回执：`已记录，编号 INC-…，正在匹配奖惩条款`。写入失败则返回“记录未保存，请重试”，输入框保留草稿。

### 4.2 匹配（保存之后）

1. 事务外立即执行一次本地规则匹配；同时持久任务保证重启后仍可继续。
2. 匹配结果写入 `incident_match_batches`，并按事件 upsert 到 `reward_penalty_records`；回调失败不影响已保存的批次。
3. 聊天中先输出“已记录/正在匹配”，再在同一回答位置更新为最终结果；语音只在当前会话仍有效时播报“已记录 + 简短结论”，不播报历史结果。

### 4.3 补充 / 更正

- 口令：`补充记录 INC-20261104-ABC123：……`、`更正记录 INC-20261104-ABC123：……`；也可在事件中心「补充 / 更正 / 匹配详情」表单操作。
- 补充保留原文并追加 `【补充】`；更正替换当前正文。两者都会：
  1. 校验事件归属（非本人且无管理权限则拒绝）、事件未作废；
  2. 写入 `version = 当前版本 + 1` 的新来源与版本账本；
  3. 将旧版本 `superseded = 1`；
  4. 事件状态回到“已记录 / 待处理”，清空旧策略匹配；
  5. 按新版本入队并自动重新匹配。
- 旧批次不删除，详情页标记“已被新版本取代”；每批结果按版本唯一，旧任务晚完成只会写旧版本批次，不会覆盖当前状态。
- 同一账号、同一事件、同一操作、同一内容的补充/更正只生成一个版本（网络重试幂等）。

## 5. 状态与展示

| 层次 | 值 | 页面文案 |
|---|---|---|
| 登记 | `registered` | 已记录 |
| 处置 | `RECORDED` | 已记录（历史 `PENDING_CONFIRMATION` 仍兼容事件中心表单） |
| 匹配-进行中 | `pending` / `processing` | 待处理（后台匹配中）/ 匹配中 |
| 匹配-有结果 | `candidate` | 系统匹配建议·需核对 |
| 匹配-无结果 | `no_match` | 未匹配到条款 |
| 匹配-缺事实 | `needs_information` | 信息不足 |
| 匹配-缺规则 | `rules_missing` | 规则未配置 |
| 匹配-执行失败 | `failed` | 匹配失败，后台重试中 |
| 版本 | `stale` | 已被新版本取代 |

候选卡片固定展示：奖惩类别、对应行为、制度名称/文号/条号、来源页码（缺失时“页码未提供”）、条款原文、匹配依据、尚缺适用条件。相关性百分比旁注明“不代表适用概率”。

## 6. 数据库迁移

新增 `migrations/incidents/0003_incident_versions.sql`，仅 `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`。

本次执行前已用 SQLite 备份接口备份：`backups/incidents-pre-auto-record-20261104-132926.db`。

```powershell
python tools/migrate.py status
python tools/migrate.py up
```

实际结果：

- `[incidents]` 已应用版本 `[1, 2, 3]`，无待执行。
- `incident_versions` 表存在，主键 `(incident_id, version)`，含 `superseded` 与来源/正文摘要字段。
- 迁移前后业务行数一致：`incidents 5`、`incident_actions 9`、`incident_state 1`、`incident_submissions 4`、`incident_tasks 4`、`incident_match_batches 4`、`reward_penalty_records 27`。

> 与第一批文档相同的说明：这 5 条事件中有 4 条是 08:48 冒烟验证写入的测试事件，真实旧数据只有 `INC-20261103-6245D8` 一条。正式验收前如需要干净台账，应先备份再按编号清理，本次未删除任何数据。

## 7. 自动测试结果

全量回归：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

→ **450 tests OK**（第一批交付时为 437）。

新增/扩展的自动化验证：

| 验收点 | 测试 |
|---|---|
| 三种来源结构一致，新记录为已记录/`RECORDED` | `test_three_sources_share_one_structure` |
| 聊天先显示“已记录、正在匹配”，随后更新为条款结果 | `test_chat_record_saves_then_updates_match_result` |
| 同一提交重试不产生第二份事件与奖惩记录 | `test_duplicate_chat_submission_does_not_create_second_event` |
| 同编号不同内容报冲突 | `test_same_submission_id_with_different_text_is_a_conflict` |
| 事务中途失败无半套记录 | `test_mid_transaction_failure_leaves_no_half_records` |
| 匹配异常仍保留事件、状态为失败并可重试 | `test_matcher_failure_keeps_event_and_retries`、`test_match_failure_keeps_event_and_does_not_claim_success` |
| 匹配成功批次唯一，重复执行不重复时间线 | `test_matcher_success_persists_batch_and_is_idempotent` |
| 规则库为空返回 `rules_missing`，不冒充无匹配 | `test_rules_missing_is_not_reported_as_no_match` |
| 结果含依据、页码、缺失条件，规则健康可查 | `test_match_result_includes_basis_page_and_missing_conditions` |
| 补充追加版本、旧结果保留且标记过期、自动重匹配 | `test_supplement_keeps_old_result_and_rematches_new_version` |
| 更正替换正文、旧批次过期 | `test_correct_replaces_current_text_and_old_batch_expires` |
| 补充重试幂等，他人不可修改 | `test_supplement_retry_is_idempotent_and_other_user_is_forbidden` |
| 补充/更正口令必须带真实事件编号 | `test_amendment_command_requires_real_incident_id` |
| 失败任务可重新排队 | `test_retry_match_requeues_dead_task` |
| 详情按权限返回 | `test_detail_is_permission_scoped` |
| 应用层：详情/更正表单显示版本与条款 | `test_detail_and_amend_form_show_versions_and_clauses` |
| 应用层：聊天补充生成 v2 且只保留一条奖惩记录 | `test_chat_supplement_creates_new_version_and_rematches` |

> 以上为自动测试结果，使用虚构文本与本地规则引擎。**不代表**真机语音识别、真实麦克风或真实制度归档效果已经验收。

## 8. 人工验收步骤

前置：服务通过统一流程重启；`LUX_VOICE_DIALOGUE_ENABLED=true`；手机与电脑登录同一账号；记录手机型号、浏览器、网络。

### A. 手动输入

1. 在聊天框输入：`帮我记录：14:20，H1 一楼入口，一名供应商未由ICT人员陪同进入管制区。`
2. 期望：先出现“已记录，编号 INC-…，正在匹配奖惩条款”；随后同一回答更新为匹配结果或明确状态。
3. 期望状态：登记“已记录”、处置 `RECORDED`；不得出现“待主管确认”或审批按钮。
4. 打开事件中心 → 刷新台账：出现同一编号，来源“手动输入”，正文摘要与输入一致，匹配状态正确。

### B. 语音听写

1. 点「语音输入」，口述同一段现场描述，结束录音后检查识别文字；可在发送前手动修改。
2. 打开「现场记录」后发送。
3. 期望：生成新的 `INC-…`，来源“语音听写”；修改过则标记“已编辑”；正文为最终确认文字。
4. 空白或只有标点的识别结果：应提示补充内容，不生成空事件。

### C. 语音助手

1. 唤醒「小安小安」→ 听到“我在，请说”。
2. 说：`帮我记录：15:10，北门消防通道堆放货物，已通知清理。`
3. 期望：语音回答“已记录，编号 …”，可带一句简短匹配结论；不得把候选说成处罚决定。
4. 未说记录口令的普通提问（如“供应商违规怎么处罚？”）只回答，不产生事件。
5. 取消录音、ASR 中间结果、播报过程中不产生事件；记录模式唤醒但未提供内容时应追问，不登记空记录。

### D. 自动匹配与结果核对

1. 对 A/B/C 三条事件分别打开详情。
2. 期望：每条显示制度名称、文号、条号、页码（缺失写“页码未提供”）、条款原文、匹配依据、尚缺适用条件；候选标注“系统匹配建议·需核对”，相关性旁注明“不代表适用概率”。
3. 若某条显示“未匹配到条款”，确认它不是“无违规”；若显示“规则未配置”，确认它与“匹配失败”是不同文案。
4. 全程未点击任何审批按钮，结果已自动出现。

### E. 重复提交

1. 复制 A 的完整输入，在同一浏览器会话连续发送两次（或断网重发）。
2. 期望：仍只有一个事件编号，台账只有一条；不出现两份相同来源记录。
3. 在事件中心对同一事件重复点击补充/更正同一内容：提示沿用原版本，不追加新版本。

### F. 失败、重试与重启

1. 匹配异常时（可用测试环境临时停用规则文件模拟）：页面必须显示事件编号和“匹配失败，后台重试中”，不得提示“未保存”。
2. 修复后点“重新匹配”：当前版本重新执行，历史批次保留。
3. 重启服务后刷新页面：此前事件仍在；“待处理”的匹配应继续完成；同一版本不出现重复批次。

### G. 补充与更正

1. 在聊天中发送：`补充记录 INC-…：保安已到场劝阻，人员未进入管制区。`
2. 期望：回答“已补充”，版本变为 v2；详情中旧版本标记“已被新版本取代”，旧匹配结果保留，新结果自动生成。
3. 再发送：`更正记录 INC-…：供应商在NPI区域内无人陪同独自游走。`
4. 期望：版本 v3，正文被更正（不保留 `【补充】` 拼接），再次自动匹配；时间线包含“补充登记/更正登记”。
5. 用另一普通账号尝试修改同一条：应被拒绝；主管账号可查看但修改仍需留痕。

### H. 权限与隐私

1. 普通账号不能读取他人事件详情，也不能通过直接调用页面接口绕过；权限在服务端校验。
2. 日志中只应出现事件编号、状态、错误类别等元数据；不得出现业务正文原文、原始音频或密钥。

### 回传模板

```
手机 __ / 浏览器 __ / 网络 __
A 手动编号 __ ；首条是否“已记录/正在匹配” __ ；最终状态 __ ；是否有审批步骤 __
B 听写编号 __ ；来源 __ ；编辑标记 __ ；空识别是否拦截 __
C 语音助手编号 __ ；播报内容 __ ；普通提问/空录音是否误登记 __
D 条款字段是否齐全 __ ；页码缺失标记 __ ；状态文案是否区分 __
E 重复提交结果 __ ；重复补充结果 __
F 失败是否保留 __ ；重试 __ ；重启后批次是否重复 __
G 补充版本 __ ；更正版本 __ ；旧结果是否保留并标记过期 __
H 越权读取 __ ；日志是否含正文 __
问题与截图：
```

## 9. 未验证项与边界

- **真机麦克风验收未执行**：本文第 8 节需要在手机上实际执行并回传；自动测试不能代替。
- **真实制度文本与事实抽取效果未验证**：当前匹配为本地规则比对，依据和缺失条件来自规则字段与文本探测，仍需人工核对人员类别、次数、时间地点等事实。
- **金额/处罚**：不自动生成或执行，页面只展示条款中的原文表述。
- **多人重复上报合并**：未实现，各自保留来源与事件。
- **普通聊天自动发现事件**：未启用，避免误登记。
- **作废/撤回**：模型保留 `retract_request` 操作类型，本批未开放用户入口。
