"""Common interface every source implements. Deliberately tiny."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Event


class Source(ABC):
    @abstractmethod
    def collect(self) -> list[Event]:
        """Fetch and normalize this run's items into Events. Must not raise
        on ordinary network hiccups — log and return [] instead, so one flaky
        source never takes down the whole collection run."""
        raise NotImplementedError
