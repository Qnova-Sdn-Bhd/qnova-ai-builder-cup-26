from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Actor:
    user_id: str
    binding_id: str
    binding_version: int


@dataclass(frozen=True)
class ProviderResponse:
    result: dict
    provenance: dict


class UnitOfWork(Protocol):
    def get(self, path: str) -> dict | None: ...
    def list(self, path: str, limit: int = 1000) -> list[dict]: ...
    def set(self, path: str, value: dict) -> None: ...
    def create(self, path: str, value: dict) -> None: ...
    def where(self, path: str, field: str, value: Any, limit: int = 100) -> list[dict]: ...


class Repository(UnitOfWork, Protocol):
    def transact(self, callback: Callable[[UnitOfWork], Any]) -> Any: ...
    def accessible_projects(self, user_id: str) -> list[str]: ...
    def where(self, path: str, field: str, value: Any, limit: int = 100) -> list[dict]: ...
    def recent(self, path: str, field: str, limit: int) -> list[dict]: ...


class IdentityProvider(Protocol):
    def verify(self, token: str, privileged: bool) -> str: ...


class SourceStore(Protocol):
    def put(self, name: str, content: bytes, mime_type: str) -> dict: ...
    def read(self, ref: dict) -> bytes: ...


class JobDispatcher(Protocol):
    def enqueue(self, job_id: str, generation: int) -> None: ...


class AnalysisProvider(Protocol):
    async def analyze(self, packet: dict, attempt_id: str) -> ProviderResponse: ...
