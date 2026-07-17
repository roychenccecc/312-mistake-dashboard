# 数据契约

本文描述看板读取的 SQLite 语义、统计口径和只读 API。本地生成的公开演示数据库遵循同一契约，但其中所有内容均为合成数据，数据库文件不进入版本控制。

## 1. 运行与只读约束

服务端只接受一个本地 SQLite 文件，通过 URI `mode=ro` 打开，并立即启用：

```sql
PRAGMA query_only = ON;
PRAGMA foreign_keys = ON;
```

数据库必须在服务启动前存在并具备所需表。服务不创建表、不执行迁移、不修复映射，也不写入缓存。所有比率都在每次请求时根据当前记录动态计算。

HTTP 服务固定监听 `127.0.0.1`。API 仅支持 `GET`，响应设置 `Cache-Control: no-store`。前端只向同源 API 发起请求；本项目没有 GitHub Pages 在线数据端点。

## 2. 最小逻辑模型

看板依赖以下逻辑实体。数据库可包含其他业务字段，但这些字段的语义不能改变。

| 表 | 关键字段 | 契约用途 |
|---|---|---|
| `chapters` | `chapter_id`, `subject`, `name` | 原始科目和章节目录 |
| `review_sessions` | `session_id`, `chapter_id` | 作答会话与原始章节关联 |
| `questions` | `question_id`, `source_type`, `year`, `question_type`, `question_family`, `prompt`, `answer`, `explanation` | 题目来源、题干、参考答案和解析 |
| `question_options` | `question_id`, `option_key`, `option_text`, `position`, `verification_status`, `source_reference` | v10 结构化选项；顺序与来源证据独立于题干 |
| `attempts` | `attempt_id`, `question_id`, `session_id`, `attempt_date`, `attempt_phase`, `independent_answer`, `used_hint`, `score`, `max_score`, `score_weight`, `mastery_unit_id` | 动态错误率的事实记录 |
| `chapter_mastery_units` | `mastery_unit_id`, `chapter_id`, `name`, `module_name` | 知识点目录 |
| `question_mastery_unit_links` | `question_id`, `mastery_unit_id`, `semantic_verification_status`, `tested_dimension` | 题目到知识点的语义映射 |
| `knowledge_exam_occurrences` | `mastery_unit_id`, `question_id`, `year`, `verification_status`, `source_id`/`source_reference`, `evidence_json`, 原题题型和分值字段 | 经核验且可追溯的考频证据 |
| `exam_frequency_audits` | `mastery_unit_id`, `status`, `year_start`, `year_end`, `blocker_reason`, `evidence_json` | 年份核验范围和完整性状态 |

`attempts.mastery_unit_id` 可以为空；非空时必须存在于 `chapter_mastery_units`。看板所需的数据库结构版本包含这一引用保护，以及“每题至多一个已核验主要知识点”的唯一性约束。

## 3. 合格作答与计分覆盖

作答进入分析范围必须同时满足：

```text
independent_answer = 1
AND used_hint = 0
AND attempt_phase IN ('pre_review', 'delayed_retest', 'legacy')
```

因此，引导复习、提示后作答和即时修复不进入错误率分母。跨日期的再次错误会作为新的合格作答自然反映，不另建“复错率”。

合格作答还必须具备以下有效分数，才能进入加权公式：

```text
score IS NOT NULL
max_score IS NOT NULL
score_weight IS NOT NULL
max_score > 0
score_weight > 0
0 ≤ score ≤ max_score
```

缺失或无效分数不会根据 `result` 等字段推测。接口同时返回：

- `eligible_attempts`：满足合格作答条件的记录数；
- `scored_attempts`：其中具备有效分数的记录数；
- `scoring_rate`：两者之比；
- `question_count` 与 `scored_question_count`：相应的不同题目数。

无有效计分记录时，错误率为 JSON `null`，界面显示“暂无数据”，不能显示为 `0%`。

## 4. 综合错误率

对每条具备有效分数的合格作答 (i)：

```text
weighted_success_i = score_weight_i × score_i / max_score_i

综合错误率 =
1 - Σ(weighted_success_i) / Σ(score_weight_i)
```

部分正确通过 `score / max_score` 直接贡献部分成功率；错误、未答或正确不依赖字符串标签，而由实际分数决定。接口保留六位小数，界面可按展示需要格式化。

真题错误率使用完全相同的合格作答和计分公式，但额外要求：

```text
questions.source_type = '真题'
```

该判断为精确匹配。包含“真题”字样的改写、汇编、变式以及 AI 或自编题不属于真题样本。

## 5. 主要知识点映射

每道进入错误率分母的题目必须对应唯一主要知识点。有效映射同时满足：

```text
semantic_verification_status = 'verified'
AND tested_dimension = 'primary'
```

契约要求：

1. 同一题最多存在一个这样的主要映射；
2. `attempts.mastery_unit_id` 必须与该主要映射一致；
3. 非空知识点 ID 必须存在于 `chapter_mastery_units`；
4. 题目可以保留多个已核验的 `secondary` 或实体关联，但它们不参与错误率归属；
5. 无法判断的题目必须保持未解析并出现在审计结果中，不得用题目 ID 伪造知识点 ID。

严格校验会检查映射覆盖、冲突和孤儿 ID。维护脚本默认失败关闭：存在清单错误或仍有未解析的合格题目时，`--apply` 不应继续。

