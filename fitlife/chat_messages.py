"""把已发生的文本/工具交互转为Chat消息；保留调用ID，不重放工具。"""

import json


def chat_messages(instructions, items):
    messages = [{"role": "system", "content": instructions}]
    for item in items:
        if item.get("type") == "reasoning":
            continue  # Provider-specific reasoning state is not a business observation.
        if item.get("type") == "function_call":
            call = {
                "id": item["call_id"],
                "type": "function",
                "function": {"name": item["name"], "arguments": item["arguments"]},
            }
            if messages[-1]["role"] != "assistant":
                messages.append({"role": "assistant", "content": None})
            messages[-1].setdefault("tool_calls", []).append(call)
        elif item.get("type") == "function_call_output":
            output = item["output"]
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item["call_id"],
                    "content": output if isinstance(output, str) else json.dumps(output),
                }
            )
        elif item.get("role"):
            converted = {k: v for k, v in item.items() if k not in {"type", "id", "status"}}
            if isinstance(converted.get("content"), list):
                parts = []
                for part in converted["content"]:
                    if part["type"] in {"input_text", "output_text"}:
                        parts.append({"type": "text", "text": part["text"]})
                    elif part["type"] == "input_image":
                        parts.append({"type": "image_url", "image_url": {"url": part["image_url"]}})
                    else:
                        raise ValueError("CHAT_INPUT_PART_NOT_SUPPORTED")
                converted["content"] = parts
            messages.append(converted)
        else:
            raise ValueError("CHAT_INPUT_ITEM_NOT_SUPPORTED")
    return messages
