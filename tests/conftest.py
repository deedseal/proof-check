"""Shared helpers: load the vector corpus and turn one vector into typed inputs."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from proof_check.verify import Evidence

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "cae46565153151beca2a53603aed4e7488552ff0"


def deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = deep_merge(base[key], value) if key in base else copy.deepcopy(value)
        return merged
    return copy.deepcopy(override)


def load_corpus() -> dict[str, Any]:
    with (FIXTURES / "scope_vectors.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def materialize(corpus: dict[str, Any], vector: dict[str, Any]) -> tuple[dict[str, Any], Evidence]:
    """Return the policy mapping and typed evidence for one vector."""
    policy = deep_merge(corpus["defaults"]["policy"], vector.get("policy", {}))
    evidence = deep_merge(corpus["defaults"]["evidence"], vector.get("evidence", {}))
    return policy, Evidence.from_dict(evidence)


CORPUS = load_corpus()
VECTORS = CORPUS["vectors"]


@pytest.fixture(scope="session")
def corpus() -> dict[str, Any]:
    return CORPUS


def vector_params():
    return [pytest.param(vector, id=vector["id"]) for vector in VECTORS]
