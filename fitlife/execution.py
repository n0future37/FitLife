"""有界模型重试与预算账本。未知用量保守预留，不声称准确计费或跨模型token硬上限。"""

import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx
from openai import APIConnectionError

from fitlife.circuit import CIRCUITS, CircuitOpen, endpoint_key


class ExecutionError(RuntimeError):
    """只允许固定内部错误码，不附请求或供应商正文。"""


@dataclass
class Ledger:
    calls: int = 0
    token_units: int = 0
    tool_calls: int = 0
    steps: int = 0
    local_fallbacks: set[str] = field(default_factory=set)


def status_code(exc):
    return getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )


def transient(exc):
    return isinstance(
        exc, (TimeoutError, APIConnectionError, httpx.TransportError, CircuitOpen)
    ) or status_code(exc) in {408, 429, 500, 502, 503, 504}


async def _attempts(model, instructions, items, tools, settings, ledger, trace, step):
    input_bytes = len(json.dumps([instructions, items, tools], ensure_ascii=False).encode())
    if input_bytes > settings.max_input_bytes:
        raise ExecutionError("INPUT_BYTE_BUDGET_EXHAUSTED")
    reservation = input_bytes + settings.max_output_tokens
    for attempt in range(settings.model_retries + 1):
        if ledger.calls >= settings.max_model_calls:
            raise ExecutionError("MODEL_CALL_BUDGET_EXHAUSTED")
        if ledger.token_units + reservation > settings.max_total_token_units:
            raise ExecutionError("TOKEN_UNIT_BUDGET_EXHAUSTED")
        permit = None
        if settings.circuit_enabled and getattr(model, "network_model", False):
            try:
                permit = CIRCUITS.acquire(endpoint_key(settings))
            except CircuitOpen:
                trace.append(
                    {
                        "kind": "model_circuit_open",
                        "requested_model": settings.model,
                        "provider": settings.provider,
                        "network_call": False,
                    }
                )
                raise
        ledger.calls += 1
        ledger.token_units += reservation
        event = {
            "kind": "model",
            "step": step,
            "attempt": attempt + 1,
            "outcome": "failed",
            "usage": None,
            "input_bytes": input_bytes,
            "available_tool_count": len(tools),
            "reserved_token_units": reservation,
            "started_ns": time.perf_counter_ns(),
            "provider": settings.provider,
            "requested_model": settings.model,
            "api_family": settings.api_family,
            "reasoning_effort_requested": settings.reasoning_effort,
            "enable_thinking_requested": settings.enable_thinking,
            "thinking_budget_requested": settings.thinking_budget,
            "timeout_seconds": settings.request_timeout,
        }
        trace.append(event)
        started = time.perf_counter()
        retry = False
        try:
            async with asyncio.timeout(settings.request_timeout):
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
            if permit:
                CIRCUITS.success(permit)
            return reply
        except Exception as exc:
            status = status_code(exc)
            event.update(error_type=type(exc).__name__, http_status=status)
            if permit:
                if transient(exc):
                    CIRCUITS.failure(
                        permit, settings.circuit_failure_threshold, settings.circuit_cooldown
                    )
                else:
                    CIRCUITS.success(permit)
            retry = attempt < settings.model_retries and transient(exc)
            event["retry_scheduled"] = retry
            if not retry:
                raise
        except BaseException as exc:
            event.update(
                error_type=type(exc).__name__,
                cancelled=isinstance(exc, asyncio.CancelledError),
                retry_scheduled=False,
            )
            if permit:
                CIRCUITS.abandon(permit, settings.circuit_cooldown)
            raise
        finally:
            event["seconds"] = round(time.perf_counter() - started, 3)
            event["finished_ns"] = time.perf_counter_ns()
        if retry:
            await asyncio.sleep(0.2 * 2**attempt)
    raise ExecutionError("MODEL_RETRY_EXHAUSTED")


async def complete(model, instructions, items, tools, settings, ledger, trace, step):
    actual = getattr(model, "settings", settings)
    key = endpoint_key(actual)
    pinned = key in ledger.local_fallbacks
    try:
        if pinned:
            raise CircuitOpen()
        return await _attempts(model, instructions, items, tools, actual, ledger, trace, step)
    except Exception as exc:
        if (
            not actual.fallback_profile
            or not getattr(model, "network_model", False)
            or not transient(exc)
        ):
            raise
        from fitlife.model import create_model
        from fitlife.model_profiles import configured, resolve
        from fitlife.registry import DOMAIN_READ_TOOLS

        readonly = DOMAIN_READ_TOOLS | {"get_activity_catalog"}
        if any(t["name"] not in readonly for t in tools):
            raise ExecutionError("LOCAL_FALLBACK_REQUIRES_READ_ONLY_TOOLS") from None
        profile = resolve(actual, actual.fallback_profile, capability="tools" if tools else "text")
        if profile.api_family != "ollama":
            raise ExecutionError("FALLBACK_MUST_BE_LOCAL_OLLAMA") from None
        images = any(
            p.get("type") in {"input_image", "image_url"}
            for item in items
            if isinstance(item.get("content"), list)
            for p in item["content"]
        )
        if images:
            raise ExecutionError("LOCAL_FALLBACK_IMAGES_NOT_SUPPORTED") from None
        fallback_settings = configured(actual, profile).model_copy(
            update={
                "fallback_profile": None,
                "model_retries": 0,
                "max_input_bytes": min(actual.max_input_bytes, 12000),
            }
        )
        ledger.local_fallbacks.add(key)
        trace.append(
            {
                "kind": "model_fallback",
                "reason": "run_pinned_to_local" if pinned else type(exc).__name__,
                "from_model": actual.model,
                "requested_model": profile.model,
                "provider": profile.provider,
                "policy": "local-text-readonly-v1",
            }
        )
        fallback = create_model(fallback_settings)
        try:
            reply = await _attempts(
                fallback, instructions, items, tools, fallback_settings, ledger, trace, step
            )
            trace[-1]["local_fallback"] = True
            return reply
        finally:
            await asyncio.wait_for(fallback.close(), 5)
