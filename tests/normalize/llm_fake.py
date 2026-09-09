from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class FakeLLMClient:
    """In-memory ``LLMClient`` for tests. Structurally conforms to the protocol."""

    def __init__(
        self,
        response: BaseModel | None = None,
        error: Exception | None = None,
    ) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def parse(self, *, system: str, user: str, output_model: type[T]) -> T:
        self.calls.append({"system": system, "user": user, "output_model": output_model})
        if self._error is not None:
            raise self._error
        assert isinstance(self._response, BaseModel), "FakeLLMClient has no response set"
        return self._response  # type: ignore[return-value]  # test supplies matching model
