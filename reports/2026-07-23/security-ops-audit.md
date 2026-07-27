# 安全与运维审计报告 · 2026-07-23

- **审计人**：Ops_安全审计员_A4（只读审计，未修改任何受审文件）
- **审计对象**：A股投资助手（硅基文明消费股交易系统）仓库根 `/Users/wangjianbin/Desktop/08-独立项目与工具/A股投资助手`
- **审计基线**：HEAD = `c92e016 Clarify agent data-quality contracts`（2026-07-16），另有未提交工作区改动（见 §6）
- **方法**：只读检查 manifest 与调度器源码、`.gitignore` 与 `git ls-files`/`check-ignore` 比对、`git log -p --all` 全历史密钥模式扫描、Docker/启动脚本/监控脚本人工审读、文档与代码交叉核对

---

## 总体结论

**未发现"高"风险问题**：全历史提交中未发现真实密钥/token，`.env` 系文件从未被 Git 跟踪，六项要求的忽略规则全部就位，代理调度器具备双指纹校验与 verdict 闸门。

主要残余风险集中在**"未跟踪但未忽略"的个人数据目录**（误 `git add -A` 即泄露个人持仓与群 ID）、**代理边界校验对 gitignored 路径不可见**、以及 **start.sh 的端口误杀行为**，共 3 项中风险。

| 级别 | 数量 | 条目 |
|---|---|---|
| 高 | 0 | — |
| 中 | 3 | M1 个人数据目录未入 .gitignore；M2 worktree 边界对 ignored 路径不可见且为事后检测；M3 start.sh 端口误杀 |
| 低 | 8 | L1–L8，见各节 |

---

## 1. ops/agents/manifests/ 边界与任务定义

调度器 `ops/agents/dispatch.py` 的机制先行评价：`load_manifest` 校验必需字段、mode 白名单（`read_only`/`worktree_code`）、`max_subagents ≤ 3`、verdict 非空（dispatch.py:92-114）；只读任务采用**双指纹**——`repo_fingerprint`（git diff + 未跟踪文件内容哈希）+ `protected_fingerprint`（对 forbidden_paths 按文件系统遍历，覆盖 gitignored 运行时路径，dispatch.py:136-165）；写任务先拒绝 allowed_paths 内未提交改动、再建独立 worktree 执行、事后校验变更路径白名单（dispatch.py:139-172）。整体设计优于多数同类实现。

### 1.1 daily-data-quality（hermes，read_only）— 合理，低风险

- `allowed_paths: []` + `forbidden_paths` 覆盖 universe.json、web/data/runtime、research/runtime、web/lib、pyserver（manifest 第 6-7 行）。只读任务的所有敏感面均被 `protected_fingerprint` 监控，越权写会被拒绝。
- `required_verdict: "PASS"` 语义正确：数据质检失败 → 代理输出 FAIL → dispatch 判 failed，即"数据坏 = 任务败"，符合 AGENTS.md "任何硬门槛失败都停止部署"的口径。
- prompt 明确禁止把 tickflow-stock-panel 当作生产源、禁止提议交易、禁止改文件，并要求首行输出 PASS/FAIL，可机审。
- **L1（低）**：`protected_fingerprint` 使用 `st_size:st_mtime_ns` 而非内容哈希（dispatch.py:64-69），理论上可用等长改写 + `touch -r` 还原 mtime 绕过。对本地受信代理场景可接受；若威胁模型升级，建议对 ≤50MB 文件改内容哈希。

### 1.2 candidate-audit（claude，read_only）— 基本合理，一处保护缺口

- forbidden 含 `research/runtime/registry`（前瞻性地保护尚未创建的注册表——当前 `research/runtime/` 下只有 baselines/benchmarks/cache/data/downloads/mlflow.db/qlib，registry 与 `active_model.json` 均不存在）。
- **L2（低）**：forbidden 只列了 `research/runtime/registry`，未覆盖 `research/runtime` 其余部分（`ledger.db`、`data/`）。只读代理若误写 ledger 或数据分区，`protected_fingerprint` 检测不到（不在 forbidden 即不指纹）。建议与 daily-data-quality 对齐，改为整个 `research/runtime`。
- "never promote a model" 目前仅靠 prompt 约束 + registry 不存在的事实兜底；待 registry 落地后应复查该 manifest 的 forbidden 是否同步更新。

### 1.3 weekly-experiment（hermes，worktree_code）— 中风险（M2），本项目最重发现

