# Goofish publish-only CI 与只读版本计划

## 安全边界

- 唯一自动构建/发布入口是 `.github/workflows/ci.yml`。`delivery.yml`、`docker-image.yml` 仅保留手动触发的弃用提示，没有发布、部署或凭据操作；`claude.yml` 不变。
- 所有分支 push 和 PR 执行真实非 live 后端测试、前端锁文件安装/构建、Linux amd64 release image 构建、OCI source/revision/platform 校验、Compose 模板解析与隔离 MCP smoke。
- 只允许固定仓库 `ColorlessCube/ai-goofish-monitor` 的 **master 非删除 push** 发布，与 ASS 的固定默认分支 publish-only 契约一致。master 发布中途不会因新 push 被 concurrency 取消；普通分支、PR、其他仓库和删除事件不发布。本次只读核对发现 master 未配置 branch protection；因此不添加会让发布永久跳过的 `github.ref_protected` 前置条件，也不擅自修改远端保护规则。将来启用分支保护需要单独授权。
- image job 内只构建一次，所有镜像检查通过后发布同一本地 `ai-goofish-monitor:tested` 镜像，不重建、不用大镜像 Artifact 中转。没有任何必需或可选 Artifact；日志和经审核 toolkit 输出构成清单通道。
- 没有 deploy job，没有 CI-to-NAS WireGuard/SSH，不修改 GitHub settings/secrets 或 NAS。源码改变不等于 GitHub 已注册工作流已停用：本次不改 registered workflow 状态，也不触发 run。

## 容器验证契约

CI `docker run --network none`，不开放端口、不挂载宿主机路径。客户端以 `docker exec` 在同一容器通过 `http://127.0.0.1:8000/api/mcp` 验证，故没有生产地址或外部爬虫/通知网络。SQLite 与回执均只在可删除容器文件系统中。

只在运行时生成不同的随机 read/write token（每项 `secrets.token_urlsafe(48)`），写入 runner masking 和环境；绝不进入 build arguments、镜像 labels 或镜像层。授权范围为：

```text
GOOFISH_MCP_ENABLED=true
GOOFISH_MCP_ALLOWED_HOSTS=127.0.0.1,localhost
GOOFISH_MCP_WRITE_SCOPES=tasks:read,tasks:write,tasks:delete,system:read
```

`GOOFISH_MCP_READ_TOKEN`、`GOOFISH_MCP_WRITE_TOKEN` 由 smoke CLI 通过环境读取。smoke 脚本使用 disabled、无 cron 的 keyword fixture；应验证初始化、工具列表、CRUD/readback、同 request ID 幂等、过期 revision 拒绝及仅删除自建 fixture。它不得启动任务、AI、爬虫或发送通知。主流程会先验证健康页和真实前端可访问，任何失败都会阻止发布；最后 `always()` 清理容器。

`Dockerfile.release` 安装当前源码中的 `requirements-runtime.txt`，避免仅依赖基础镜像内历史依赖。现有基础镜像来源未升级；基础镜像的浮动 tag 不等于输出 release 镜像可变，部署输入始终是最终 digest。`.dockerignore` 排除运行 `.env`、账号状态、SQLite/WAL 和 toolkit checkout。

## 私有 GHCR 与不可变清单

沿用经审核 toolkit commit：

```text
13ae397ae0e116eeac0bf388c40f4b12a7f82855
```

`scripts/publish_image_manifest.py` 在执行前额外核对该版本 `infra/deploy/publish.py` 的 SHA-256：

```text
8797dc8e806853bfc3b0df039bdeef49deb9110a2f02f9dfa08b4061b81a2b9e
```

重要区别：该 toolkit 原生支持将 package visibility PATCH 为 private；本仓库 wrapper 禁止所有非 GET package API，并用只读、fail-closed 的检查替换 privacy repair。固定 package 必须**已经存在且 private、类型 container、关联固定仓库**，元数据不可读也必须失败。不会创建新 package 或修复可见性。

