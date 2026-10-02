"""测试夹具：从 tests/fixtures 加载 .98 导出的真实数据。"""

from __future__ import annotations

import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def simple_ui() -> dict:
    return json.loads((FIXTURES / "simple.ui.json").read_text(encoding="utf-8"))


@pytest.fixture
def object_info() -> dict:
    return json.loads((FIXTURES / "object_info.json").read_text(encoding="utf-8"))
