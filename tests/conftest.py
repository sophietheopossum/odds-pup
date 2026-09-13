"""Shared test configuration."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

from odds_pup.storage import DataPaths, Repository
from tests.storage_helpers import FakeClock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

settings.register_profile("ci", max_examples=300, suppress_health_check=[HealthCheck.too_slow])
settings.register_profile("quick", max_examples=50)
settings.load_profile("ci")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    return DataPaths(tmp_path / "data")


@pytest.fixture
def repo(paths: DataPaths, clock: FakeClock) -> Iterator[Repository]:
    repository = Repository.open(paths, clock=clock, backups=False)
    yield repository
    repository.close()
