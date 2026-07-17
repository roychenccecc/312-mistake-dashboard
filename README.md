# 312 错题分析看板

一个直接读取 SQLite 的本地错题分析看板。它把章节、知识点、题目明细和经语义核验的考频证据放在同一视图中，但不会修改复习数据库。

> A local-only, read-only dashboard for inspecting synthetic or privately held study-review data.

公开仓库不提交任何数据库。`scripts/create_demo_database.py` 会在本地生成完全合成的 `data/demo.sqlite3`，其中不含任何人的真实作答、错题、成绩或学习记录。

演示库是满足看板查询与校验所需字段的 **v9 兼容子集**，不是私人复习数据库的完整替代品，也不能交给维护回填脚本修改。

## 设计边界

- 服务固定监听 `127.0.0.1`，不会监听局域网或公网地址。
- SQLite 以 `mode=ro` 打开，并对每个连接执行 `PRAGMA query_only=ON`。
- HTTP API 只接受 `GET`；`POST`、`PUT`、`PATCH`、`DELETE`、`HEAD` 和 `OPTIONS` 均返回 `405`。
- 指标在请求时动态计算，不把错误率缓存或写回数据库。
- 前端没有外部依赖，也不会请求外部字体、脚本、分析服务或 CDN。
- 本项目没有 GitHub Pages 在线数据版本。GitHub 只托管源代码；真实数据库始终留在使用者自己的设备上。

`127.0.0.1` 不是身份验证机制。同一台电脑上的其他本地进程仍可能在服务运行期间访问页面，因此请只在可信设备上使用，并在看完后按 `Ctrl+C` 停止服务。

## 快速开始

要求 Python 3.10 或更高版本，不需要安装第三方 Python 包。

```bash
python3 scripts/create_demo_database.py

python3 scripts/sqlite/validate_mistake_dashboard_data.py \
  --database data/demo.sqlite3 \
  --strict

python3 scripts/mistake_dashboard_server.py \
  --database data/demo.sqlite3 \
  --port 4174
```

浏览器打开 [http://127.0.0.1:4174](http://127.0.0.1:4174)。

生成后的演示数据是默认数据源，因此也可以直接运行：

```bash
python3 scripts/mistake_dashboard_server.py --port 4174
```

## 指标口径

进入统计范围的合格作答必须同时满足：

```text
independent_answer = 1
AND used_hint = 0
AND attempt_phase IN ('pre_review', 'delayed_retest', 'legacy')
```

综合错误率按有效分数和权重计算：

```text
综合错误率 =
1 - Σ(score_weight × score / max_score) / Σ(score_weight)
```

只有 `score`、`max_score` 和 `score_weight` 都有效，且满足 `max_score > 0`、`score_weight > 0`、`0 ≤ score ≤ max_score` 的记录才进入公式。其余记录不会被猜测补值，而是通过“计分覆盖率”公开呈现。

真题错误率沿用同一公式，但只接受精确的 `source_type = '真题'`。真题改写、AI 变式和自编题不会混入真题统计。

考频与个人错误率是两组独立证据：

- 只有 `verification_status = 'verified'` 的原题级记录可作为已核验出现证据。
- `candidate` 记录永远不进入正式考频。
- 只有覆盖 2007–2026 且状态为 `complete` 的审计，才会给出正式出现次数和不同年份数。
- `partial`、`blocked` 或未审计知识点显示已核验下限或“考频未知”，绝不把未知年份当成 0。
- 错误率与考频不会合成为隐藏加权分数。

完整字段与 API 语义见 [数据契约](docs/data-contract.md)。

## 使用自己的数据库

不要把真实数据库复制进这个仓库，也不要把任何 SQLite 文件强制加入 Git。把私人数据库放在仓库之外，并显式传入相对路径：

```bash
python3 scripts/sqlite/validate_mistake_dashboard_data.py \
  --database ../private-data/312-review.sqlite3 \
  --strict

python3 scripts/mistake_dashboard_server.py \
  --database ../private-data/312-review.sqlite3 \
  --port 4174
```

服务端不会替你迁移、清洗或修复数据库。个人数据在打开前应满足数据契约和严格校验；映射回填脚本属于完整 v9 私人数据库的维护工具，必须显式传入 `--database`，并先在已验证的数据库副本上执行 `--dry-run`、人工检查审计报告。该脚本会明确拒绝合成演示库。

## 测试

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

同时验证演示数据库：

```bash
python3 scripts/sqlite/validate_mistake_dashboard_data.py \
  --database data/demo.sqlite3 \
  --strict
```

## 仓库结构

```text
data/                                  合成演示数据与隐私说明
docs/data-contract.md                  指标、映射、考频与 API 契约
integrations/mistake-dashboard/        无外部依赖的前端
scripts/mistake_dashboard_server.py    本机只读 HTTP 服务
scripts/create_demo_database.py         生成完全合成的演示数据库
scripts/sqlite/                        校验、审计与迁移辅助脚本
tests/                                 单元测试与只读验收测试
```

## 隐私与发布

请先阅读 [data/README.md](data/README.md) 和 [SECURITY.md](SECURITY.md)。真实题干、答案、错误笔记、作答日期、分数、稳定 ID、导出文件和数据库日志都可能构成个人数据；仅删除姓名并不等于完成脱敏。

本项目采用 [MIT License](LICENSE)。
