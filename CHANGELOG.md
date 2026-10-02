# Changelog

## 2026-10-02 version:0.0.1

- pending: 初始实现：三个 MCP 工具（list_workflows / run_workflow / get_image，
  基于 mcp 2.x SDK 的 MCPServer/StreamableHTTP，端点 /mcp）；
  Bearer 鉴权中间件（未设 MCP_AUTH_TOKEN 拒绝启动、无/错凭据 401）；
  comfy-cli 1.22.0 运行期 UI→API 转换（仅作库 import，object_info 进程内缓存
  300s，API 格式文件直通）；字段发现与 label 打标（节点标题/连线/类型默认）；
  参考图 data URL / HTTP URL 代下载上传；WS 优先 + history 轮询兜底的等待
  策略与超时/running/completed 状态机；图片 base64 内联返回（ImageContent）；
  字段地址对账校验（未知/跨类地址报错并回列可填字段）；离线测试套件 34 项
  （真实 .98 fixtures + 假 ComfyUI 的端到端 MCP 协议冒烟）；systemd 部署单元
  与中文文档。
