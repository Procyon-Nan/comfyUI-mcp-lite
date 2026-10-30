# 项目架构

## 服务职责

comfy-mcp-lite 将已有 ComfyUI 工作流提供为远程 MCP 工具。ComfyUI 负责模型、GPU 执行、任务队列及磁盘产物；本服务负责鉴权、工作流转换、输入参数替换、执行等待和图片返回。

工作流通过 ComfyUI 的 `/userdata` 接口读取，来源为其 `workflows/` 目录。仓库中的 `tests/fixtures/` 是测试数据，不是生产工作流目录。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `server.py` | 创建 ComfyClient、注册 MCP 工具、装配 StreamableHTTP 与鉴权中间件，启动 Uvicorn |
| `config.py` / `auth.py` | 读取环境配置、校验端口与令牌，对 HTTP 请求执行 Bearer 校验 |
| `tools.py` | 编排 list_workflows、run_workflow、get_image，校验字段地址并构造 MCP 返回内容 |
| `comfy.py` | 集中封装 ComfyUI REST 接口，解析历史错误及产物元数据 |
| `converter.py` | 封装 comfy-cli 的 UI → API 转换，缓存 object_info 300 秒 |
| `discover.py` | 纯函数实现字段发现、标签推断及工作流名称过滤 |
| `images.py` | 解析参考图 data URL、下载 HTTP 图片并上传到 ComfyUI |
| `progress.py` | 并发监听 WebSocket 和轮询历史记录，收集 PNG 帧并处理终态 |
| `image_cache.py` | 以 prompt_id 为键缓存 WS 图片，按 LRU 淘汰 |

## 请求流程

### 列出工作流

1. 通过 `/userdata` 获取工作流路径。
2. 根据请求头 `X-Comfy-Workflows` 过滤待展示名称。
3. 读取工作流，UI 格式转换为 API 格式，API 格式直接使用。
4. 从 API prompt 发现字段，返回各工作流的 prompts、numbers、images；空类别省略。

过滤只影响列表展示，执行和取图接口不受该请求头限制。服务使用共享 Bearer 令牌，当前没有用户级任务归属校验。

### 执行工作流

1. 校验工作流名称，读取并转换工作流。
2. 使用字段发现结果校验 `node_id.field` 地址，未知地址和跨类别地址报错。
3. 将参考图下载或解码后上传到 ComfyUI，替换图片、提示词及数值输入；未填写字段保持原值。
4. 为本次调用生成独立 `client_id`，提交 `/prompt`，再启动对应 WS 监听与历史轮询。
5. 完成后优先返回捕获的 WS 图片并写入缓存；无 WS 图片时从历史记录读取元数据，通过 `/view` 获取磁盘图片。
6. 返回首个文本块作为状态 JSON，图片以 MCP `ImageContent` 内联返回。

### 查询图片

`get_image` 不等待任务完成。它先检查进程内 WS 图片缓存，再查历史记录；没有历史记录时通过队列区分正在运行与不存在的任务。`index` 有效时返回指定图片，越界时返回全部图片。

## 字段规则

| 类别 | 支持的节点与字段 |
| --- | --- |
| prompts | `CLIPTextEncode.text` |
| numbers | `EmptyLatentImage`、`EmptyFlux2LatentImage`、`LatentUpscale` 的 `width`、`height` |
| images | `LoadImage.image` |

标签优先采用用户自定义节点标题。提示词其次根据与 `KSampler` / `KSamplerAdvanced` 的直接连线判断正负语义；尺寸和参考图使用类型默认标签。未识别语义的提示词标签为 null，字段仍可填写。

扩展节点时先核实实际节点 schema、转换结果及连线，复用现有发现、标签和校验逻辑。

## 状态与图片生命周期

- Converter 与 ImageCache 由工具注册闭包持有，属于当前服务进程。
- 每次调用的 `client_id` 独立，提交和 WS 监听共用，避免并发连接相互顶替。
- WS 收集器最多保留 16 帧，每帧不超过 32 MB；依据图片类型和 PNG 魔数筛选帧。
- 图片缓存最多保留 32 个任务、总计 256 MB，超限按 LRU 淘汰，重启后失效。
- 等待超时后监听任务被取消，run_workflow 返回 timeout，不缓存该次已收集的部分帧。仅 WS 输出且未缓存的图片无法通过后续磁盘查询补取；超时补取适用于具有可用磁盘产物的任务。
- 当前实现先提交任务再连接 WS，连接建立前发出的图片帧无法由该监听捕获。改动执行生命周期时需关注此时序。
- HTTP POST 使用 JSON 响应承载内联图片，配置位置为 `server.py` 的 `json_response=True`。

## 测试入口

离线服务测试使用 `tests/fake_comfy.py` 提供 HTTP/WS 接口。转换与字段发现见 `test_convert_discover.py`，工具协议见 `test_service.py`，WS 帧和缓存见 `test_ws_images.py`，并发隔离见 `test_client_id_isolation.py`。

假服务的自动 WS 任务在监听接入后完成；该测试时序不覆盖真实服务在监听建立前完成任务的情况。
