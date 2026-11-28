"""Contract-checked dispatch and separation of visible results from effect evidence."""

from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter_ns
from typing import Literal

from toolswap.contracts import JsonObject, Reference
from toolswap.determinism import stable_id
from toolswap.recovery.records import Observation, ToolCall
from toolswap.service_tools import ToolError, ToolRegistry, ToolResult
from toolswap.state import StateStore

type Timeout = Literal["before", "after"]


@dataclass(frozen=True)
class Dispatch:
    visible: Observation
    actual: ToolResult | None
    before: str
    after: str
    timeout: Timeout | None
    elapsed_ns: int


class Gateway:
    def __init__(self, store: StateStore, registry: ToolRegistry, run_id: str, seed: int):
        self.store = store
        self.registry = registry
        self.run_id = run_id
        self.seed = seed
        self.sequence = 0

    def execute(
        self,
        tool: Reference,
        arguments: JsonObject,
        *,
        request_key: str | None = None,
        timeout: Timeout | None = None,
        record_call: Callable[[str, ToolCall, str], None] | None = None,
    ) -> Dispatch:
        contract = self.registry.contract(tool)
        call = ToolCall(
            call_id=stable_id("call", self.seed, self.run_id, self.sequence),
            tool=tool,
            arguments=arguments,
            request_key=request_key,
        )
        self.sequence += 1
        before = self.store.current_state().state_hash
        if record_call is not None:
            record_call("tool_attempt", call, before)
        actual = None
        observation = None
        error: JsonObject | None = None
        started = perf_counter_ns()
        try:
            self.registry.validate_call(tool, arguments, request_key=request_key)
        except ToolError as exc:
            error = {"code": exc.code, "message": str(exc)}
            timeout = None
        else:
            if timeout != "before":
                if record_call is not None:
                    record_call("tool_dispatch", call, before)
                actual = self.registry.invoke(self.store, tool, arguments, request_key=request_key)
                observation, error = actual.observation, actual.error
            if timeout == "before" or (timeout == "after" and error is None):
                observation = None
                error = {"code": "timeout", "message": "Tool response timed out"}
            else:
                timeout = None
        elapsed_ns = perf_counter_ns() - started
        retryable = error is not None and any(
            error["code"] == failure.code and failure.retryable
            for failure in contract.failure_modes
        )
        return Dispatch(
            Observation(call=call, result=observation, error=error, retryable=retryable),
            actual,
            before,
            self.store.current_state().state_hash,
            timeout,
            elapsed_ns,
        )
