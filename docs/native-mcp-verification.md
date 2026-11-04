# Native MCP 本地验收记录

记录日期：2026-11-04（Asia/Shanghai）。本记录只证明本地实现、测试和限定范围独立审查，不是镜像发布、生产部署或 ASS 接入回执。

## 源码状态

- 工作分支：`feat/native-remote-mcp`。
- 开发基线／本地验收时 HEAD：`4201e2d1b12ed5c0820ac3e628deb8e360460eb3`。
- 本记录所述本地验收时，功能变更尚未提交；当时没有 commit、push、合并、触发 Actions、发布镜像或生产写入。后续经授权的功能分支 CI 应以对应 commit/run 为独立证据。
- 原生端点 `/api/mcp` 默认关闭。工具目录包含 49 项；目录、权限和 schema 有自动化断言。
- 本轮没有安装或升级前端依赖，`web-ui/package-lock.json` 与基线一致。

## 父代理实际复跑结果

| 验证 | 结果 | 边界 |
| --- | --- | --- |
| 完整后端非 live 回归 | 792 passed，3 deselected | 拒绝外部 DNS 和连接，仅允许测试自身创建的临时回环 HTTP fixture；未使用生产状态 |
| 前端 `npm run build` | prebuild 56/56 passed，`vue-tsc -b` 与 Vite 成功 | Node 22.23.1，已有依赖；仅现有 Browserslist 数据陈旧提示 |
| CI 契约测试 | 118 passed | 已包含在上述后端总数中，不能重复相加；Docker／registry 边界为 fixture |
| `scripts/check_ci_contract.py` | `CI publish-only contract verified` | 静态契约，不是真实 Actions 或镜像验证 |
| 原同步关键词提交后解锁失败探针 | 通过 | 真实临时 SQLite COMMIT 后注入解锁异常，返回提交事实 |
| 原前端独立复现 | 3/3 passed | 不再误报完全成功，不重复提交，不错误展示已提交任务为创建失败 |
| `git diff --check` | 通过 | 没有提交或暂存全部工作区 |

真实已安装 MCP SDK 2.0.0 连接独立完整 `src.app` 进程，通过实际 loopback HTTP 验证：

- initialize 协商 `2025-06-18`；按最小权限发现工具；读身份不能写入。
- 创建 disabled、无 cron 的关键词 fixture；任务 ID 0 兼容。
- CRUD/readback、同 request_id 重放不重复创建、过期 revision 拒绝。
- 持久回执查询；删除后临时任务数量为 0。
- 服务使用全新随机凭据、独立 SQLite／prompts／journal，禁止外连和真实爬虫启动；结束后清理临时实例。

SDK 的最小测试身份分别发现 20 个读工具、21 个选择性授权工具；这不是完整 49 项目录的生产授权。

## 独立审查结论

下列分工均已通过各自最后一轮独立复审，已报告的范围内阻塞均关闭。不同审查测试存在重叠，不应相加为全套数量。

- 传输与认证：凭据脱敏、嵌套／结构化环境秘密、非法 Bearer、明确 unknown/failed 回执、输出预算及历史回执重脱敏。
- 任务与提示词：跨 REST/MCP CAS、实例身份和版本、克隆源一致性、共享资产锁、统一根目录、空引用与降风险操作。
- 运行与生成：子进程配置版本绑定、事务提交快照、完成状态持久化顺序、取消与异常退出收敛。
- 同步关键词生成：同事务回执恢复、真实 COMMIT 后解锁异常、scheduler 失败、回滚、不可读证据、取消、同名并发隔离及旧成功响应兼容。
- 结果／诊断／设置：日志事件脱敏、共享配置写事务、不可变探测快照、原始状态 revision、结果提交证据和 blacklist incarnation。
- 通知与 AI 探测：真实 HTTP/SDK 底层重定向正反对照；代理与直连禁止自动重发，unknown 回执不自动重复调用。
- 前端：顶层与异步回执兼容、提交证据优先展示、安全重试、无虚构 job_id、迟到轮询不能恢复已清除状态。
- publish-only CI：固定发布上下文、同一已测试镜像、私有 registry 只读检查、不可变 manifest 与只读计划器契约。

这不是对整个历史应用、全部第三方依赖或任何生产环境的无漏洞保证。未执行真实 LLM、通知、闲鱼抓取、真实账号登录态或生产 CRUD 测试。

## 尚未验证／不得直接启用

1. 本机没有 Docker CLI。真实 Linux amd64 镜像构建、OCI provenance、Compose 解析和容器内 smoke 尚未执行。
2. GitHub Actions、私有 GHCR 发布、真实 job 日志到 planner 的链路尚未执行。fixture 测试不替代这些验收。
3. 未部署、未注册 Goofish 到 ASS、未授予生产工具权限。生产已授权其他服务不构成本服务的授权。
4. 已观察的生产 `/app/.env` 是只读单文件 bind；不能直接启用 `settings:write`。可写目录或非敏感覆盖存储方案须单独批准并验证，不能退化为原地截断写入绕过只读保护。
5. 既有前端依赖审计告警未通过本轮升级处理。锁文件未改，不能宣称依赖风险已消除；发布前应单独复核。Browserslist 提示也未通过安装更新消除。
6. 数据迁移及锁语义要求停止旧写端、一致备份 SQLite/WAL、prompts、配置和回执。旧、新写进程不能混跑；仅回退镜像不等于回滚数据。

## 后续授权顺序

1. 先审核拟提交文件，确认不含运行状态、凭据、测试临时文件；取得 commit/push 功能分支授权。
2. 功能分支 push 会运行 Linux CI 和隔离镜像验收，但不会触发 master 限定的 registry 发布，也不会连接 NAS 部署。
3. 真实 CI 成功后，再单独确认合并／master 发布；必须发布同一已测试镜像并取得可信不可变 manifest。
4. 按 `native-mcp.md` 和 `task-prompt-consistency.md` 完成生产配置、权限、入口、备份和回滚审查；另行授权部署与 ASS 注册。

若后续源码改变，需重跑受影响测试；本记录不能自动背书后续提交。任何阶段失败都保持 MCP 默认关闭和生产边界，不以 NAS 临时重建或真实副作用调用替代未完成的验收。
