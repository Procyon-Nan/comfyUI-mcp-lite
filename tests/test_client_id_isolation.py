"""回归：每次 run_workflow 调用使用独立的 client_id（并发不串图）。

背景缺陷（规格 omp-spec-client-id-isolation.md）：client_id 曾在
register() 里生成一次、全进程复用；而 ComfyUI 的 WS 注册表按
clientId 单槽位（同 id 再连顶替旧连接），并发或先后两次调用的监听
会互相顶掉，先发起一方的成品 PNG 帧被投给后者的连接（串图），
自身收不到帧只能落磁盘兜底，而 SaveImageWebsocket 不落盘 → 误报
「任务已完成但未产出图片」。

两个用例：
1. 直接断言：两次成功调用的任务在 fake 端记录的 client_id 互不相同；
2. 行为回归（核心）：两路并发 + diskless + 按调用可区分的 ws_images，
   两路必须各自拿到自己的图。共用 client_id 的旧实现上必失败：后连
   顶替先连，两路帧都投给后者的连接 → 先发起的一路报「未产出图片」
   或混入对方的图。
"""

import asyncio

from fake_comfy import make_png
from test_service import (
    _env,
    _error_text,
    _inline_images,
    _text_payload,
    mcp_session,
)

_RED = make_png(b"\xff\x00\x00")
_BLUE = make_png(b"\x00\x00\xff")


async def _until(predicate, timeout: float = 5.0) -> None:
    """轮询等待条件成立（同一事件循环，fake 服务端状态立即可见）。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "等待条件超时"
        await asyncio.sleep(0.01)


async def test_two_calls_get_distinct_client_ids(simple_ui: dict, object_info: dict) -> None:
    """直接断言方案 A：连续两次成功调用，任务记录的 client_id 互不相同。"""
    async with _env(simple_ui, object_info) as env:
        async with mcp_session(env) as session:
            for text in ("first", "second"):
                result = await session.call_tool(
                    "run_workflow", {"workflow": "simple", "prompts": {"78.text": text}}
                )
                assert not result.is_error, _error_text(result)
        client_ids = [job["client_id"] for job in env.fake.jobs.values()]
        assert len(client_ids) == 2
        assert client_ids[0] != client_ids[1], "两次调用必须使用不同的 client_id"


async def test_concurrent_calls_do_not_steal_each_others_frames(
    simple_ui: dict, object_info: dict
) -> None:
    """行为回归（核心）：两路并发调用各自拿到自己的图，互不串帧。

    驱动方式：任务先 hold 挂起，先发起 A、其 WS 监听建立后再发起 B
    （复刻真实场景：两路监听同时在线时执行完成），随后手动驱动两路
    按 WS 路径收尾，帧按任务记录的 client_id 定向投递。旧实现（全进程
    共用 client_id）上 B 的连接会顶掉 A 的槽位 → 两路帧都到 B 的连接。
    """
    async with _env(simple_ui, object_info, ws=True) as env:
        fake = env.fake
        # hold：提交后挂起，由测试在两路 WS 监听都建立后驱动完成
        fake.job_policy = lambda prompt: "hold"
        # diskless：只有 WS 帧、磁盘为空 —— 串图方收不到帧即「未产出图片」
        fake.diskless = True
        # 按调用区分成品帧颜色（prompt 文本 → 图），两路可互相对照
        fake.ws_images = lambda job: (
            [_RED] if job["prompt"]["78"]["inputs"]["text"] == "A" else [_BLUE]
        )
        async with mcp_session(env) as session_a, mcp_session(env) as session_b:
            task_a = asyncio.create_task(
                session_a.call_tool(
                    "run_workflow",
                    {"workflow": "simple", "prompts": {"78.text": "A"}, "timeout_seconds": 30},
                )
            )
            await _until(lambda: len(fake.jobs) == 1 and len(fake.ws_client_ids) >= 1)
            task_b = asyncio.create_task(
                session_b.call_tool(
                    "run_workflow",
                    {"workflow": "simple", "prompts": {"78.text": "B"}, "timeout_seconds": 30},
                )
            )
            await _until(lambda: len(fake.jobs) == 2 and len(fake.ws_client_ids) >= 2)

            # 两路监听均已建立，按提交顺序驱动两路收尾（帧定向投递）
            for prompt_id in list(fake.jobs):
                await fake.complete_over_ws(prompt_id)
            result_a, result_b = await asyncio.gather(task_a, task_b)

        assert not result_a.is_error, _error_text(result_a)
        assert not result_b.is_error, _error_text(result_b)
        assert _text_payload(result_a)["status"] == "completed"
        assert _text_payload(result_b)["status"] == "completed"
        # 各自拿到自己的颜色，无串图、无「未产出图片」
        assert _inline_images(result_a) == [_RED]
        assert _inline_images(result_b) == [_BLUE]
        # 两路占用的 WS 槽位（clientId）互不相同 —— 旧实现共用同一个 id
        assert fake.ws_client_ids[-1] != fake.ws_client_ids[-2]
