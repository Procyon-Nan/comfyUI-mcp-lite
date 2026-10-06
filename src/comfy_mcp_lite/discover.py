"""可填字段发现与 label 打标（纯函数，无 IO）。

输入是转换后的 API 格式 prompt（{node_id: {class_type, inputs, _meta}}），
输出规格 §4.1 的 list_workflows 字段结构。

三类来源（规格固定）：
- prompts ← CLIPTextEncode.text
- numbers ← 尺寸节点白名单（EmptyLatentImage、LatentUpscale）的 .width / .height
- images  ← LoadImage.image

label 优先级（规格 §4.1）：
1. 节点标题：转换器保留在 _meta.title；仅当用户自定义过（非空且不等于节点类型名）才采用
2. 类型 + 连线：CLIPTextEncode 连到 KSampler(.Advanced) 的
   positive → 「Positive prompt」、negative → 「Negative prompt」
3. 类型默认：白名单尺寸节点的 width/height →
   「Width」/「Height」、LoadImage.image → 「Reference image (img2img base)」
4. 都不行 → label: null（地址仍可调用）

按客户端过滤（规格 omp-spec-header-filter）：X-Comfy-Workflows 请求头
点名该客户端可见的工作流（逗号分隔、逐项 URL 编码），select_workflow_names
据此收窄 list_workflows 的展示范围；头缺失/为空则不过滤（完全向后兼容）。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote


_PROMPT_NODE = "CLIPTextEncode"
# latent 尺寸节点白名单：这些节点的 width/height 输入按 numbers 类收
# （EmptyLatentImage 定初始尺寸，LatentUpscale 重设尺寸，如图生图.json #96）。
# 以后新增别的架构节点（能控制潜空间尺寸的）往这里加即可。
_LATENT_SIZE_NODES = frozenset({
    "EmptyLatentImage",
    "LatentUpscale",
})
_IMAGE_NODE = "LoadImage"
_SAMPLER_TYPES = frozenset({"KSampler", "KSamplerAdvanced"})

_LABEL_POSITIVE = "Positive prompt"  # 正面提示词
_LABEL_NEGATIVE = "Negative prompt"  # 负面提示词
_LABEL_WIDTH = "Width"  # 宽度
_LABEL_HEIGHT = "Height"  # 高度
_LABEL_REFERENCE = "Reference image (img2img base)"  # 参考图（图生图底图）


def _link_target(value: Any) -> str | None:
    """API 格式的连线值形如 ["78", 0]；否则返回 None。"""
    if isinstance(value, list) and len(value) == 2:
        node_id = value[0]
        if isinstance(node_id, (str, int)):
            return str(node_id)
    return None


def _sampler_wiring(prompt: dict[str, Any]) -> tuple[set[str], set[str]]:
    """返回 (正面提示词节点 id 集合, 负面提示词节点 id 集合)。"""
    positive: set[str] = set()
    negative: set[str] = set()
    for node in prompt.values():
        if not isinstance(node, dict) or node.get("class_type") not in _SAMPLER_TYPES:
            continue
        inputs = node.get("inputs") or {}
        for field, targets in (("positive", positive), ("negative", negative)):
            target = _link_target(inputs.get(field))
            if target is not None:
                targets.add(target)
    return positive, negative


def _node_title(
    node: dict[str, Any], class_type: str, display_name: str
) -> str | None:
    """用户自定义的节点标题（转换器放在 _meta.title）；默认名不算。

    转换器对 _meta.title 的兜底链是「用户标题 → display_name → 类型名」，
    故两者都要排除。
    """
    title = (node.get("_meta") or {}).get("title")
    if isinstance(title, str):
        title = title.strip()
        if title and title not in (class_type, display_name):
            return title
    return None


def _add(
    result: dict[str, dict[str, dict[str, Any]]],
    category: str,
    node_id: str,
    field: str,
    label: str | None,
) -> None:
    result.setdefault(category, {})[f"{node_id}.{field}"] = {"label": label}


def discover_fields(
    prompt: dict[str, Any], object_info: dict[str, Any] | None = None
) -> dict[str, dict[str, dict[str, Any]]]:
    """从 API prompt 提取可填字段；空类别不出现在结果里。

    object_info（/object_info 响应）用于区分用户自定义标题与节点默认显示名。
    """
    result: dict[str, dict[str, dict[str, Any]]] = {}
    positive, negative = _sampler_wiring(prompt)

    for node_id, node in prompt.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        if class_type not in (_PROMPT_NODE, *_LATENT_SIZE_NODES, _IMAGE_NODE):
            continue
        inputs = node.get("inputs") or {}
        display_name = (object_info or {}).get(class_type, {}).get("display_name") or class_type
        title = _node_title(node, class_type, display_name)

        if class_type == _PROMPT_NODE:
            if "text" in inputs:
                label = title
                if label is None:
                    if node_id in positive and node_id not in negative:
                        label = _LABEL_POSITIVE
                    elif node_id in negative and node_id not in positive:
                        label = _LABEL_NEGATIVE
                _add(result, "prompts", node_id, "text", label)

        elif class_type in _LATENT_SIZE_NODES:
            for field, default in (("width", _LABEL_WIDTH), ("height", _LABEL_HEIGHT)):
                if field in inputs:
                    _add(result, "numbers", node_id, field, title or default)

        elif class_type == _IMAGE_NODE:
            if "image" in inputs:
                # 与 numbers 同模式：节点标题优先，否则类型默认 label
                _add(result, "images", node_id, "image", title or _LABEL_REFERENCE)

    return result


def select_workflow_names(names: list[str], header_value: str | None) -> list[str]:
    """按 X-Comfy-Workflows 头值筛选要展示的工作流名（纯函数）。

    - 头缺失 / 空 / 纯空白 → 原样返回全部（向后兼容）；
    - 头存在 → 逐项匹配：先 unquote(item) 解码后与服务端真名精确匹配，
      不中再把 item 原样精确匹配（容忍已编码/未编码两种写法），仍不中
      即忽略（服务端不存在的工作流静默跳过，改名/删除不报错）；
    - 忽略空项（连续逗号、首尾逗号、纯空白项），重复项去重；
    - 输出保持 names 的原顺序。
    """
    if header_value is None or not header_value.strip():
        return list(names)
    known = frozenset(names)
    selected: set[str] = set()
    for item in header_value.split(","):
        item = item.strip()
        if not item:
            continue  # 空项：连续逗号 / 首尾逗号 / 纯空白
        decoded = unquote(item)
        if decoded in known:
            selected.add(decoded)
        elif item in known:
            selected.add(item)
    return [name for name in names if name in selected]
