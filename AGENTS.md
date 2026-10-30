# 项目协作规范与文档索引

## 代码结构

```text
comfyUI-mcp-lite/
├── AGENTS.md                          # 文件布局、全项目规则与文档入口
├── README.md                          # 项目介绍、配置、工具协议、接入与部署
├── CHANGELOG.md                       # 项目版本与历史变更
├── LICENSE                            # MIT 许可证
├── pyproject.toml                     # Python 依赖、版本、命令入口与 pytest 配置
├── .gitignore                         # 构建产物、虚拟环境与缓存忽略规则
├── doc/
│   ├── architecture.md                # 模块边界、工具流程与图片生命周期
│   └── development.md                 # 环境准备、验证、部署与临时文件
├── deploy/
│   └── comfy-mcp-lite.service          # systemd 部署模板与环境文件说明
├── src/
│   └── comfy_mcp_lite/
│       ├── __init__.py                # 包说明与服务版本
│       ├── __main__.py                # python -m comfy_mcp_lite 入口
│       ├── server.py                  # ASGI 应用装配、MCP 注册与 Uvicorn 启动
│       ├── config.py                  # 环境变量读取、令牌与端口校验
│       ├── auth.py                    # HTTP Bearer 鉴权中间件
│       ├── tools.py                   # 工作流列表、运行与取图三个 MCP 工具
│       ├── comfy.py                   # ComfyUI REST 客户端与历史结果解析
│       ├── converter.py               # UI → API 工作流转换与 object_info 缓存
│       ├── discover.py                # 可填字段发现、标签推断与列表过滤
│       ├── images.py                  # 参考图 data URL 解析、HTTP 下载与上传
│       ├── progress.py                # WS 事件与 PNG 收集、历史轮询及超时收尾
│       └── image_cache.py             # 按任务 ID 存储 WS 图片的进程内 LRU 缓存
├── tests/
│   ├── conftest.py                    # 真实工作流与节点信息夹具加载
│   ├── fake_comfy.py                  # 用于离线测试的 ComfyUI HTTP/WS 服务
│   ├── test_auth.py                   # Bearer 鉴权与非 HTTP scope 透传
│   ├── test_config.py                 # 配置默认值、覆盖与非法输入
│   ├── test_convert_discover.py        # 工作流转换、字段发现与标签规则
│   ├── test_service.py                # MCP HTTP 调用、运行、参考图与双源取图
│   ├── test_headers_filter.py         # X-Comfy-Workflows 过滤与协议行为
│   ├── test_ws_images.py              # WS 帧解析、收集上限与图片缓存
│   ├── test_client_id_isolation.py     # 调用级 client_id 与并发图片隔离
│   └── fixtures/
│       ├── simple.ui.json             # 从实际 ComfyUI 导出的 UI 工作流
│       └── object_info.json           # 对应节点 schema 快照
└── .venv/                             # 本地 Python 虚拟环境（Git 忽略）
```

## 全项目规则

### 代码与设计

- 拒绝防御性编程和防御性设计。围绕已确认的需求、类型和接口契约实现，不为假设中的异常叠加判空、兜底、重试、兼容分支或抽象层。
- 在实际输入边界处理明确的校验和失败场景；内部代码遵循已建立的契约，不重复检查同一条件。契约被破坏时暴露具体错误，修复问题来源，不用默认值、空结果或静默捕获掩盖问题。
- 禁止大块、多重嵌套的三元表达式，尤其是 JSX 中连续嵌套的条件渲染。多分支使用清晰的 `if`、提前返回、具名函数或职责明确的组件；简单二选一可以使用单层三元表达式。
- 优先保证代码可读性。变量、分支和组件应直接表达业务含义，不为压缩行数将多个判断和操作挤在一起。

### 说明与表达

- 本节适用于用户沟通、项目文档、代码注释和界面文案。
- 直接陈述实现、行为、结果和依据。禁止使用“这不是……”“这不代表……”等防御性、甩锅式说明，不添加与当前问题无关的免责声明。
- 需要说明能力范围时，用具体事实表达，例如“任务在当前页面内计时，关闭页面后暂停”，避免用免责式否定句代替行为说明。
- 不使用“可能”“大概”“也许”“未必”等模糊措辞替代核实。技术结论应以源码、文档、运行记录或实际验证为依据。
- 尚未核实的内容明确写“尚未核实”，说明待核实的具体事项并继续核实；需要用户决定的需求直接提出问题。不得把推测写成确定结论。

## 专项文档索引

| 工作内容 | 对应文档或实现入口 |
| --- | --- |
| 了解模块边界、执行流程、字段规则与图片生命周期 | [项目架构](doc/architecture.md) |
| 准备环境、选择验证方式、核对部署与存放临时文件 | [开发与部署](doc/development.md) |
| 了解项目用途、安装与启动方式 | [README：快速开始](README.md#快速开始) |
| 配置 ComfyUI 地址、HTTP 监听和 Bearer 令牌 | [README：环境变量](README.md#配置环境变量)、[配置实现](src/comfy_mcp_lite/config.py) |
| 查看三个工具的参数、状态和图片返回约定 | [README：工具](README.md#工具)、[返回格式](README.md#返回格式说明)、[工具实现](src/comfy_mcp_lite/tools.py) |
| 接入 MCP 客户端、按请求头筛选工作流 | [客户端接入](README.md#客户端接入)、[工作流过滤](README.md#按客户端过滤工作流) |
| 理解 UI/API 转换、节点信息缓存与字段白名单 | [工作流格式](README.md#工作流格式说明)、[转换实现](src/comfy_mcp_lite/converter.py)、[字段发现](src/comfy_mcp_lite/discover.py) |
| 修改执行等待、WS 取图和缓存 | [等待与帧收集](src/comfy_mcp_lite/progress.py)、[图片缓存](src/comfy_mcp_lite/image_cache.py)、[相关测试](tests/test_ws_images.py) |
| 核对并发调用与图片隔离 | [并发隔离测试](tests/test_client_id_isolation.py)、[假 ComfyUI](tests/fake_comfy.py) |
| 运行开发验证、排查协议行为 | [README：开发](README.md#开发)、[服务级测试](tests/test_service.py)、[项目配置](pyproject.toml) |
| 部署 systemd 服务、处理迁移期端口冲突 | [README：部署](README.md#部署b-机)、[端口冲突](README.md#端口冲突迁移期)、[服务单元模板](deploy/comfy-mcp-lite.service) |
| 查阅版本和历史行为变更 | [CHANGELOG](CHANGELOG.md) |

全项目适用的项目规则直接维护在本文件；仅适用于特定领域的说明与规范放入 `doc/`，按当前任务阅读对应文档。已有全局规范不在此重复。
所有临时方案文件、脚本等不应该提交到远端仓库的内容放入`tmp/`。
