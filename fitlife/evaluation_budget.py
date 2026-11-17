"""本轮实模验收的保守预留账本；不是平台实扣账单或任意平台通用单价。"""

import json
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fitlife.execution import ExecutionError
from fitlife.model import create_model

# Public maximum listed standard group rates checked 2026-11-02; no priority requested.
RATES = {"gpt-5.6-sol": ("5", "22.5"), "gpt-6-luna": ("0.25", "0.9375")}
SOURCES = [
    "https://modelflare.dev/pricing/gpt-5.6-sol",
    "https://modelflare.dev/pricing/gpt-6-luna",
]


class PaidBudget:
    def __init__(self, path: Path, limit="1.5"):
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {
                "policy_version": 2,
                "limit_usd": str(limit),
                "reserved_usd": "0",
                "attempts": [],
                "price_sources": SOURCES,
                "actual_billed_usd": None,
                "note": (
                    "Conservative reservation with input-byte factor 4, overhead and 25% margin; "
                    "not platform billing."
                ),
            }
        )
        if Decimal(self.state["limit_usd"]) > Decimal(limit):
            raise ValueError("EXISTING_BUDGET_LIMIT_MISMATCH")
        if self.state.get("policy_version", 1) == 1:
            backup = self.path.with_name(self.path.stem + ".policy-v1.json")
            if not backup.exists():
                backup.write_text(
                    json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            total = Decimal(0)
            for event in self.state["attempts"]:
                known = event.get("maximum_group_rate_estimate_usd")
                held = (
                    Decimal(known) * Decimal("1.25")
                    if known is not None and Decimal(known) > 0
                    else Decimal(event["reserved_usd"])
                )
                event["held_after_usage_usd"] = str(held)
                total += held
            self.state.update(
                policy_version=2,
                reserved_usd=str(total),
                note=(
                    "Preflight reserves unknown usage; completed known usage holds "
                    "maximum public group price plus 25% margin. Not platform billing."
                ),
            )
            self._save()

    def _save(self):
        self.state["effective_ceiling_used_usd"] = str(self.effective_used())
        temp = self.path.with_suffix("." + uuid4().hex + ".tmp")
        temp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def effective_used(self):
        checkpoints = self.state.get("billing_checkpoints", [])
        if not checkpoints:
            return Decimal(self.state["reserved_usd"])
        last = checkpoints[-1]
        return Decimal(last["cumulative_actual_usd"]) + sum(
            (
                Decimal(e.get("held_after_usage_usd", e["reserved_usd"]))
                for e in self.state["attempts"][last["through_attempt_count"] :]
            ),
            Decimal(0),
        )

    def acknowledge_bill(self, total, *, source, evidence):
        """仅可信本机入口根据明确账单信息调用；不清空调用记录或提高授权上限。"""
        amount = Decimal(total)
        if (
            not amount.is_finite()
            or amount < 0
            or source not in {"user_reported", "provider_statement"}
            or not evidence
        ):
            raise ValueError("INVALID_BILLING_CHECKPOINT")
        with self.lock:
            if any(e.get("usage") is None for e in self.state["attempts"]):
                raise ValueError("INCOMPLETE_ATTEMPTS_REQUIRE_REVIEW")
            checkpoint = dict(
                cumulative_actual_usd=str(amount),
                source=source,
                evidence=evidence,
                through_attempt_count=len(self.state["attempts"]),
                recorded_at=datetime.now(UTC).isoformat(),
            )
            self.state.setdefault("billing_checkpoints", []).append(checkpoint)
            self._save()

    def reserve(self, settings, instructions, items, tools):
        if (
            settings.base_url.rstrip("/") != "https://modelflare.dev/v1"
            or settings.model not in RATES
        ):
            raise ExecutionError("UNPRICED_EVALUATION_MODEL")
        input_bytes = len(json.dumps([instructions, items, tools], ensure_ascii=False).encode())
        if input_bytes > 80000:
            raise ExecutionError("PAID_EVALUATION_INPUT_TOO_LARGE")
        rate_in, rate_out = map(Decimal, RATES[settings.model])
        reserved = (
            ((4 * input_bytes + 10000) * rate_in + settings.max_output_tokens * rate_out)
            / Decimal(1000000)
            * Decimal("1.25")
        )
        with self.lock:
            if self.effective_used() + reserved > Decimal(self.state["limit_usd"]):
                raise ExecutionError("PAID_EVALUATION_BUDGET_EXHAUSTED")
            index = len(self.state["attempts"])
            self.state["reserved_usd"] = str(Decimal(self.state["reserved_usd"]) + reserved)
            self.state["attempts"].append(
                {
                    "model": settings.model,
                    "input_bytes": input_bytes,
                    "reserved_usd": str(reserved),
                    "usage": None,
                    "maximum_group_rate_estimate_usd": None,
                }
            )
            self._save()
            return index

    def usage(self, index, usage, request_id=None, reported_model=None):
        with self.lock:
            event = self.state["attempts"][index]
            event["usage"] = usage
            if reported_model:
                event["reported_model"] = reported_model
            event["provider_request_id"] = request_id
            if (
                usage
                and isinstance(usage.get("input_tokens"), int)
                and isinstance(usage.get("output_tokens"), int)
            ):
                rate_in, rate_out = map(Decimal, RATES[event["model"]])
                cost = (
                    usage["input_tokens"] * rate_in + usage["output_tokens"] * rate_out
                ) / Decimal(1000000)
                event["maximum_group_rate_estimate_usd"] = str(cost)
                if cost > 0:
                    previous = Decimal(event.get("held_after_usage_usd", event["reserved_usd"]))
                    held = cost * Decimal("1.25")
                    self.state["reserved_usd"] = str(
                        Decimal(self.state["reserved_usd"]) + held - previous
                    )
                    event["held_after_usage_usd"] = str(held)
            self._save()

    def factory(self, settings):
        model = create_model(settings)
        budget = self

        class Budgeted:
            network_model = True

            def __init__(self):
                self.settings = settings

            async def complete(self, instructions, items, tools):
                index = budget.reserve(settings, instructions, items, tools)
                started = time.perf_counter()
                try:
                    reply = await model.complete(instructions, items, tools)
                    budget.usage(index, reply.usage, reply.request_id, reply.response_model)
                    return reply
                finally:
                    with budget.lock:
                        budget.state["attempts"][index]["seconds"] = round(
                            time.perf_counter() - started, 3
                        )
                        budget._save()

            async def close(self):
                await model.close()

        return Budgeted()
