import os
import sys
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")


class FakeBlock(SimpleNamespace):
    def model_dump(self, mode="python", exclude_none=False):
        data = {}
        for key, value in vars(self).items():
            if isinstance(value, SimpleNamespace):
                value = {k: v for k, v in vars(value).items()}
            if exclude_none and value is None:
                continue
            data[key] = value
        return data


def usage(input_tokens=100, output_tokens=50, cache_read=0, cache_write=0):
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                           cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write,
                           cache_creation=None, server_tool_use=None)


def tool_use(block_id, name, tool_input, caller="direct"):
    return FakeBlock(type="tool_use", id=block_id, name=name, input=tool_input,
                     caller=SimpleNamespace(type=caller) if caller == "direct"
                     else SimpleNamespace(type=caller, tool_id="srvtoolu_1"))


def text_block(text):
    return FakeBlock(type="text", text=text)


def response(blocks, stop_reason="end_turn", **usage_kwargs):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason, usage=usage(**usage_kwargs), container=None)


@pytest.fixture
def runtime_assets(tmp_path, monkeypatch):
    from agent import agent

    for name in ("monitoring_dashboard.png", "architecture_diagram.png"):
        (tmp_path / name).write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-image")
    monkeypatch.setattr(agent, "ASSETS_DIR", str(tmp_path))
    return tmp_path
