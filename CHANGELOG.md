# Changelog

## 2026-10-04 version:0.3.0

- pending: 移除 v0.2 引入的图片签名 URL 功能（ChatLuna 客户端已自行
  转存图片并以 text 形式给出链接，urls 字段无人使用，且随签名 TTL
  1 小时过期）：删除 GET /images/{prompt_id}/{index} 端点与 signing.py，
  run_workflow / get_image 完成态文本 JSON 不再附带 urls 字段
  （status / prompt_id / images 保留，内联 base64 图块不变）；移除配置项
  PUBLIC_BASE_URL 与 IMAGE_URL_TTL_SECONDS 及其校验；build_app 回退为
  Bearer 鉴权中间件直接包住 /mcp 应用（不再需要 Starlette 顶层路由，
  pyproject 相应去掉显式 starlette 依赖，由 mcp SDK 传递提供）；移除
  相关测试（/images 端点规格 8 条、PUBLIC_BASE_URL 配置校验、urls 断言）。
  版本 0.2.0 → 0.3.0。

- pending: 三个 MCP 工具（list_workflows / run_workflow / get_image）的
  工具描述与参数描述统一改为「简明英文 + 中文直译注释」形式：英文在前，
  中文紧随并以 (中文：……) 包裹，工具描述按段组织；run_workflow 与
  get_image 的每个参数改用 typing.Annotated[<type>,
  Field(description=...)] 声明，schema 中每个参数均携带 description
  （参数名、类型与默认值保持不变，未设置中文 title）。仅改描述文案，
  功能行为与测试断言均不变。

- pending: 类型默认 label 的 5 个代码内常量（正面提示词/负面提示词/宽度/
  高度/参考图（图生图底图））改为「简明英文 + (中文：直译)」格式，与工具/
  参数描述的风格统一（Positive prompt / Negative prompt / Width / Height /
  Reference image (img2img base)）；用户自定义标题的处理逻辑与优先级链
  不变（节点标题 → 类型+连线 → 类型默认 → null）。discover 顶部 docstring、
  tools.py list_workflows 示例、README 示例与说明、测试中的类型默认断言
  同步更新，用户自定义标题相关断言与 fixture 保持原样。版本仍为 0.3.0。

- pending: 新增双源取图（WS 优先、磁盘兜底），支持输出节点为
  SaveImageWebsocket（custom_nodes/websocket_image_save.py，不落盘、
  成品 PNG 经进度 hook 推给 WS）的工作流：progress.py 的 WS 监听不再
  丢弃二进制帧，按 ComfyUI 协议解析（帧 = >I event_type + 数据，
  PREVIEW_IMAGE(1)/UNENCODED_PREVIEW_IMAGE(2) 的数据 = >I image_type +
  图片字节），只收集 image_type=2 且带 PNG 魔数的成品帧（上限 16 帧 /
  单帧 32MB，超限按长度剪枝丢弃），websockets max_size 由 16MB 放宽到
  32MB+8 以容纳完整 PNG；wait_for_completion 返回值由状态字符串改为
  Completion(status, ws_images)，且成功收尾时对 WS 监听做最长 0.5 秒的
  宽限排水（轮询抢先判定终态时成品帧可能仍在接收队列，等监听收到终态
  JSON 事件自然退出可确保帧收全）。tools.py 的 run_workflow 改为 WS 帧
  优先出图（每帧一张、按捕获顺序，并写入内存缓存），WS 无图回退原
  history/SaveImage 磁盘路径，两路皆空才报「任务已完成但未产出图片」，
  完成态文本 JSON 结构不变（仍为 status/prompt_id/images）。新增
  image_cache.py：prompt_id → PNG 字节列表的进程内 LRU 缓存（上限
  32 条 / 256MB），get_image 命中即直接内联返回（index 语义与磁盘路
  一致），未命中回退磁盘；缓存属进程内存，服务重启即失效（已知可接受
  权衡，磁盘路不受影响）。tests/fake_comfy.py 增加可选 /ws（构造传
  ws=True：auto 任务等 WS 客户端接入时完成，按真实时序推成品帧 →
  落 history → 发终态事件；ws_images/diskless 两个开关模拟成品帧推送与
  不落盘），make_png 用于生成可区分的贴图。新增测试 17 项（帧解析 3、
  收集器 3、wait_for_completion 集成 1、ImageCache 5、服务级双源优先级
  /兜底/双空报错/缓存命中 5），共 55 项。README 取图说明同步。版本仍为
  0.3.0。

