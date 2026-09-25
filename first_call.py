"""Smallest useful Claude Opus 5.5 request: confirm the key works and look at
the content blocks that come back.

    python first_call.py
"""
import anthropic
from dotenv import load_dotenv

load_dotenv()
client = anthropic.Anthropic()

response = client.messages.create(
    model="claude-opus-5-5",
    max_tokens=2048,
    messages=[{
        "role": "user",
        "content": "A checkout API returns HTTP 503 right after a deploy. Name the first two things to check.",
    }],
)

print("block types:", [block.type for block in response.content])
print("stop_reason:", response.stop_reason)
print("usage:", response.usage.input_tokens, "in,", response.usage.output_tokens, "out")
text = "".join(block.text for block in response.content if block.type == "text")
print("\n" + text)
