# Changelog

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