章节别名只在查询展示层归一化。规范化后的章节会合并统计，但原始 `review_sessions.chapter_id` 不被改写，作答不会被复制或重复计算。

## 6. 考频证据与审计状态

考频是知识点维度的独立证据，不参与综合错误率计算。

### 原题级证据

只有以下记录进入已核验证据集合：

```text
knowledge_exam_occurrences.verification_status = 'verified'
```

每条 `verified` 记录还必须至少提供 `source_id` 或 `source_reference`，并带有非空的语义核验证据；缺少来源或证据时，严格校验失败，不能作为正式考频。

`candidate` 仅表示待核验线索，不能计入出现次数、不同年份数、近三年趋势或正式散点比较。

### 审计状态

`exam_frequency_audits.status` 的语义为：

| 状态 | 含义 | 页面处理 |
|---|---|---|
| `complete` | 指定范围已经完整逐题核验 | 仅当范围恰为 2007–2026 时产生正式考频 |
| `partial` | 只核验了明确的部分年份或材料 | 显示已核验下限，剩余范围未知 |
| `blocked` | 因原题缺失等原因无法完成 | 显示已核验下限与阻塞原因 |
| 无记录/异常值 | 尚无可依赖审计 | 显示“考频未知” |

只有同时满足以下条件，`exam_frequency.formal` 才为 `true`：

```text
status = 'complete'
AND year_start = 2007
AND year_end = 2026
```

正式考频可以出现真实的 0；其他状态下，`occurrence_count` 和 `distinct_year_count` 必须为 `null`。若已有核验原题，则通过 `verified_occurrence_lower_bound`、`verified_distinct_year_lower_bound` 和非零年份证据表示下限。未知年份不能自动补 0。

错频—考频散点只接受同时具备个人错误率和正式考频的知识点。未知、部分或阻塞项必须单列。

## 7. 筛选与新鲜度

公共筛选参数：

| 参数 | 格式 | 说明 |
|---|---|---|
| `date_from` | `YYYY-MM-DD` | 含起始日期 |
| `date_to` | `YYYY-MM-DD` | 含结束日期 |
| `subject` | 文本 | 规范化科目名 |
| `chapter_id` | 文本 | API 返回的规范化章节 ID |

`date_from` 晚于 `date_to` 时返回 `400`。知识点接口要求 `chapter_id`，题目接口要求 `mastery_unit_id`；不一致的科目、章节和知识点组合返回 `400` 或 `404`。

所有主要响应包含 `freshness`：

- `start_date`、`end_date`：当前筛选结果的日期范围；
- `data_cutoff_date`：整个数据库中最新的合格作答日期；
- `generated_at`：服务端生成响应的 UTC 时间。

即使筛选日期改变，页面仍应显示全库的数据截止日期。

## 8. 只读 API

### `GET /api/summary`

返回 `overall`、`real_exam`、`coverage`、`freshness`、可用科目和当前章节选择。覆盖信息区分作答映射覆盖、题目映射覆盖、完整考频审计覆盖与已有审计状态覆盖。

### `GET /api/chapters`

返回规范化章节列表。每一项含综合指标、真题指标和映射覆盖；默认按有数据项的综合错误率降序排列。没有有效计分的章节错误率为 `null`。

### `GET /api/knowledge?chapter_id=...`

返回章节内知识点的综合指标、真题指标、课程重要性元数据，以及原题级考频证据和审计状态。`unknown_frequency` 明确列出不能进入正式比较的知识点 ID。

### `GET /api/questions?mastery_unit_id=...`

返回知识点下具有当前筛选范围内合格作答的题目及其失败记录；仅有考频映射、但尚无个人合格作答的原题不混入个人错题明细。默认展示顺序为真题、教材题、辅导书题、改写/变式题、AI/自编题及其他来源；来源排序不改变任何指标。

题目明细还返回按 `position` 排序的 `options`，以及以下 `options_status`：

| 状态 | 含义 |
|---|---|
| `structured` | 已从 `question_options` 读取结构化选项 |
| `legacy_inline` | 旧题把选项保留在 `prompt` 中，前端不重复渲染 |
| `missing` | 客观题没有可展示的选项，页面明确提示“选项未记录” |
| `not_applicable` | 简答、论述等题型不需要选项 |

为兼容尚未执行 v10 迁移的只读数据库，`question_options` 表不存在时服务不会报错；客观题会依据旧题干格式返回 `legacy_inline` 或 `missing`。服务端不在运行时猜测、拆分或写回旧题干。

## 9. 空值与对账规则

- 未知数值使用 JSON `null`，不能用 `0`、空字符串或虚构默认值替代。
- 总体、章节、知识点和题目接口必须使用同一套合格作答和有效分数规则。
- 按章节或知识点汇总后的样本数、总权重与加权成功量应能回加到相同筛选下的上一级指标。
- 章节别名归一化不得导致重复或遗漏。
- 数据库在启动、查询和关闭前后的内容哈希应保持一致；测试应验证非 `GET` 方法被拒绝。

## 10. 发布数据规则

公开仓库不提交任何 SQLite 数据库。`scripts/create_demo_database.py` 只在本地从零生成合成的 `data/demo.sqlite3`，该产物由 `.gitignore` 排除。不得从真实数据库抽取、扰动、删列或改名后作为演示数据。真实题目、成绩、时间线、错误描述、稳定 ID、同步映射和资料文件均不属于本契约的公开数据。
