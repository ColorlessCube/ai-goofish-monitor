# ASS 账号来源首次 NAS 交付（2026-10-19）

## 授权与范围

用户授权推送、部署、配置真实独立 Read Key 及实际测试，并明确不要最小范围试验：按 Goofish 常规流程、原任务参数运行，不使用 `debug-limit`，不禁用 AI、图片分析或通知。不批量触发任务、不绕过登录、验证码、网站熔断或暂停。

本次首次将 NAS 从上游镜像切到自己的 Fork。Fork 中此前已合并的管理员会话认证及只读机器访问随本次镜像上线；未接入新的 Hermes/MCP 客户端。

## 代码和发布

- 功能：`a4c05da`，PR #8；合并提交 `6f8ca0bf7f32db014eb23638a2d7685bd50d4df7`。
- 主 CI `39234037946` 成功。
- Delivery `39234038593`：backend/frontend/image 验证、测试原镜像发布成功；NAS deploy **skipped**，不是自动部署成功。
- 测试原镜像：`ghcr.io/colorlesscube/ai-goofish-monitor@sha256:50f51d4ff094604ac5b6b89218013cdeedd7fb82d8df8b7dfd2d5eadf13369a7`。
- NAS image ID：`sha256:1219309010db9f111ed5cf722fa3cddef9801c0dc08b8cff5ea88175fb25c954`；amd64、RepoDigest 和源码 revision 核对一致。
- manifest artifact：`12177974099`；测试原镜像 artifact：`12177774518`。
- 已在 GitHub 禁用旧 `docker-image.yml` 工作流，避免合并 PR 时旁路构建并推送另一份未验收的 mutable `latest`。保留同份测试镜像的 delivery 发布路径，未重跑 publisher、改包为公开或覆盖不可变标签。

NAS 自动部署通道仍未启用，没有新增 WireGuard peer、部署 SSH 账号或 service policy。本次使用已有管理员 SSH **手动部署固定 digest**，不能据此声称通用 NAS onboarding 已完成。

## 拉取权限与延迟

初次 NAS pull 因私有包鉴权拒绝。用户通过 GitHub device flow 授予本机 CLI `read:packages` 后，本机只在内存使用 OAuth 凭据换取此镜像仓库的短期 pull token，将短期 token 内存转交 NAS Docker API；不把广域 OAuth 凭据或永久 registry 配置写到 NAS。

19:27 开始的同一次拉取约至 19:44 才完成。SSH 等待超时后核对原 helper 仍在下载，继续跟踪原操作，没有重复发布/拉取，也未改变 Docker 代理、共享网络或镜像源。慢下载根因未定位。

## 备份与安装

- 项目：`/volume4/docker/ai-goofish-monitor`，原容器 `ai-goofish-monitor-app`。
- 发现无爬虫进程后停止旧实例，进行停机一致备份及 SQLite `quick_check`。备份读取较慢，超时后沿用原操作，未重复停机或重做未知结果写入。
- 备份：`.deployment/20261019-6f8ca0b/before.tar.gz`，20,322,350 bytes，SHA-256 `c6760ac122883c4dda87e2716544e19680b23ee75971336841de814d360e716e`，66 个成员；归档可读校验完成，未执行数据库恢复。
- 备份覆盖 `data`、`state`、`config.json`、`prompts`、`jsonl`、`images`、`logs`、`price_history`、原 `.env` 和 Compose；目录 0700、归档及回执 0400。
- 数据基线：5 个任务（3 启用）、2,369 个结果、38,405 个价格快照、4 个 app metadata、0 个黑名单规则。
- 原 `.env`、Compose、端口 8567→8000 和全部既有挂载不变。新增默认加载的 `docker-compose.override.yaml` 固定镜像并附加 `.env.ass`、只读 Key 文件；无需替换原配置文件。
- `.env.ass` 为 root 0600，保存非凭据 ASS 引用及独立随机 Web 会话签名密钥/只读机器 token；ASS Read Key 不放环境变量。没有将新 token 交给浏览器或外部客户端。
- 已有 SQLite/bootstrap 与本地代码一致；本次无需新增表结构迁移，不把历史 JSON 导入称为本次迁移。
- 约 19:53 新实例启动并核对镜像、旧业务环境、挂载、端口保持一致；沿用原任务启用状态和 cron。

## ASS 授权与读取

专用 Key 只包含 `secrets:metadata:read`、`secrets:value:read`，仅 REST、仅一个精确会话 UUID 的 object grant，无 name/write/producer 授权，不复用 MoviePilot 或采集 Key。通过既有业务 handler、tenant RLS、OperationContext、审计及幂等回执一次创建。

