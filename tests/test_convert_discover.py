"""转换冒烟 + 字段发现（验收条 1：simple → prompts 78/79、numbers 80、无 images 键）。

依据实测结论（.98）：convert_ui_to_api(simple.ui.json, object_info.json)
产出 10 个 API 节点。此处在本机 venv 复跑同一转换，证明依赖安装完整、
转换链路可用；字段发现断言即规格验收标准第 1 条。
"""

from __future__ import annotations

from comfy_cli.workflow_to_api import convert_ui_to_api

from comfy_mcp_lite.discover import discover_fields


def test_convert_simple(simple_ui: dict, object_info: dict) -> None:
    prompt = convert_ui_to_api(simple_ui, object_info)
    assert isinstance(prompt, dict)
    assert len(prompt) == 10  # 与 .98 实测一致
    assert prompt["78"]["class_type"] == "CLIPTextEncode"
    assert prompt["79"]["class_type"] == "CLIPTextEncode"
    assert prompt["80"]["class_type"] == "EmptyLatentImage"
    assert prompt["87"]["class_type"] == "SaveImage"


def test_discover_simple_fields(simple_ui: dict, object_info: dict) -> None:
    """验收条 1：78/79 → prompts；80.width/height → numbers；无 images 键。"""
    prompt = convert_ui_to_api(simple_ui, object_info)
    fields = discover_fields(prompt, object_info)

    assert set(fields) == {"prompts", "numbers"}  # 空类省略：无 images 键
    assert set(fields["prompts"]) == {"78.text", "79.text"}
    assert set(fields["numbers"]) == {"80.width", "80.height"}
    # 每个字段都带 label 键（值可为 None，规格允许）
    for entry in {**fields["prompts"], **fields["numbers"]}.values():
        assert "label" in entry


def test_discover_labels(simple_ui: dict, object_info: dict) -> None:
    """真实数据打标：78/79 用用户标题（优先级 1）；宽高走类型默认（优先级 3）。"""
    prompt = convert_ui_to_api(simple_ui, object_info)
    fields = discover_fields(prompt, object_info)
    assert fields["prompts"]["78.text"]["label"] == "正面提示词（你要画的东西，写在这）"
    assert fields["prompts"]["79.text"]["label"] == "负面提示词（你不想让画面中出现的东西，写在这）"
    assert fields["numbers"]["80.width"]["label"] == "宽度"
    assert fields["numbers"]["80.height"]["label"] == "高度"




def test_discover_empty_for_nonfillable_prompt() -> None:
    """没有三类节点的工作流 → 全空（list_workflows 对其返回空对象）。"""
    fields = discover_fields(
        {"5": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}}}
    )
    assert fields == {}


def test_discover_wiring_labels() -> None:
    """无标题时按连线打标：positive → 正面、negative → 负面。"""
    prompt = {
        "10": {"class_type": "KSampler", "inputs": {
            "positive": ["78", 0], "negative": ["79", 0], "seed": 1,
        }},
        "78": {"class_type": "CLIPTextEncode", "inputs": {"text": "a"}},
        "79": {"class_type": "CLIPTextEncode", "inputs": {"text": "b"}},
    }
    fields = discover_fields(prompt)
    assert fields["prompts"]["78.text"]["label"] == "正面提示词"
    assert fields["prompts"]["79.text"]["label"] == "负面提示词"


def test_discover_title_priority() -> None:
    """节点标题优先于连线打标。"""
    prompt = {
        "10": {"class_type": "KSampler", "inputs": {
            "positive": ["78", 0], "negative": ["79", 0], "seed": 1,
        }},
        "78": {"class_type": "CLIPTextEncode", "inputs": {"text": "a"},
               "_meta": {"title": "你要画的东西写在这"}},
        "79": {"class_type": "CLIPTextEncode", "inputs": {"text": "b"}},
    }
    fields = discover_fields(prompt)
    assert fields["prompts"]["78.text"]["label"] == "你要画的东西写在这"
    assert fields["prompts"]["79.text"]["label"] == "负面提示词"


def test_discover_unwired_prompt_label_null() -> None:
    """未连线且无标题的提示词字段：label 为 null（地址仍可调用）。"""
    prompt = {"78": {"class_type": "CLIPTextEncode", "inputs": {"text": "a"}}}
    fields = discover_fields(prompt)
    assert fields["prompts"]["78.text"]["label"] is None


def test_discover_loadimage_default_label() -> None:
    """无自定义标题的 LoadImage.image → 类型默认 label（与宽高同模式）。"""
    fields = discover_fields(
        {"5": {"class_type": "LoadImage", "inputs": {"image": "x.png"}}}
    )
    assert set(fields) == {"images"}
    assert fields["images"]["5.image"]["label"] == "参考图（图生图底图）"


def test_discover_loadimage_custom_title_wins() -> None:
    """节点自定义标题优先于类型默认 label（与 numbers 的兜底链一致）。"""
    fields = discover_fields(
        {"5": {"class_type": "LoadImage", "inputs": {"image": "x.png"},
               "_meta": {"title": "人物底图"}}}
    )
    assert fields["images"]["5.image"]["label"] == "人物底图"
