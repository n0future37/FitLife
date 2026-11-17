"""本机Ollama非思考文本/工具适配；不访问Ollama云端，不自带业务权限。"""

import json
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from fitlife.chat_messages import chat_messages
from fitlife.model import Reply


class OllamaModel:
    network_model = True

    def __init__(self, settings):
        if urlsplit(settings.base_url).hostname not in {
            "127.0.0.1",
            "localhost",
            "host.docker.internal",
        }:
            raise ValueError("OLLAMA_LOCAL_ENDPOINT_REQUIRED")
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/") + "/",
            timeout=settings.request_timeout,
            trust_env=False,
        )

    async def complete(self, instructions, items, tools):
        if len(json.dumps([instructions, items, tools], ensure_ascii=False).encode()) > 12000:
            raise ValueError("LOCAL_INPUT_TOO_LARGE")
        if tools and "tools" not in self.settings.model_capabilities:
            raise ValueError("MODEL_TOOLS_NOT_SUPPORTED")
        messages, call_names = [], {}
        for item in chat_messages(instructions, items):
            content = item.get("content") or ""
            if isinstance(content, list):
                if any(p["type"] != "text" for p in content):
                    raise ValueError("LOCAL_IMAGES_NOT_SUPPORTED")
                content = "\n".join(p["text"] for p in content)
            message = {"role": item["role"], "content": content}
            if item.get("tool_calls"):
                message["tool_calls"] = []
                for call in item["tool_calls"]:
                    function = dict(call["function"])
                    call_names[call["id"]] = function["name"]
                    if isinstance(function["arguments"], str):
                        function["arguments"] = json.loads(function["arguments"])
                    message["tool_calls"].append({"function": function})
            if item["role"] == "tool":
                if item["tool_call_id"] not in call_names:
                    raise ValueError("LOCAL_TOOL_RESULT_WITHOUT_CALL")
                message["tool_name"] = call_names[item["tool_call_id"]]
            messages.append(message)
        response = await self.client.post(
            "api/chat",
            json={
                "model": self.settings.model,
                "messages": messages,
                "stream": False,
                "think": False,
                "tools": [
                    {"type": "function", "function": {k: v for k, v in t.items() if k != "type"}}
                    for t in tools
                ],
                "options": {
                    "num_ctx": 16384,
                    "num_predict": self.settings.max_output_tokens,
                    "temperature": 0,
                },
                "keep_alive": "10m",
            },
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("done") or data.get("done_reason") == "length":
            raise RuntimeError("MODEL_INCOMPLETE")
        message = data["message"]
        calls = [
            {
                "name": c["function"]["name"],
                "arguments": json.dumps(c["function"]["arguments"], ensure_ascii=False),
                "call_id": "local-" + uuid4().hex,
            }
            for c in message.get("tool_calls", [])
        ]
        continuation = {"role": "assistant", "content": message.get("content") or None}
        if calls:
            continuation["tool_calls"] = [
                {
                    "id": c["call_id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": c["arguments"]},
                }
                for c in calls
            ]
        usage = None
        if isinstance(data.get("prompt_eval_count"), int) and isinstance(
            data.get("eval_count"), int
        ):
            usage = {
                "input_tokens": data["prompt_eval_count"],
                "output_tokens": data["eval_count"],
                "total_tokens": data["prompt_eval_count"] + data["eval_count"],
            }
        return Reply(
            text=message.get("content", ""),
            calls=calls,
            continuation=[continuation],
            usage=usage,
            response_model=data.get("model"),
        )

    async def close(self):
        await self.client.aclose()
