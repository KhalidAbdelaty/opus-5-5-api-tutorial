"""Send one request with a forced tool_choice and print what Opus 5.5 returns.

    python check_tool_choice.py
"""
import anthropic
from dotenv import load_dotenv

load_dotenv()
client = anthropic.Anthropic()

tool = {
    "name": "get_deployment_context",
    "description": "Return the latest deployment's metadata.",
    "input_schema": {"type": "object", "properties": {}},
}

for choice in ({"type": "any"}, {"type": "tool", "name": "get_deployment_context"}, {"type": "auto"}):
    try:
        resp = client.messages.create(
            model="claude-opus-5-5",
            max_tokens=256,
            tools=[tool],
            tool_choice=choice,
            messages=[{"role": "user", "content": "What changed in the last deployment?"}],
        )
        print(f"tool_choice={choice}: accepted, stop_reason={resp.stop_reason}, "
              f"blocks={[b.type for b in resp.content]}")
    except anthropic.BadRequestError as exc:
        print(f"tool_choice={choice}: HTTP {exc.status_code}: {exc.body.get('error', {}).get('message')}")
