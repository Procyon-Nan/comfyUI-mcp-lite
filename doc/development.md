# 开发与部署

## 环境准备

使用 Linux Shell 和 Python 3.10 及以上版本。在项目根目录执行：

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

依赖、版本和 pytest 配置以 [pyproject.toml](../pyproject.toml) 为准。comfy-cli 固定为 1.22.0，仅作为 UI → API 转换库使用。

## 本地运行

通过隐藏输入设置令牌后启动：

```bash
read -rsp 'MCP_AUTH_TOKEN: ' MCP_AUTH_TOKEN
export MCP_AUTH_TOKEN
printf '\n'
.venv/bin/comfy-mcp-lite
```

也可运行 `.venv/bin/python -m comfy_mcp_lite`。默认连接 `http://127.0.0.1:8188`，监听 `0.0.0.0:9101/mcp`；环境变量完整说明见 [README](../README.md#配置环境变量)。未配置令牌时拒绝启动。

## 验证

按修改范围选择已有测试：

```bash
# 工作流转换与字段发现
.venv/bin/pytest -q tests/test_convert_discover.py

# WS 图片、缓存与并发隔离
.venv/bin/pytest -q tests/test_ws_images.py tests/test_client_id_isolation.py

# 完整离线测试
.venv/bin/pytest

# 差异格式检查
git diff --check
```

离线测试基于真实导出夹具及假 ComfyUI 服务，无需实际 GPU。不要将离线测试通过表述为生产生图验证通过。真实服务验证需明确区分连通性、MCP 工具调用、工作流执行与产物检查。

## 部署

[systemd 单元](../deploy/comfy-mcp-lite.service) 是系统级部署模板，默认工作目录为 `/opt/comfy-mcp-lite`，用户和组为 `baigong`，环境文件为 `/etc/comfy-mcp-lite.env`。实际部署前核对路径、解释器、运行用户及服务管理方式。

使用该模板的系统级部署命令：

```bash
sudo cp deploy/comfy-mcp-lite.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now comfy-mcp-lite
```

环境文件中设置 `MCP_AUTH_TOKEN` 及所需配置，限制文件访问权限。账号、密码和令牌不得进入仓库。

### 已核实的远端部署

2026-10-30 检查 `192.168.111.98` 时确认：

- 项目目录为 `/home/baigong/workspaces/comfy-mcp-lite`，部署副本没有 Git 元数据。
- 使用 `/home/baigong/workspaces/ComfyUI/.venv/bin/comfy-mcp-lite` 启动，共用 ComfyUI 的虚拟环境。
- `comfy-mcp-lite.service` 和 `comfyui.service` 由用户级 systemd 管理。
- MCP 版本为 0.3.0，监听 9101；ComfyUI 版本为 0.42.0，监听 8188。

这些信息是检查时快照，部署前重新核实。该环境查看状态使用 `systemctl --user status comfy-mcp-lite`。更新部署副本后需要重启对应用户服务才能加载代码；重启前检查运行队列及服务使用情况，避免中断任务。

当前仓库的新增白名单不会自动同步到远端。Git 推送与更新该部署副本是独立操作。

## 临时文件

临时方案、检查脚本和输出文件放入 `tmp/`，不提交到仓库。使用前将 `/tmp/` 加入本地 `.git/info/exclude`，或按项目需要更新 `.gitignore`；当前项目的 `.gitignore` 尚未包含该目录。