- `allowed_paths: ["research"]`，forbidden 列 web/pyserver/ops/tickflow-stock-panel/universe.json；`max_subagents: 3` 顶格但未超 dispatcher 上限；timeout 3600s 与测试命令 `cd research && uv run pytest` 合理。
- **M2a（中）边界对 gitignored 路径不可见**：写任务的事后校验 `git_paths()` 基于 `git status --porcelain`（dispatch.py:31-39），**不列出被 .gitignore 忽略的文件**。`/research/runtime/`（含未来的 registry、`active_model.json`）整目录被忽略（.gitignore:26），代理在 worktree 内改写这些文件不会产生任何 git 可见变更，`invalid` 检查（dispatch.py:167-172）必然放行。AGENTS.md 明文"代理不得编辑 `active_model.json`/注册表"，但 manifest 层面对 weekly-experiment **没有**对应的可执行防线。缓解因素：worktree 物理隔离使 ignored 文件改动不回流主仓，合并分支时 ignored 文件也不随 diff 走——风险在"把 worktree 目录当作已验边界的产物直接复制/采信"的下游操作。
- **M2b（中）边界是事后检测而非事前预防**：`agent_command`（dispatch.py:80-89）以用户完整权限拉起 hermes/gemini/claude CLI；hermes 分支（`hermes -z`）没有任何沙箱标志。代理运行期间可写 worktree 外任意绝对路径，dispatch 只在结束后报错，不阻止已发生的写入（包括 `.env`、部署脚本）。建议：prompt 中已有 forbidden 清单，可再在 dispatcher 层为写任务预生成 `protected_fingerprint`（与只读任务同款）覆盖 forbidden_paths，至少做到事后可检测 ignored 路径改动；长期可评估 `sandbox-exec`（macOS）或容器化执行。
- **L3（低）**：`git worktree add -b agent/...` 创建的分支与目录用后不清理（dispatch.py:151-156 无移除逻辑），长期积累 `.agent-worktrees/` 与 `agent/*` 分支。建议加 `--cleanup` 选项或在结果 JSON 中提示人工清理命令。

### 1.4 monthly-oss-review（gemini，read_only）— 合理，低风险

- forbidden 覆盖全部四个代码目录，任务面是外部官方仓库（Qlib/LightGBM/Optuna/Evidently/RQAlpha/vn.py/tickflow-stock-panel）的版本与许可证动态，不触碰本仓文件。`read_only` + 空 allowed_paths 与该定位自洽。

---

## 2. .gitignore 覆盖核查 — 六项全覆盖，另有中风险缺口（M1）

逐项核对（.gitignore 共 31 行）：

| 要求项 | 覆盖情况 | 证据行 |
|---|---|---|
| `cache.db` | ✅ `*.db`、`*.db-journal`，且显式 `pyserver/cache.db*` | 9-10, 15 |
| `.env` | ✅ `.env` | 6 |
| `.env.local` | ✅ `.env.local` + `.env*.local` | 7-8 |
| `mlruns` | ✅ `/research/mlruns/` | 27 |
| `runtime` | ✅ `web/data/runtime/**`、`/research/runtime/`、`/ops/agents/runtime/`（含 `.gitkeep` 例外） | 18-23, 26, 30 |
| `node_modules` | ✅ `node_modules/` | 1 |

额外正确覆盖：`.next/`、`.venv/`、`.DS_Store`、`/.playwright-cli/`、`/.agent-worktrees/`、`web/.cache/`、`/research/Users/`（实测该目录含 `wangjianbin` 子目录，为 Qlib _HOME 泄漏产物，已忽略 ✅）。

