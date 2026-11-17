"""实验专用跨进程人民币账本；整批上限不随进程/重试重置，未知用量保留预留。"""

import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fitlife.model import create_model


@contextmanager
def file_lock(path):
    with path.open("a+b") as file:
        if not file.tell():
            file.write(b"0")
            file.flush()
        deadline = time.monotonic() + 10
        while True:
            file.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("EXPERIMENT_BUDGET_LOCK_TIMEOUT") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


class ExperimentBudget:
    def __init__(self, path, prices, limit="5", tag=None, response_dir=None):
        self.response_dir = Path(response_dir) if response_dir else None
        self.path, self.prices, self.limit = Path(path), prices, str(limit)
        self.tag, self.local_calls, self.local_lock = tag, 0, threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path.with_suffix(".lock")):
            if not self.path.exists():
                self.save(
                    {
                        "version": "experiment-cny-v1",
                        "limit_cny": self.limit,
                        "attempts": [],
                        "actual_billed_cny": None,
                        "price_source": "https://www.siliconflow.cn/models",
                    }
                )
            assert self.read()["limit_cny"] == self.limit, "BUDGET_LIMIT_MISMATCH"

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, state):
        temp = self.path.with_name(self.path.name + "." + str(uuid4()) + ".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def reserve(self, settings):
        if (
            settings.provider != "siliconflow"
            or settings.base_url.rstrip("/") != "https://api.siliconflow.cn/v1"
            or settings.model not in self.prices
        ):
            raise ValueError("UNPRICED_EXPERIMENT_MODEL")
        price = self.prices[settings.model]
        rates = price["rates_cny_per_million"]
        hold = (
            (
                Decimal(price["context_length"]) * Decimal(rates["prompt"])
                + (settings.max_output_tokens + (settings.thinking_budget or 0))
                * Decimal(rates["completion"])
            )
            / 1000000
            * Decimal("1.25")
        )
        with file_lock(self.path.with_suffix(".lock")):
            state = self.read()
            if (
                sum(Decimal(e["held_cny"]) for e in state["attempts"]) + hold > Decimal(self.limit)
                or len(state["attempts"]) >= 1200
            ):
                raise ValueError("EXPERIMENT_BUDGET_EXHAUSTED")
            id_ = str(uuid4())
            state["attempts"].append(
                {
                    "id": id_,
                    "model": settings.model,
                    "held_cny": str(hold),
                    "reserved_cny": str(hold),
                    "usage": None,
                    "rates": rates,
                    "episode": self.tag,
                }
            )
            self.save(state)
            with self.local_lock:
                self.local_calls += 1
            return id_

    def settle(self, id_, reply):
        with file_lock(self.path.with_suffix(".lock")):
            state = self.read()
            event = next(e for e in state["attempts"] if e["id"] == id_)
            event.update(
                usage=reply.usage,
                requested_model=event["model"],
                response_model=reply.response_model,
                request_id=reply.request_id,
            )
            usage = reply.usage or {}
            if all(
                type(usage.get(k)) is int and usage[k] >= 0
                for k in ("input_tokens", "output_tokens")
            ):
                cost = (
                    usage["input_tokens"] * Decimal(event["rates"]["prompt"])
                    + usage["output_tokens"] * Decimal(event["rates"]["completion"])
                ) / 1000000
                event.update(estimated_cny=str(cost), held_cny=str(cost * Decimal("1.25")))
            self.save(state)

    def factory(self, settings):
        underlying, budget = create_model(settings), self

        class Budgeted:
            network_model = True

            def __init__(self):
                self.settings = settings

            async def complete(self, instructions, items, tools):
                id_ = budget.reserve(settings)
                try:
                    reply = await underlying.complete(instructions, items, tools)
                except Exception as exc:
                    with file_lock(budget.path.with_suffix(".lock")):
                        state = budget.read()
                        event = next(e for e in state["attempts"] if e["id"] == id_)
                        event.update(
                            error_type=type(exc).__name__,
                            http_status=getattr(exc, "status_code", None),
                        )
                        body = getattr(exc, "body", {})
                        text = json.dumps(body, ensure_ascii=False, default=str).lower()
                        # Only provider code/message; never request bodies, headers, or credentials.
                        detail = body.get("error", body) if isinstance(body, dict) else {}
                        if isinstance(detail, dict):
                            message = str(detail.get("message", ""))[:400]
                            key = os.getenv(settings.key_env, "")
                            if key:
                                message = message.replace(key, "[redacted]")
                            event["provider_error_code"] = str(detail.get("code", ""))[:60]
                            event["provider_error_message"] = re.sub(
                                r"sk-[A-Za-z0-9_-]+", "[redacted]", message
                            )
                        event["error_category"] = (
                            "tpm_limit"
                            if "tpm" in text
                            else "rpm_limit"
                            if "rpm" in text
                            else "rate_limit"
                            if getattr(exc, "status_code", None) == 429
                            else "provider_error"
                        )
                        budget.save(state)
                    raise
                budget.settle(id_, reply)
                if budget.response_dir:
                    budget.response_dir.mkdir(parents=True, exist_ok=True)
                    (budget.response_dir / (id_ + ".json")).write_text(
                        json.dumps(
                            {
                                "requested_model": settings.model,
                                "instructions_sha256": hashlib.sha256(
                                    instructions.encode()
                                ).hexdigest(),
                                "tool_names": [t["name"] for t in tools],
                                "text": reply.text,
                                "calls": reply.calls,
                                "usage": reply.usage,
                                "request_id": reply.request_id,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                return reply

            async def close(self):
                await underlying.close()

        return Budgeted()
