"""有界模型重试与预算账本。未知用量保守预留，不声称准确计费或跨模型token硬上限。"""

import asyncio
import json
import time
from dataclasses import dataclass

from openai import APIConnectionError


class ExecutionError(RuntimeError):
    """只允许固定内部错误码，不附请求或供应商正文。"""


@dataclass
class Ledger:
    calls: int = 0
    token_units: int = 0
    tool_calls: int = 0
    steps: int = 0


async def complete(model, instructions, items, tools, settings, ledger, trace, step):
    input_bytes = len(json.dumps([instructions, items, tools], ensure_ascii=False).encode())
    if input_bytes > settings.max_input_bytes:
        raise ExecutionError("INPUT_BYTE_BUDGET_EXHAUSTED")
    reservation = input_bytes + settings.max_output_tokens
    for attempt in range(settings.model_retries + 1):
        if ledger.calls >= settings.max_model_calls:
            raise ExecutionError("MODEL_CALL_BUDGET_EXHAUSTED")
        if ledger.token_units + reservation > settings.max_total_token_units:
            raise ExecutionError("TOKEN_UNIT_BUDGET_EXHAUSTED")
        ledger.calls += 1
        ledger.token_units += reservation
        event = {
            "kind": "model",
            "step": step,
            "attempt": attempt + 1,
            "outcome": "failed",
            "usage": None,
            "input_bytes": input_bytes,
            "reserved_token_units": reservation,
        }
        trace.append(event)
        started = time.perf_counter()
        retry = False
        try:
            reply = await model.complete(instructions, items, tools)
            total = (reply.usage or {}).get("total_tokens")
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                ledger.token_units += total - reservation
            event.update(
                outcome="completed",
                response_model=reply.response_model,
                usage=reply.usage,
                provider_request_id=getattr(reply, "request_id", None),
                requested_model=settings.model,
                reported_model_matches_request=reply.response_model == settings.model
                if reply.response_model
                else None,
            )
            if ledger.token_units > settings.max_total_token_units:
                raise ExecutionError("REPORTED_TOKEN_BUDGET_EXCEEDED")
            return reply
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            event.update(error_type=type(exc).__name__, http_status=status)
            retry = attempt < settings.model_retries and (
                status in {408, 429, 500, 502, 503, 504} or isinstance(exc, APIConnectionError)
            )
            event["retry_scheduled"] = retry
            if not retry:
                raise
        finally:
            event["seconds"] = round(time.perf_counter() - started, 3)
        if retry:
            await asyncio.sleep(0.2 * 2**attempt)
    raise ExecutionError("MODEL_RETRY_EXHAUSTED")