**M1（中）个人数据目录"未跟踪也未忽略"**：`git status --porcelain` 显示 `?? monitoring/`、`?? tmp/`、`?? reports/`、`?? web/scripts/monitor.py` 全部裸露在暂存视野中；`tickflow-stock-panel/`（907MB，含自身 8.4MB `.git`）仅靠 `.git/info/exclude` 第 7 行**本地**排除，换新机器/新克隆即失效。这些目录含个人真实持仓（monitoring/state-2026-07-20.json：申菱环境 400 股、晶瑞电材 4000 股、鹏鼎控股 200 股及退出线）、交易复盘截图（tmp/pdfs/*.png）、飞书群 ID（web/scripts/monitor.py 第 12 行 `FEISHU_CHAT = "feishu:oc_..."`）。一次 `git add -A` 即全部入库。建议把 `/tmp/`、`/monitoring/`、`/reports/`、`/tickflow-stock-panel/` 移入 `.gitignore`（与本机 `.git/info/exclude` 双保险），`web/scripts/monitor.py` 要么纳管要么忽略。

---

## 3. git log -p 历史密钥抽查 — 未发现真实密钥（通过）

执行的全历史扫描（`git log -p --all`，含已删除行）：

- `sk-[a-zA-Z0-9]{20,}` 模式：命中均为 `sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` 掩码占位，出处为 `web/env.example.txt`（首次出现于 f9e1efa）、`web/test/deepseek.test.ts`、文档——**无真实 DeepSeek key**。
- `TUSHARE_TOKEN` 赋值：命中为 `xxxx...` 掩码或 `your-tushare-pro-token-here` 占位（pyserver/env.example）——**无真实 Tushare token**。
- `[token|TOKEN]=[a-f0-9]{32,48}` 模式：零命中。
- `DB_PASSWORD`/`ADMIN_PASSWORD`（3cb0f28 引入的 Umami 部署脚本）：值为部署时 `openssl rand -hex` **生成**，非硬编码 ✅。
- `.env`/`.env.local` 文件名维度：`git log --all -- '.env' '**/.env' ...` 零记录——**从未被提交** ✅。

**L4（低）**：README.md:185-190 与 `web/env.example.txt` 明文写入生产服务器公网 IP 及 root 登录用户（`DEPLOY_HOST=root@47.77.231.22`，历史提交 3cb0f28 起）。对私有仓库可接受；若仓库可能转公开，建议改为占位符并把真实地址留在本地 `.env.local`。

---

## 4. docker-compose.yml / start.sh / monitoring/ 安全性

### 4.1 docker-compose.yml — 良好，少量加固空间

- pyserver **不向宿主暴露端口**（仅 compose 内部网络），web 只映射 `3100:3000` ✅；TUSHARE_TOKEN 经 `env_file: ./pyserver/.env` 注入、不写入 compose 文件 ✅；两服务 `.dockerignore` 均排除 `.env*`（web）与 `.env`、`cache.db*`（pyserver），`COPY . .` 不会把密钥带进镜像 ✅；healthcheck 用本地 urllib、无外部依赖 ✅。
- **L5（低）**：两 Dockerfile 均无 `USER` 指令（容器内 root 运行）、compose 无 `restart` 策略与只读根文件系统等加固。个人自托管场景可接受，上公网前建议补。

### 4.2 start.sh — 中风险（M3）

- 优点：`set -euo pipefail`、`trap cleanup EXIT INT TERM`、显式处理 macOS bash 3.2 无 `wait -n` 的兼容注释（start.sh:48-49）。
- **M3（中）**：`free_port()` 对占用 8001/3000 端口的**任意**进程直接 `kill`、失败再 `kill -9`（start.sh:11-27），并不校验该进程是否本项目拉起。脚本头部注释声称"If a port is already taken by our own process, reuse it"——**注释与行为不符**：代码既没有 reuse 逻辑，也不区分进程归属。在共享开发机上会误杀他人服务。建议：kill 前用 `ps -p $pid -o command=` 匹配 `uvicorn main:app`/`next dev` 特征，不匹配则报错退出。

### 4.3 monitoring/ — 用途正当，隐私数据需隔离（与 M1 联动）

- 内容：两个盘中巡检脚本（check.py=2026-07-20 持仓止损监控，buy_watch.py=2026-07-21 买入信号观测）+ 对应 state JSON。由定时任务每周期调用，数据源为腾讯公开行情接口（`qt.gtimg.cn`、`web.ifzq.gtimg.cn`），仅 HTTP GET + 本地 state 读写，**无密钥、无下单动作、无外部写**，输出纪律（无触发只报"无新触发"、数据过期拒绝使用、15:02 后 FINAL_SUMMARY 并终态化）设计克制 ✅。
- 成交确认语义正确：`confirmedExecutions` 只由主会话写入、脚本只读（check.py:180-181 注释与实现一致）✅。
- **L6（低）**：`TRADE_DATE`、`STATE_PATH` 硬编码单日日期，脚本为一次性用品，过期即成死代码；check.py:235 的 `if nb.hour >= 15 and nb.minute >= 2 or nb.hour > 15` 依赖 and/or 优先级碰巧正确，可读性差。建议统一归档到 `monitoring/archive/` 或删除，留模板参数化日期。

---

## 5. tickflow-stock-panel/ 与 tmp/ 是什么、该不该留

