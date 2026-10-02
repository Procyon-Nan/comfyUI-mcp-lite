"""最小假 ComfyUI（starlette）。

只实现本项目实际用到的端点，用于无 ComfyUI 环境下的服务级冒烟：
userdata 列表/读取、object_info、upload/image、prompt 提交、
history、queue、view。不提供 /ws —— 正好覆盖「WS 连不上 → 轮询兜底」路径。

任务完成策略：job_policy(prompt) 返回 "hold" 则一直运行（用于超时测试），
默认提交后立即完成并产出一张贴图。
"""

from __future__ import annotations

import base64
import typing
from urllib.parse import unquote

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

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
    ) -> None:
        self.workflows = workflows
        self.object_info = object_info
        self.job_policy = job_policy
        self.uploads: dict[str, bytes] = {}
        self.jobs: dict[str, dict] = {}
        self.counter = 0

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
        }
        if self.job_policy(prompt) == "complete":
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

    def build(self) -> Starlette:
        return Starlette(
            routes=[
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
        )
