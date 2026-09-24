# CI/CD 与 NAS 接入

## 当前交付边界

`master` 的每次推送会运行：Python 非 live 测试、Vue 前端构建、带源码 provenance label 的 release image 构建，以及 NAS Compose 模板解析。通过后，`Deliver main to NAS` 会将同一份已测试镜像发布为私有 GHCR 的不可变 `build-<完整 commit SHA>` 标签，并上传包含固定 digest 和 Compose 基线的部署 manifest。

当前仓库没有启用 `NAS_DEPLOY_ENABLED=true`，因此不会连接 NAS、不会替换当前闲鱼监控容器，也不会修改 SQLite、登录 state、Cookie 或监控任务。该开关和 NAS 端的 service policy 都必须独立启用，双重条件缺一不可。

## 共享工具链

发布和 NAS 连接使用固定的 `ColorlessCube/nas-deploy-toolkit` commit `e418193f998d9772cb2dc0db704e81d44bfb0ee9`。workflow checkout、可复用 workflow 引用和 `toolkit_ref` 输入必须保持为相同的完整 SHA；不要改成分支名或浮动标签。

## NAS onboarding（尚未执行）

启用部署前需要单独完成并验证：

1. 为此仓库创建独立 WireGuard peer、NAS 受限 SSH 用户 `deploy-ai-goofish` 和 GitHub secrets。
2. 由 NAS 管理员安装固定 service policy，策略必须默认 `enabled: false`，只允许本仓库 `master` 的 delivery workflow 和 `backend`、`frontend`、`image`、`publish` 必需 jobs。
3. 固定 `/volume4/docker/ai-goofish-monitor` 的 Compose 基线、8567 端口映射及持久目录；先备份 `data`、`state`、`config.json`、`prompts`、`jsonl`、`images`、`logs`、`price_history`。
4. 先执行 plan-only 与隔离验收；确认回执、旧镜像恢复和运行数据无变化后，才允许将两端开关设为 true。

生产部署只能使用 manifest 中的 `@sha256:` digest，不能使用 `latest`。
