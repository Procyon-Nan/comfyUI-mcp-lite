"""最小假 ComfyUI（starlette）。

只实现本项目实际用到的端点，用于无 ComfyUI 环境下的服务级冒烟：
userdata 列表/读取、object_info、upload/image、prompt 提交、
history、queue、view。

默认不提供 /ws —— 正好覆盖「WS 连不上 → 轮询兜底」路径。构造时
传 ws=True 则挂上 /ws：auto 任务改为等 WS 客户端接入时完成（模拟
执行期有人在监听），完成时向已连接客户端推 ws_images 里的成品
PNG 帧（PREVIEW_IMAGE 事件、image_type=PNG，与 SaveImageWebsocket
一致），再发 executing(node=None) 终态事件。diskless=True 时
history outputs 置空，模拟 SaveImageWebsocket 不落盘。

任务完成策略：job_policy(prompt) 返回 "hold" 则一直运行（用于超时测试），
默认提交后立即完成并产出一张贴图。
"""

from __future__ import annotations

import base64
import struct
import typing
import zlib
from urllib.parse import unquote

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket


def make_png(color: bytes = b"\x00\x00\x00") -> bytes:
    """生成 1×1 RGB PNG（测试用；颜色不同即字节不同）。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data))
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00" + color))
        + chunk(b"IEND", b"")
    )


# 1×1 PNG（透明）
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

JobPolicy = typing.Callable[[dict], typing.Literal["complete", "hold"]]


def _default_policy(prompt: dict) -> typing.Literal["complete", "hold"]:
    return "complete"


class FakeComfy:
    def __init__(
        self,
        workflows: dict[str, dict],
        object_info: dict,
        job_policy: JobPolicy = _default_policy,
        ws: bool = False,
    ) -> None:
        self.workflows = workflows
        self.object_info = object_info
        self.job_policy = job_policy
        self.ws = ws
        # ws 模式行为开关（测试按需设置）：
        # - ws_images：完成时推送的成品 PNG 帧内容（None=不推，走磁盘兜底）
        # - diskless：history outputs 置空（模拟 SaveImageWebsocket 不落盘）
        self.ws_images: list[bytes] | None = None
        self.diskless: bool = False
        self.uploads: dict[str, bytes] = {}
        self.jobs: dict[str, dict] = {}
        self.counter = 0
        self._ws_clients: set[WebSocket] = set()

    # ---- 测试驱动接口 ----

    def complete(self, prompt_id: str, images: list[dict] | None = None) -> None:
        job = self.jobs[prompt_id]
        job["state"] = "completed"
        job["outputs"] = images if images is not None else [
            {"filename": "ComfyUI_00001_.png", "subfolder": "", "type": "output"}
        ]

    def fail(self, prompt_id: str) -> None:
        job = self.jobs[prompt_id]
        job["state"] = "error"

    # ---- 路由 ----

    async def list_userdata(self, request: Request) -> Response:
        if request.query_params.get("dir") != "workflows":
            return JSONResponse([], status_code=404)
        return JSONResponse(sorted(self.workflows))

    async def get_userdata(self, request: Request) -> Response:
        path = request.path_params["path"]
        if "%" in path:
            path = unquote(path)
        path = path.removeprefix("workflows/")
        if path in self.workflows:
            return JSONResponse(self.workflows[path])
        return JSONResponse({"error": "not found"}, status_code=404)

    async def get_object_info(self, request: Request) -> Response:
        return JSONResponse(self.object_info)

    async def upload_image(self, request: Request) -> Response:
        form = await request.form()
        upload = form.get("image")
        if upload is None:
            return JSONResponse({"error": "no image"}, status_code=400)
        name = upload.filename or "upload.bin"
        self.uploads[name] = upload.file.read()
        return JSONResponse({"name": name, "subfolder": "", "type": "input"})

    async def post_prompt(self, request: Request) -> Response:
        body = await request.json()
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return JSONResponse({"error": {"message": "No prompt provided"}, "node_errors": {}}, status_code=400)
        self.counter += 1
        prompt_id = f"00000000-0000-0000-0000-{self.counter:012d}"
        self.jobs[prompt_id] = {
            "prompt": prompt,
            "client_id": body.get("client_id"),
            "state": "running",
            "outputs": None,
            "auto": self.job_policy(prompt) == "complete",
        }
        # ws 模式：auto 任务等 WS 客户端接入时再完成（帧要在监听建立后推）
        if self.jobs[prompt_id]["auto"] and not self.ws:
            self.complete(prompt_id)
        return JSONResponse({"prompt_id": prompt_id, "number": self.counter, "node_errors": {}})

    async def get_history(self, request: Request) -> Response:
        prompt_id = request.path_params["prompt_id"]
        job = self.jobs.get(prompt_id)
        if job is None or job["state"] == "running":
            return JSONResponse({})
        entry = {
            "prompt_id": prompt_id,
            "outputs": (
                {"87": {"images": job["outputs"]}} if job["outputs"] else {}
            ),
            "status": {
                "status_str": "error" if job["state"] == "error" else "success",
                "completed": True,
                "messages": (
                    [
                        [
                            "execution_error",
                            {
                                "node_id": "78",
                                "node_type": "CLIPTextEncode",
                                "exception_type": "ValueError",
                                "exception_message": "boom",
                            },
                        ]
                    ]
                    if job["state"] == "error"
                    else []
                ),
            },
        }
        return JSONResponse({prompt_id: entry})

    async def get_queue(self, request: Request) -> Response:
        running = [
            [1, pid, job["prompt"], {}, []]
            for pid, job in self.jobs.items()
            if job["state"] == "running"
        ]
        return JSONResponse({"queue_running": running, "queue_pending": []})

    async def view(self, request: Request) -> Response:
        filename = request.query_params.get("filename", "")
        if filename.startswith("ComfyUI_"):
            return Response(content=_TINY_PNG, media_type="image/png")
        if filename in self.uploads:
            return Response(content=self.uploads[filename], media_type="image/png")
        return JSONResponse({"error": "not found"}, status_code=404)

    async def static(self, request: Request) -> Response:
        return Response(content=_TINY_PNG, media_type="image/png")

    # ---- WebSocket（模拟 ComfyUI /ws）----

    async def ws_endpoint(self, websocket: WebSocket) -> None:
        """接入即完成等待中的 auto 任务（帧 → history → 终态事件）。"""
        await websocket.accept()
        self._ws_clients.add(websocket)
        try:
            for prompt_id, job in list(self.jobs.items()):
                if job["state"] == "running" and job.get("auto"):
                    await self._finish_over_ws(prompt_id)
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
        finally:
            self._ws_clients.discard(websocket)

    async def _finish_over_ws(self, prompt_id: str) -> None:
        """按真实时序收尾：执行期推成品帧 → 完成落 history → 终态事件。"""
        if self.ws_images is not None:
            for image in self.ws_images:
                # PREVIEW_IMAGE(1) + image_type PNG(2)，与 ComfyUI 协议一致
                await self._ws_broadcast(struct.pack(">II", 1, 2) + image)
        self.complete(prompt_id, images=[] if self.diskless else None)
        await self._ws_broadcast_json(
            {"type": "executing", "data": {"node": None, "prompt_id": prompt_id}}
        )

    async def _ws_broadcast(self, payload: bytes) -> None:
        for websocket in list(self._ws_clients):
            await websocket.send_bytes(payload)

    async def _ws_broadcast_json(self, payload: dict) -> None:
        for websocket in list(self._ws_clients):
            await websocket.send_json(payload)

    def build(self) -> Starlette:
        routes = [
            Route("/userdata", self.list_userdata),
            Route("/userdata/{path:path}", self.get_userdata),
            Route("/object_info", self.get_object_info),
            Route("/upload/image", self.upload_image, methods=["POST"]),
            Route("/prompt", self.post_prompt, methods=["POST"]),
            Route("/history/{prompt_id}", self.get_history),
            Route("/queue", self.get_queue),
            Route("/view", self.view),
            Route("/static/{name}", self.static),
        ]
        if self.ws:
            routes.append(WebSocketRoute("/ws", self.ws_endpoint))
        return Starlette(routes=routes)