- **tickflow-stock-panel/**：第三方开源项目 `shy3130/tickflow-stock-panel` v0.1.64 的完整克隆（含自身 `.git` 8.4MB，总计 **907MB**），一个基于 TickFlow 数据源的自托管 A 股选股/监控/回测工作台（MIT 许可）。定位是**参考项目**：daily-data-quality 与 monthly-oss-review 两个 manifest 都将其列为外部参照，prompt 明确"不得当作生产数据源"。**建议不留在仓库根**：体积过大且与生产代码同根易混淆；移到仓库外目录或至少按 M1 加入 `.gitignore`。当前无提交风险（本地 exclude 生效中），故定低-中。
- **tmp/**：仅 `tmp/pdfs/` 下 12 张 `risk-case-*.png` 交易风控复盘截图（1.9MB），无 PDF、无代码。**属个人复盘材料，不应留在仓库**——删除或移入个人笔记目录；保留则必须入 `.gitignore`。

---

## 6. README.md / AGENTS.md 与实际代码一致性

| 文档声明 | 核验结果 |
|---|---|
| README 快速开始/daily:close/dashboard:update/dashboard:build/deploy:server | ✅ `web/package.json` scripts 全部存在，命令串一致 |
| README "DeepSeek 只用于辅助刷新股票池，不参与交易评分" | ✅ 与代码结构一致（信号为确定性规则打分，DeepSeek 在 universe 刷新链路） |
| AGENTS.md "research 固定 Python 3.11" | ✅ `research/pyproject.toml` `>=3.11,<3.12`（pyserver 为 `>=3.13`，文档未误述） |
| AGENTS.md 测试命令 `node --test --import tsx` | ✅ 与 package.json `test` 一致 |
| CI 覆盖 | ⚠️ `.github/workflows/ci.yml` 只有 web（typecheck+test+build，用 dummy key ✅）与 pyserver（py_compile+import，dummy token ✅）两个 job，**research 的 pytest/ruff 不在 CI**——与 AGENTS.md "每个时点标签/晋升门/注册表变更都需回归测试"的要求存在执行缺口（L7 低） |
| README "运行刷新不应产生 Git 变更" | ⚠️ 当前工作区 `web/package.json` + `package-lock.json` 有未提交改动（overrides：js-yaml 4.2.0→4.3.0、新增 sharp 0.35.3，疑似人工依赖安全升级）+ 未跟踪 `web/scripts/monitor.py`。非刷新所致可能性大，但工作区现状与该口径不符，且未提交依赖变更影响构建可复现性（L8 低） |
| README "research/runtime/registry 存候选/正式/回滚模型状态" | ⚠️ registry 目录尚不存在，文档超前于实现（信息性，不算缺陷） |
| AGENTS.md "代理不得编辑 active_model.json" | ⚠️ 仅 candidate-audit 显式 forbidden 了（尚不存在的）registry 路径；weekly-experiment 无对应可执行防线（已计入 M2a） |
| README "不提交 .env、cache.db、API key" | ✅ 与 §2、§3 实测一致 |
| ops/agents/README.md 对调度器行为的描述 | ✅ 与 dispatch.py 实现逐项吻合（双指纹、worktree 拒绝脏 allowed paths、verdict 闸门） |

---

## 建议优先级清单

| # | 级别 | 动作 |
|---|---|---|
| M1 | 中 | `.gitignore` 增加 `/tmp/`、`/monitoring/`、`/reports/`、`/tickflow-stock-panel/`；决定 `web/scripts/monitor.py` 纳管或忽略（若纳管，先把飞书 chat_id 移到环境变量） |
| M2 | 中 | dispatch.py 写任务增加 forbidden_paths 的 `protected_fingerprint` 前后比对；weekly-experiment manifest 显式 forbidden `research/runtime`；评估 agent 执行沙箱化 |
| M3 | 中 | start.sh `free_port` 增加进程归属校验，不匹配则退出而非强杀 |
| L2 | 低 | candidate-audit forbidden 补 `research/runtime`（整体） |
| L3 | 低 | dispatch.py 增加 worktree/branch 清理或提示 |
| L4 | 低 | README/env.example 中生产 IP 与 root 用户占位化（若仓库有公开计划） |
| L5 | 低 | Dockerfile 增加非 root `USER`，compose 增加 `restart: unless-stopped` |
| L6 | 低 | monitoring 过期单日脚本归档/参数化 |
| L7 | 低 | CI 增加 research pytest（可允许失败起步） |
| L8 | 低 | 提交或回滚 web/package.json 依赖 overrides 改动，恢复"刷新无 Git 变更"口径 |
| L1 | 低 | protected_fingerprint 升级内容哈希（可选） |

*报告完。本审计为只读操作，未修改任何受审文件；所有结论均可在上述文件与行号处复核。*
