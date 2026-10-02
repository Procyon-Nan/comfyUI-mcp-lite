# comfy-mcp-lite

远程可用的轻量 ComfyUI MCP 服务器：**用已有工作流生图**。

任何端侧 agent（Hermes、ChatLuna/Koishi、Claude……）连上就能用，
ComfyUI 与 agent 不必同机。

- 端点：`http://<B 机>:9101/mcp`（StreamableHTTP），Bearer 鉴权
- 工具：`list_workflows` / `run_workflow` / `get_image`
- 图片一律 base64 字节内联返回，不暴露 ComfyUI 地址

## 快速开始

```bash
python3 -m venv .venv
# 国内网络建议用镜像直连（不要同时挂代理）：
# .venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e ".[dev]"
.venv/bin/pip install -e ".[dev]"     # 依赖含 comfy-cli（仅作库用其 UI→API 转换器）
export MCP_AUTH_TOKEN=<长随机串>
.venv/bin/comfy-mcp-lite            # 或 .venv/bin/python -m comfy_mcp_lite
```

未设置 `MCP_AUTH_TOKEN` 时拒绝启动（共享服务禁止裸跑）。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI 地址（服务器与它同机，默认回环） |
| `MCP_HOST` | `0.0.0.0` | HTTP 绑定地址（跨机访问） |
| `MCP_PORT` | `9101` | HTTP 端口 |
| `MCP_AUTH_TOKEN` | 无（必设） | 客户端 → 本服务器的 Bearer 校验 |

## 工具

### list_workflows（无参）

列出可用工作流及每个工作流能填什么：

```json
{"simple": {"prompts": {"78.text": {"label": "正面提示词"}, "79.text": {"label": "负面提示词"}},
            "numbers": {"80.width": {"label": "宽度"}, "80.height": {"label": "高度"}}}}
```

- 空类省略键（无参考图入口的工作流不出现 `images`）
- `label` 为 null 表示未识别出语义，地址仍可直接调用
- 不填的字段运行时用工作流原值

### run_workflow

```json
{"workflow": "simple",
 "prompts": {"78.text": "masterpiece, 1cat, blue hair, garden"},
 "numbers": {"80.width": 768, "80.height": 1024},
 "timeout_seconds": 60}
```

- 地址必须是该工作流真实存在的 `node_id.field`（即 `list_workflows` 报出的），否则报错
- `images` 的值仅两种：内联 `data:image/png;base64,...` 或 http(s) URL（服务器代下载后上传）
- 默认等到结果：成功内联返回图片；超时返回 `{"status":"timeout","prompt_id":...}`（不含图），稍后用 `get_image` 补取

### get_image

```json
{"prompt_id": "…", "index": 0}
```

立即返回、绝不等待：已完成回图（`index` 越界返回全部）；未完成返回 `{"status":"running"}`；
无任务/无图给出明确错误。

## 返回格式说明

图片通过 MCP 原生 `ImageContent`（base64）内联返回；每个工具结果的第一块
文本是状态 JSON（`status` / `prompt_id` / 图片数）。

## 客户端接入

Hermes：

```
hermes mcp add comfy --url http://<B 地址>:9101/mcp --auth header
```

ChatLuna（chatluna-agent 插件）：连接类型 HTTP，服务 URL `http://<B 地址>:9101/mcp`，
请求头 `{"Authorization": "Bearer <MCP_AUTH_TOKEN>"}`，工具调用超时建议 120 秒。

## 工作流格式说明

ComfyUI `userdata/workflows/` 里的文件是 **UI 格式**，`POST /prompt` 只吃
**API 格式**。服务器用 comfy-cli 的 `convert_ui_to_api`（`comfy_cli.workflow_to_api`，
仅作库 import，不跑其 CLI）在运行期转换；已是 API 格式的文件自动直通。

`object_info` 在进程内缓存 300 秒：ComfyUI 装节点或重启后，最多 5 分钟生效。

## 部署（B 机）

```bash
sudo cp deploy/comfy-mcp-lite.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now comfy-mcp-lite
```

单元文件默认 venv 路径 `/opt/comfy-mcp-lite`，部署时把仓库拷过去并建 venv，
`MCP_AUTH_TOKEN` 写入 `/etc/comfy-mcp-lite.env`（见 unit 内注释）。

## 开发

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest            # 离线测试：转换冒烟、字段发现、鉴权、配置、
                            # 服务级端到端（假 ComfyUI，走轮询兜底路径）
```

`tests/fixtures/` 存放从 .98 导出的 `simple.ui.json` 与 `object_info.json`，
测试基于真实数据。

## 端口冲突（迁移期）

旧 `comfy-mcp-bridge` 占用 9101。验收期间新服务可用 `MCP_PORT=9102` 临时起，
验收通过退役旧桥后切回 9101。
