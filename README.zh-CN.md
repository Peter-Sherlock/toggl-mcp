<div align="center">

# toggl-mcp

**让 MCP 智能体完整操作你的 Toggl Track 工作区——计时、条目、项目、报表、整理，
共 35 个经过真实验证的工具。**

[![CI](https://github.com/Peter-Sherlock/toggl-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Peter-Sherlock/toggl-mcp/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-Official%20Python%20SDK-8A2BE2)
![License](https://img.shields.io/badge/License-MIT-3DA639)

**[English](./README.md)** | **[简体中文](./README.zh-CN.md)**

</div>

---

`toggl-mcp` 是一个本地 [Model Context Protocol](https://modelcontextprotocol.io) 服务器，
把 Toggl Track 封装成干净、对智能体友好的工具面。它替智能体隐藏了分页、ID 拼接、
时间戳格式化，以及 Toggl 新一代 Focus API 的各种坑，只暴露带明确成功/失败语义的
结构化输出——智能体可以自主规划并自我纠错，不需要人工粘合代码。

下面每一个工具都在真实 Toggl 账户上验证过；与公开文档不一致的上游行为均已实测
并记录（见[已验证的上游行为](#-已验证的上游行为)）。

## ✨ 功能总览

**35 个工具**——13 个只读、22 个写入。写入工具仅在显式开启后才存在
（fail-closed 设计，见[配置](#%EF%B8%8F-配置)）。

### ⏱ 时间跟踪

| 工具 | 说明 |
| --- | --- |
| `start_timer(description, project_id=None)` | 启动真实计时器；已有计时器在跑时拒绝而不是顶掉它 |
| `continue_timer(description)` | 按描述继续上次的计时，由上游恢复对应的项目/标签上下文 |
| `stop_timer()` | 停止当前计时器；没有计时器时是无副作用的幂等空操作 |
| `get_current_timer()` | 查询当前正在运行的计时器 |

### 📝 时间条目

| 工具 | 说明 |
| --- | --- |
| `get_time_entries(start, end)` | 查询时区感知区间内的全部条目，内部分页，带 `possibly_truncated` 截断标记 |
| `get_time_entry(entry_id)` | 按 ID 精确读取一条条目 |
| `create_time_entry(description, start, duration_seconds, ...)` | 回填已完成的条目，可带项目、标签、计费标记 |
| `update_time_entry(entry_id, ...)` | 部分更新——描述、项目、标签、开始时间、时长 |
| `delete_time_entry(entry_id)` | 删除一条条目（上游为软删除） |
| `restore_time_entry(entry_id)` | 恢复一条被软删除的条目 |

### 📦 批量操作

| 工具 | 说明 |
| --- | --- |
| `bulk_edit_time_entries(entry_ids, add_tags, remove_tags, project_id)` | 一次调用批量打标签和/或移动项目；逐条报告结果 |
| `bulk_delete_time_entries(entry_ids)` | 批量删除，分块执行并逐条确认；一条失败不影响其余 |
| `log_planned_entry(entry_id)` | 把日历计划原地转成已计时条目 |

### 📁 项目

| 工具 | 说明 |
| --- | --- |
| `list_projects()` | 列出工作区全部项目，内部分页 |
| `get_project(project_id)` | 完整详情：预算、起止日期、归档/完成状态、累计时长 |
| `create_project(name, ...)` | 可带客户、颜色、描述、私密性、计费标记和预算 |
| `update_project(project_id, ...)` | 部分更新——名称、客户、颜色、描述、私密性、计费、预算 |
| `set_project_archived(project_id, archived)` | 归档 / 取消归档 |
| `set_project_completed(project_id, completed)` | 标记完成 / 重新打开 |
| `duplicate_project(project_id, name=None)` | 复制项目，可指定新名称 |
| `delete_project(project_id)` | 删除项目（时间条目保留，仅解除关联） |

### 👥 客户与标签

| 工具 | 说明 |
| --- | --- |
| `list_clients()` / `create_client(name)` | 列出、创建客户 |
| `update_client(client_id, name)` | 重命名客户 |
| `delete_client(client_id)` | 删除客户 |
| `list_tags()` / `create_tag(name)` | 列出、创建标签 |
| `update_tag(tag_id, name=None, color=None)` | 重命名或改色 |
| `delete_tag(tag_id)` | 删除标签 |

### 📊 报表与搜索

| 工具 | 说明 |
| --- | --- |
| `summarize_time(start, end, group_by, project_id=None, user_account_id=None)` | Toggl 原生报表引擎：按项目 / UTC 日期 / ISO 周 / 标签分组统计，可按项目和/或成员过滤 |
| `search(keyword, per_group=5)` | 跨时间条目、任务、项目的统一搜索 |
| `list_planned_entries(start, end)` | 尚未转成实际计时的日历计划条目 |

### 🏢 工作区

| 工具 | 说明 |
| --- | --- |
| `get_me()` | 当前用户的设置，包括在 Toggl 里选中的工作区 |
| `list_workspace_members()` | 组织成员及其所属工作区 |
| `list_tasks(project_id)` | 项目任务——需要开通任务功能的 Toggl 套餐（其他套餐会干净地返回 404） |

## 🚀 快速开始

前提条件：[uv](https://docs.astral.sh/uv/)、Python 3.11+（由 uv 管理）、带 API 密钥的
Toggl Track 账户。

```bash
git clone https://github.com/Peter-Sherlock/toggl-mcp.git
cd toggl-mcp
uv sync
copy .env.example .env        # PowerShell；macOS/Linux 用 cp
# 编辑 .env —— 见下一节
```

先做一次只读冒烟验证（绝不修改账户数据）：

```bash
uv run --env-file .env python scripts/verify_account.py
```

以 stdio 方式启动服务器：

```bash
uv run --env-file .env toggl-mcp
```

## ⚙️ 配置

服务器完全通过环境变量配置（见 [`.env.example`](./.env.example)）：

| 变量 | 必填 | 默认值 | 说明 |
| --- | :---: | --- | --- |
| `TOGGL_API_KEY` | ✅ | — | Toggl 2.0 API 密钥，在 **Track → Profile → API Token** 生成，以 `toggl_sk_` 开头 |
| `TOGGL_ORGANIZATION_ID` | ✅ | — | 组织 ID（整数） |
| `TOGGL_WORKSPACE_ID` | ✅ | — | 工作区 ID（整数） |
| `TOGGL_ENABLE_WRITE_TOOLS` | — | `false` | **fail-closed 写入门禁**。为 `false` 时，22 个写入工具根本不会注册——在 `tools/list` 里完全不可见 |
| `TOGGL_TIMEOUT_SECONDS` | — | `10` | 每个请求的 HTTP 超时（1–60 秒） |

查找 ID：在浏览器打开 Toggl Track 并进入你的工作区，工作区 ID 和组织 ID 就是页面
URL 里的整数（`track.toggl.com/...workspaces/<workspace_id>...`、
`.../organizations/<organization_id>/...`）。任一 ID 填错时，
`scripts/verify_account.py` 会立刻以认证错误失败，便于排查。

> ⚠️ **安全说明。** API 密钥只会发送到固定的上游主机（`focus.toggl.com`），该主机
> 刻意不可配置。绝不提交 `.env`；错误信息会自动脱敏密钥。推荐做法：平时关闭写入
> 工具，只在需要的客户端里开启。

## 🔌 接入 MCP 客户端

### 通用 stdio 客户端（Claude Desktop、ZCode 等）

```json
{
  "mcpServers": {
    "toggl-track": {
      "command": "uv",
      "args": ["run", "--env-file", ".env", "python", "-m", "toggl_mcp.server"],
      "cwd": "/path/to/toggl-mcp"
    }
  }
}
```

### Codex

在工作区根目录旁放置项目级 `.codex/config.toml`：

```toml
[mcp_servers.toggl_track]
command = 'uv'
args = ["run", "--frozen", "--env-file", ".env", "python", "-m", "toggl_mcp.server"]
cwd = '/path/to/toggl-mcp'
startup_timeout_sec = 30
tool_timeout_sec = 60

[mcp_servers.toggl_track.env]
TOGGL_ENABLE_WRITE_TOOLS = "true"
```

`enabled_tools` 可以钉死一份精确的工具白名单；本仓库自带漂移测试
（`tests/test_codex_config.py`），白名单与实际注册工具面不一致时测试会失败。

## ✅ 验证

```bash
uv run pytest          # 96 项离线测试（httpx.MockTransport——绝不触碰 Toggl）
uv run ruff check .    # 代码风格
uv run mypy            # 严格类型检查
```

CI 在每次 push 和 PR 时运行全部三项。协议测试通过真实 MCP 边界
（`Client` ↔ `MCPServer`，校验 structuredContent），`scripts/verify_mcp.py`
则会启动真实 stdio 子进程核对完整的 35 个工具面。

## 🔍 已验证的上游行为

Toggl 新一代 Focus API（`focus.toggl.com/api`——唯一接受 `toggl_sk_` 密钥的主机）
与公开文档存在多处偏差，本客户端已替你处理。以下要点均经真实账户验证：

<details>
<summary><strong>时间条目与计时</strong></summary>

- 创建必须带 `type` 字段；标签只能通过 `tag_ids` 附加（字符串 `tags` 被静默忽略），
  因此工具入参的标签名会先在工作区内解析。
- 单条目的 PUT 路由是全量替换、返回空 204、并静默忽略项目变更——更新和批量编辑
  都走部分更新的 `PATCH` 路由，并重读确认。
- `PATCH /time-entries/bulk-edit` 接受 `{ids, changes}`，其中 `changes.tag_ids`
  是三态（缺省 = 不动，列表 = 设置，`[]` = 清空）。
- `DELETE /time-entries/bulk?ids=<csv>` 返回空 204，且 batch 读取会静默省略已删除
  的 ID——批量删除按 100 个 ID 分块并逐条确认。
- 单条删除是软删除：`PATCH /time-entries/{id}/restore` 可以恢复。
- `POST /tracking/start-from-description` 必须带 `{name, extension_source, type}`
  （通过上游校验报错逐步发现）。
- 计划（日历）条目没有工作区 token 可达的专属路由；它们混在共享的区间端点里，
  带 `planned_start`/`planned_duration` 而没有 `start`。该端点在 `per_page` 超过
  100 时会静默返回空页。

</details>

<details>
<summary><strong>项目、客户与标签</strong></summary>

- 项目的 `active` 标志是只读的遗留噪声——连正常使用中的项目也是 false——所以
  智能体看到的是有意义的 `archived` 状态（来自 `archived_at`）。
- 项目创建的键是 `private`（不是 `is_private`），且没有 `active` 字段；项目 PUT
  是全量替换、会重置未提及的字段（已实测），因此更新走部分 PATCH 路由。
- `POST .../complete` 返回 `{project}` 信封，`POST .../uncomplete` 返回裸对象；
  `POST .../duplicate` 尊重显式命名并分配新颜色。
- `.../clients/{id}` 与 `.../tags/{id}` 对 PATCH 回 405——动词是 PUT。
  `PUT clients/{id}` 必须带 `name` 且静默忽略归档字段，所以 `update_client`
  只提供重命名。客户的 `archived` 由上游 `active` 取反派生。

</details>

<details>
<summary><strong>报表与搜索</strong></summary>

- `POST /reports/workspaces/{wid}/query` 支持 `project_id`、`start_date`、
  `tag_ids`、`user_account_id` 分组和 `sum(duration)`（秒）聚合；空结果是
  `{}`，没有 `data_json_row` 键。运行中的条目计 0 秒；计划条目不计入。
- 过滤器的等值操作符必须是 `"="`；`project_id` 和 `user_account_id` 过滤已实测，
  而标签过滤被上游以各种形态拒绝——因此未提供（标签维度请用 `group_by="tag"`）。
- 原生 `week` 分组只返回没有年份的周数字；周汇总改为客户端按无歧义的 ISO
  `YYYY-Www` 标签聚合（与原生行交叉验证一致）。
- 分组行显式分页并带安全页数上限；显式 `count` 聚合被上游拒绝（每行本来就免费
  带 count）。
- 搜索返回的是建议式分组：项目命中带 ID，时间条目命中是去重后的描述（无 ID）——
  请用 `get_time_entries` 解析出精确条目。

</details>

## 🗺 路线图

- [ ] 端到端智能体验证：脚本化真实客户端会话，依据观察到的智能体行为打磨工具描述
- [ ] 项目批量归档/恢复、置顶
- [ ] 可选的 PyPI 发布，支持 `uvx` 一键安装

刻意不做：任务 CRUD（付费功能）、计费费率与利润率、工时审批、管理面、CSV 导入、
日历集成、webhooks。

## 许可证

[MIT](./LICENSE) © Peter Wang