toolkit 负责检查 linux/amd64、OCI revision/source，拒绝已有 `build-<完整40位SHA>` tag，tag/push 已测镜像并取得 digest；wrapper 再次核对 package privacy、版本 tag 和 digest 后才输出清单。push 成功后任一验证失败，必须调查并用**新提交**重新发布，不可重跑覆盖同一 immutable tag。

清单顶层精确字段：`schema, service, repository, commit, run_id, baseline, images`。

- `schema` 必须是整数 1，不能是 bool。
- `service=ai-goofish-monitor`，`repository=ColorlessCube/ai-goofish-monitor`。
- commit 为小写完整 40 位 SHA，run ID 为规范正整数字符串，必须绑定当前运行。
- `images` 只能有 `AIGOOFISH_IMAGE`，其值必须为 `ghcr.io/colorlesscube/ai-goofish-monitor@sha256:<64位小写hex>`。
- 拒绝重复 JSON key、多余字段、错误镜像仓库、mutable tag、非规范值及超过 16 KiB 的 manifest。
- `baseline` 是 toolkit 计算的 Compose 文件哈希摘要，**不证明生产数据/迁移兼容**。

`GOOFISH_IMAGE_MANIFEST <sha256> <canonical-json>` 是唯一日志记录；hash 对排序且无空白的 canonical JSON 字节计算，与 toolkit `handoff.py export` 输出一致。hash 用于完整性，不是独立身份认证。planner 必须同时验证 GitHub 来源。

## 本地只读计划器

从审核后的 checkout 执行（需已有 GitHub CLI 只读访问授权）：

```sh
python3 scripts/manual_image_plan.py --plan --run-id <RUN_ID>
```

计划器只有 GET：不支持 `--apply`、`--inspect-nas`、任意仓库/工作流、手工清单输入或 Artifact fallback。它核对：

1. 固定仓库、`ci.yml`、成功完成的 master push、规范 SHA/run ID。
2. 最新 attempt 的唯一成功 `image` job；job 的 run/attempt/SHA 必须一致；不完整或重复 job 列表失败。
3. 此 job 原始日志中的唯一 bounded marker（允许 GitHub 时间戳）；重复 marker 即使相同也拒绝，验证 canonical JSON/hash。原始日志、异常正文和下载 URL 不打印。日志读取上限 8 MiB。
4. 固定 package 的 private 元数据及 `build-<SHA>` 对应 digest；有限分页，无法确认时失败，不修改可见性。
5. 在读取日志以及读取 registry 后重新核对 CI 来源，避免接受已过期 attempt。

日志过期、缺失、私有 metadata 无权限、错误来源或 hash 均不能生成可信计划。输出 `mode=plan-only`、`deployment_performed=false`、`confirmation_required=true`，`current=null`，因为它根本不读取生产。计划不证明镜像可在 NAS 成功 pull，不证明迁移/备份完成，也不是部署授权。

实际部署前还需单独批准：比较运行代码和已部署版本、验证备份、审查数据库/配置迁移兼容、保存旧 digest 和回滚方案。

## 本地测试与待验证范围

```sh
python3 -m pytest tests/unit/test_ci_delivery*.py -q
python3 scripts/check_ci_contract.py
python3 -m pytest -m 'not live' -q
(cd web-ui && npm ci --no-fund && npm run build)
git diff --check
```

Python 测试环境需已有项目依赖及 PyYAML；CI 显式使用 `--with PyYAML==6.0.3`，planner/publisher 的运行实现仅依赖标准库。契约测试包括 manifest 边界、重复/过大日志、工作流/attempt 来源、private registry 检查、无副作用 CLI、发布上下文和危险 YAML 变更；以 RED→GREEN 分片完成。静态契约不是 Docker 或 GitHub 实跑证明。

本次 Mac 无 Docker CLI。真实 Linux 镜像构建、provenance、Compose 解析、隔离容器 smoke、GHCR 发布和真实 job 日志格式/权限，均须在另外批准的 GitHub CI 运行中验证；没有用生产或本地 NAS Docker 替代。本次没有 commit、push、publish、触发 Actions 或生产写入。开发期间仅只读核对了选定的生产容器元数据，没有输出完整 Env 或凭据；CI 本身不连接 NAS。最新本地验证与待授权事项见 `native-mcp-verification.md`。
