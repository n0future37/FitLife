"""进程内模型熔断器；跨事件循环线程安全，半开时只放行一个探测。"""

import hashlib
import json
import threading
import time
from dataclasses import dataclass


class CircuitOpen(RuntimeError):
    def __init__(self):
        super().__init__("MODEL_CIRCUIT_OPEN")


def endpoint_key(settings):
    value = [
        settings.provider,
        settings.base_url,
        settings.model,
        settings.key_env,
        str(settings.key_file),
        settings.reasoning_effort,
        settings.enable_thinking,
        settings.thinking_budget,
    ]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


@dataclass
class State:
    failures: int = 0
    generation: int = 0
    opened_until: float = 0
    probing: bool = False


class Circuits:
    def __init__(self, clock=time.monotonic):
        self.clock, self.states, self.lock = clock, {}, threading.Lock()

    def acquire(self, key):
        with self.lock:
            state = self.states.setdefault(key, State())
            if state.opened_until:
                if self.clock() < state.opened_until or state.probing:
                    raise CircuitOpen()
                state.probing = True
                return key, state.generation, True
            return key, state.generation, False

    def success(self, permit):
        key, generation, _ = permit
        with self.lock:
            state = self.states[key]
            if state.generation == generation:
                state.failures, state.opened_until, state.probing = 0, 0, False

    def failure(self, permit, threshold, cooldown):
        key, generation, probe = permit
        with self.lock:
            state = self.states[key]
            if state.generation != generation:
                return
            state.failures += 1
            if probe or state.failures >= threshold:
                state.opened_until = self.clock() + cooldown
                state.probing = False
                state.generation += 1

    def abandon(self, permit, cooldown):
        key, generation, probe = permit
        if probe:
            with self.lock:
                state = self.states[key]
                if state.generation == generation:
                    state.probing = False
                    state.opened_until = self.clock() + cooldown
                    state.generation += 1


CIRCUITS = Circuits()