- 首次签发为90天期限；随后用户明确确认改为**永不过期**，已通过既有 `update_tenant_key` 业务操作将 `expires_at` 设为 `null`，revision 1→2。原token和文件不变，scope/REST/单条目授权不变，现有Key实测 access-status 200、expiry null、active grant 1。操作保留审计和独立幂等回执 `secrets/ass-key-permanent-20261019-receipt.json`；原签发回执保留历史，不覆盖。此次无需部署、重启或触发任务。
- `secrets/` root 0700；`ass-read-key` root 0400、普通文件，容器 `/run/secrets/ass-read-key` **只读**。精确引用及一次性 guard、签发回执亦为 0400；不要删除 guard 重发 Key。
- 实际授权读取：exact one active grant、schema 1 complete、version 2、130,282 bytes、一个来源/页面、16 个 Cookie（当时 13 个未过期）。无需开启 mixed 或 SameSite 归一。
- 部署后真实客户端首次读出现固定 `ass_unavailable`，后续正常读取成功；没有弱化 TLS、期限、身份、版本或授权检查，没有回退本地。来源故障不增加网站风控失败计数。
- 观察时间旧只提示，不当成已退出，也不刷新原时间。新包没有与旧本地状态相同类型的账号身份提示，未独立确认两者真实用户身份一致；`main` 标签不构成账号连续性证明。

## HTTPS、页面配置及访问控制

沿用已有 OpenWrt HTTPS 入口 `https://ialex.site:18567`，不改共享代理或入口；通过真实隔离 Chrome 打开正式登录页并输入现有管理员凭据，非手工注入 Cookie 的 HTTP 测试。

- `/health` HTTPS 200 / healthy。
- 无凭据 `/api/tasks` 401。
- 普通浏览器登录 200，得到 Secure、HttpOnly、SameSite=Strict 管理员 Cookie，页面及任务 API 正常。
- 独立机器 token 读任务 200，写任务入口 403；未调用有效任务的机器写操作。
- 从正式编辑弹窗将已有任务 8 改为 ASS，正常 PATCH 200，只有 `account_strategy` 改动。
- 原 3 页、图片分析、AI 模式、启用状态、cron 及其他任务均保持。无自动账号轮换、本地 fallback 或快照变更解锁。
- 19:57:10 从正式页面“启动”按钮提交一次，正常 start API 200；没有额外 CLI、debug limit、重复提交、人工补发通知或强制 AI 测试请求。

## 常规运行结果

本次普通运行约 **19:57:10–20:00:16 +08（约 185 秒）**，一个开始/完成记录，任务最终停止，FailureGuard 连续失败为 0、无暂停。未清空或人为修复 guard。

- 原上限仍是 3 页；处理第 1 页，进入第 2 页时未找到可用下一页按钮，按原程序分页条件结束。无分页点击/响应超时，没有人为缩减页数。
- 10 个已存在商品跳过；1 个新商品取得详情并提交后台分析。
- 实际下载 7 张图片；AI 首次调用响应验证通过，0 次重试。
- 新增 1 条结果，`analysis_source=ai`，结果为“不推荐”；新增 11 个市场价格快照。数据库合计 2,370 个结果、38,416 个快照，任务仍为 5 个、3 启用。
- 0 个 ASS 来源错误、登录失效或网站风控失败；旧观察提示未被伪造成新观察。
- 通知配置没有禁用；因结果不推荐，按原规则不发送通知。0 次通知发送/失败，**本轮没有实际通知投递证据**，也没有为了验收另发测试消息。

启动、读取、真实网页、图片下载、AI、结果持久化分别验证，不把零通知称为渠道投递成功；也不把任务成功当成已独立证明原/新账户身份相同。

## 后续维护

- 日常手工 Compose 操作需要同时保留原基线和默认 override。固定 digest 已在本机缓存，不能为了日常重启换回上游 `latest`。
- 正常任务配置只切换任务 8；其他任务仍维持原来源，未批量切换。
- 本Key已无固定到期日，不需要定期续期；仍可由管理员随时撤销，泄漏时应撤销/轮换。永不过期的是读取凭据，不是闲鱼登录态；登录态失效仍需重新登录并同步。Read Key 撤销只保证后续读取拒绝，不能撤回已授权浏览器请求。
- 回退前先停止 ASS 任务并核对策略兼容；旧程序可能将未知 `ass` 归为 `auto`，不可盲目降级或用数据库全量恢复覆盖新结果。
- 本地校验 195 非 live 测试、隔离原生 Chrome、前端构建仍是本地证据；线上业务以本记录实际运行结果为准。
