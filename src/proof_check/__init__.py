"""Proof Check deterministic core.

Everything in this package is pure computation over inputs handed to it: no
network, no GitHub calls, no clock reads. The public contract it obeys lives in
``docs/contract/`` and ``schemas/`` of the repository; code follows the
contract, not the other way round.
"""

from proof_check.canonical import CanonicalizationError, canonical_bytes, canonical_sha256, sha256_hex
from proof_check.contracts import (
    ContractError,
    DetailCode,
    PolicyDocument,
    ReasonCode,
    Receipt,
    SchemaViolation,
    Verdict,
    validate_policy,
    validate_receipt,
)
from proof_check.scope import PathRefusal, ScopeRefusal, compile_scope, parse_pr_body_allowlist
from proof_check.verify import (
    ConfigurationError,
    Evaluation,
    Evidence,
    VerificationResult,
    build_receipt,
    evaluate,
    verify_receipt,
)

__version__ = "0.0.0"

__all__ = [
    "CanonicalizationError",
    "ConfigurationError",
    "ContractError",
    "DetailCode",
    "Evaluation",
    "Evidence",
    "PathRefusal",
    "PolicyDocument",
    "ReasonCode",
    "Receipt",
    "SchemaViolation",
    "ScopeRefusal",
    "VerificationResult",
    "Verdict",
    "build_receipt",
    "canonical_bytes",
    "canonical_sha256",
    "compile_scope",
    "evaluate",
    "parse_pr_body_allowlist",
    "sha256_hex",
    "validate_policy",
    "validate_receipt",
    "verify_receipt",
]
