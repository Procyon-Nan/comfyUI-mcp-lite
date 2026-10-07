"""最小假 ComfyUI（starlette）。

只实现本项目实际用到的端点，用于无 ComfyUI 环境下的服务级冒烟：
userdata 列表/读取、object_info、upload/image、prompt 提交、
history、queue、view。

默认不提供 /ws —— 正好覆盖「WS 连不上 → 轮询兜底」路径。构造时
传 ws=True 则挂上 /ws，并复刻 ComfyUI 服务端的 WS 语义（并发回归
测试依赖）：注册表按 clientId 单槽位，同 clientId 再连会顶替旧连接
（server.py:276-284）；auto 任务等本 client_id 的监听接入时完成
（模拟执行期有人在监听），完成时把 ws_images 里的成品 PNG 帧
（PREVIEW_IMAGE 事件、image_type=PNG，与 SaveImageWebsocket 一致）
与 executing(node=None) 终态事件按任务记录的 client_id 定向发给
对应连接（execution.py:684/:737 的按 client_id 路由；该 id 无在线
连接时帧丢弃）。diskless=True 时 history outputs 置空，模拟
SaveImageWebsocket 不落盘。

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
        # - ws_images：完成时推送的成品 PNG 帧内容（None=不推，走磁盘
        #   兜底；list=全体任务共用；callable(job)=按任务定制帧，
        #   并发回归测试用来给两路不同的颜色）
        # - diskless：history outputs 置空（模拟 SaveImageWebsocket 不落盘）
        self.ws_images: list[bytes] | typing.Callable[[dict], list[bytes] | None] | None = None
        self.diskless: bool = False
        self.uploads: dict[str, bytes] = {}
        self.jobs: dict[str, dict] = {}
        self.counter = 0
        # WS 注册表：clientId → 连接，单槽位（同 id 再连顶替旧连接），
        # 复刻 ComfyUI server.py:276-284
        self._ws_clients: dict[str, WebSocket] = {}
        # 接入顺序记录的 clientId（含被顶替的旧连接），测试等待/断言用
        self.ws_client_ids: list[str] = []

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

    async def complete_over_ws(self, prompt_id: str) -> None:
        """测试驱动：让指定 running 任务走 WS 收尾路径（等价执行完成）。

        与 auto 任务接入即完成的路径相同，帧按任务记录的 client_id 定向
        投递；用于在两路监听都建立后手动驱动完成时序（并发回归测试）。
        """
        await self._finish_over_ws(prompt_id)

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
        """接入即完成等待中的本 client_id 的 auto 任务（帧 → history → 终态事件）。

        复刻 ComfyUI server.py:276-284：注册表按 clientId 单槽位，
        同 clientId 再连顶替旧连接（旧连接不再收到定向帧）。
        """
        sid = websocket.query_params.get("clientId", "")
        await websocket.accept()
        self._ws_clients[sid] = websocket
        self.ws_client_ids.append(sid)
        try:
            for prompt_id, job in list(self.jobs.items()):
                if (
                    job["state"] == "running"
                    and job.get("auto")
                    and job.get("client_id") == sid
                ):
                    await self._finish_over_ws(prompt_id)
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
        finally:
            # 只摘自己：被顶替的旧连接断开时不得误删新连接的槽位
            if self._ws_clients.get(sid) is websocket:
                self._ws_clients.pop(sid, None)

    async def _finish_over_ws(self, prompt_id: str) -> None:
        """按真实时序收尾：执行期推成品帧 → 完成落 history → 终态事件。

        复刻 ComfyUI execution.py:684/:737 的定向投递：帧与终态事件只
        发给该任务 client_id 对应的那条连接；该 id 无在线连接时帧丢弃
        （任务照常完成，监听方靠轮询兜底感知）。
        """
        job = self.jobs[prompt_id]
        websocket = self._ws_clients.get(job.get("client_id"))
        if websocket is not None:
            for image in self._job_ws_images(job) or []:
                # PREVIEW_IMAGE(1) + image_type PNG(2)，与 ComfyUI 协议一致
                await websocket.send_bytes(struct.pack(">II", 1, 2) + image)
        self.complete(prompt_id, images=[] if self.diskless else None)
        if websocket is not None:
            await websocket.send_json(
                {"type": "executing", "data": {"node": None, "prompt_id": prompt_id}}
            )

    def _job_ws_images(self, job: dict) -> list[bytes] | None:
        """该任务完成时要推的成品帧：ws_images 为 list 时全体共用，
        为 callable(job) 时按任务定制（并发回归测试给两路不同颜色）。"""
        if callable(self.ws_images):
            return self.ws_images(job)
        return self.ws_images

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