## 2026-10-02 version:0.2.0

- pending: 新增图片签名 URL：GET /images/{prompt_id}/{index}[.ext]?e=&s= 端点
  免 Bearer（发图组件不带 Authorization 头），鉴权走查询串 HMAC-SHA256
  签名（key=MCP_AUTH_TOKEN，message="prompt_id:index:e"，e 为 unix 过期
  时间），校验失败/过期 403，任务无记录或下标越界 404，实时代理 ComfyUI
  /view 返回图片字节（.ext 后缀仅识别用，取点前数字作 index）；新增
  signing.py（签名生成/校验共用同一算法，compare_digest 防时序侧信道）。
  run_workflow / get_image 完成态文本 JSON 在设置 PUBLIC_BASE_URL 时附带
  urls（顺序与内联图块一致，get_image 选定 index 时用产物全集原始下标），
  内联 base64 图块保持不变（Hermes 兼容）。新增配置 PUBLIC_BASE_URL（须
  http(s):// 开头，未设则不出现 urls，行为与 v0.1 一致）与
  IMAGE_URL_TTL_SECONDS（默认 3600）。build_app 改为 Starlette 顶层路由
  （/images 免鉴权，其余含 /mcp 走 Bearer 中间件，顶层接管内层 lifespan
  启动会话管理器），ComfyClient 由 build_app 创建并在工具与图片端点间共享。
  版本 0.0.1 → 0.2.0；补 v0.2 规格 8 条测试与配置校验测试（共 53 项）。
- pending: numbers 字段来源改为「latent 尺寸节点白名单」：新增
  _LATENT_SIZE_NODES（现含 EmptyLatentImage、LatentUpscale，后续架构
  节点加进白名单即可）；LatentUpscale 的 width/height 此前不识别，
  导致用其控尺寸的工作流（如图生图.json #96）numbers 类为空。label
  优先级链不变（节点标题 → 类型默认「宽度/高度」），EmptyLatentImage
  行为不变；补 LatentUpscale 收录与标题优先两测试。
- pending: LoadImage.image 的 label 增加类型默认兜底「参考图（图生图底图）」：
  此前仅节点自定义标题可用（无标题即 null）；现与 width/height 相同的
  优先级链（节点标题 → 类型默认），discover 文档与 README 同步更新，
  测试改为断言默认 label 与标题优先两种情形。

## 2026-10-02 version:0.0.1

- 92380e0: 初始实现：三个 MCP 工具（list_workflows / run_workflow / get_image，
  基于 mcp 2.x SDK 的 MCPServer/StreamableHTTP，端点 /mcp）；
  Bearer 鉴权中间件（未设 MCP_AUTH_TOKEN 拒绝启动、无/错凭据 401）；
  comfy-cli 1.22.0 运行期 UI→API 转换（仅作库 import，object_info 进程内缓存
  300s，API 格式文件直通）；字段发现与 label 打标（节点标题/连线/类型默认）；
  参考图 data URL / HTTP URL 代下载上传；WS 优先 + history 轮询兜底的等待
  策略与超时/running/completed 状态机；图片 base64 内联返回（ImageContent）；
  字段地址对账校验（未知/跨类地址报错并回列可填字段）；离线测试套件 34 项
  （真实 .98 fixtures + 假 ComfyUI 的端到端 MCP 协议冒烟）；systemd 部署单元
  与中文文档。
- pending: 修复大图断流：StreamableHTTP 改为 json_response=True，tool 结果整体
  application/json 返回，绕过 SSE 解析器的单事件 1MiB 硬限制（base64 内联图
  原始 >约 768KB，如 4K 图，此前必报 "SSE stream ended without a response"）；
  新增回归测试断言 tools/call 响应为 application/json 而非 text/event-stream。
