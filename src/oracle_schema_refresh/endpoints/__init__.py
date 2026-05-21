"""Endpoint abstraction — named, reusable Oracle connection targets.

An ``Endpoint`` is the unit a future cross-host job operates over. For the
same-instance refresh case today, source and target endpoints resolve to the
same DSN — no behaviour change.

See ``docs/redesign.md`` §2.2.
"""
from __future__ import annotations

from oracle_schema_refresh.endpoints.endpoint import Endpoint

__all__ = ["Endpoint"]
