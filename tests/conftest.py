"""Shared test configuration."""

from __future__ import annotations

from hypothesis import HealthCheck, settings

settings.register_profile("ci", max_examples=300, suppress_health_check=[HealthCheck.too_slow])
settings.register_profile("quick", max_examples=50)
settings.load_profile("ci")
