"""
SETT Framework
==============================
Scalable Expert-based Task Topology

A multi-agent development framework inspired by the Badger Architecture
(Rosa et al., 2019). SETT applies the philosophy of coordinated expert agents
at a macro scale: pre-designed, domain-specialized agents working under a
single orchestrator, each maintaining independent memory, communicating only
final results through a shared universal memory layer.

Author:  Eduardo Daniel Viñales
Paper:   https://doi.org/10.5281/zenodo.21287133 (EN)
         https://doi.org/10.5281/zenodo.21287355 (ES)
         https://doi.org/10.5281/zenodo.21301700 (JP)
LinkedIn: https://www.linkedin.com/in/edudanielv/

Usage:
    from sett import SETTOrchestrator, SETTAgent, SETTExpert, EthicalFilter
"""

import logging as _logging

# Libraries must not choose an application's logging destination. A handler on
# the package logger prevents Python's lastResort handler from writing WARNING+
# records to stderr when the host has not configured logging. Records still
# propagate normally to any handlers the host application installs.
_sett_logger = _logging.getLogger(__name__)
if not any(
    isinstance(handler, _logging.NullHandler)
    for handler in _sett_logger.handlers
):
    _sett_logger.addHandler(_logging.NullHandler())

__version__ = "0.13.2"
__author__ = "Eduardo Daniel Viñales"
__license__ = "MIT"

# Core building blocks
from sett.audit_ruler.trace import TraceEvent, TraceRecorder

# Biometric system
from sett.biometric_ruler.biometric_reading import BiometricReading
from sett.core_ruler.action import Action
from sett.core_ruler.adapter import (
    AdapterCapabilities,
    AdapterErrorKind,
    AdapterErrorView,
    AdapterHandle,
    AdapterHealth,
    AdapterHealthStatus,
    AdapterKind,
    AdapterRequirements,
    AdapterSelection,
    CapabilitySupport,
    ConnectivityRequirement,
    PrioritySelectionPolicy,
    SelectionPolicy,
    select_adapter,
)
from sett.core_ruler.agent import SETTAgent
from sett.core_ruler.execution_context import (
    ExecutionContext,
    TracedResult,
    current_execution_context,
)
from sett.core_ruler.execution_result import (
    ExecutionResult,
    ExecutionState,
    ExecutionStatus,
)
from sett.core_ruler.executor import (
    HandlerContext,
    SETTExecutor,
    current_handler_context,
)
from sett.core_ruler.expert import SETTExpert
from sett.core_ruler.idempotency import (
    ActionAttempt,
    IdempotencyRecord,
    IdempotencyReservation,
    IdempotencyStatus,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    action_fingerprint,
)

# Lifecycle: cancellation, deadlines and structured outcomes
from sett.core_ruler.lifecycle import (
    CancellationSnapshot,
    CancellationSource,
    CancellationToken,
    Deadline,
    ExecutionControl,
    ExecutionControlRegistry,
    LifecyclePolicy,
    current_execution_control,
    lifecycle_scope,
)
from sett.core_ruler.orchestrator import SETTOrchestrator
from sett.core_ruler.phrasing_expert import PhrasingExpert
from sett.core_ruler.pipeline import (
    PipelineResult,
    PipelineStep,
    RejectionOutcome,
    SkipCause,
    StageOutcome,
)
from sett.core_ruler.resilience import (
    CircuitBreaker,
    CircuitBreakerPolicy,
    CircuitState,
    ErrorClass,
    RetryPolicy,
    classify_error,
    execute_with_resilience,
)
from sett.core_ruler.stub_agent import StubDomainAgent
from sett.ethics_ruler.ethic_kernel.context_analyzer import (
    ContextAnalysis,
    ContextAnalyzer,
    SafetyAssessment,
)

# Ethics governance layer
from sett.ethics_ruler.ethic_kernel.filter import EthicalFilter, FilterVerdict
from sett.ethics_ruler.ethic_kernel.rules import (
    EthicalRule,
    EthicalRuleset,
    HarmCategory,
    default_ruleset,
)

# Exceptions
from sett.exceptions import (
    SETTAdapterError,
    SETTAgentNotFoundError,
    SETTCancelledError,
    SETTConfigurationError,
    SETTDependencyError,
    SETTError,
    SETTEthicalFilterRejectedError,
    SETTEthicalFilterWarningError,
    SETTExpertNotFoundError,
    SETTIdempotencyConflictError,
    SETTLLMAdapterError,
    SETTMemoryAccessDeniedError,
    SETTServiceAdapterError,
    SETTTimeoutError,
    SETTValidationError,
)
from sett.memory_ruler.private import PrivateMemory

# Memory layers
from sett.memory_ruler.universal import UniversalMemory
from sett.risk_ruler.environmental_context import EnvironmentalContext

# Risk system: three-layer hybrid evaluation
from sett.risk_ruler.risk_level import RiskLevel
from sett.risk_ruler.risk_profile import RiskProfile

# LLM adapters
from sett.services_llm.base import LLMBase

# Sentiment adapters
from sett.services_sentiment.base import (
    SentenceSentiment,
    SentimentBase,
    SentimentResult,
)

# TTS/STT adapters
from sett.services_tts_stt.base import STTBase, TTSBase

# Public symbols are grouped by subsystem to keep the API reviewable.
__all__ = [  # noqa: RUF022
    # Core
    "SETTOrchestrator",
    "SETTAgent",
    "SETTExpert",
    "PhrasingExpert",
    "Action",
    "SETTExecutor",
    "ExecutionContext",
    "TracedResult",
    "current_execution_context",
    "TraceEvent",
    "TraceRecorder",
    "StubDomainAgent",
    "PipelineStep",
    "PipelineResult",
    "StageOutcome",
    "RejectionOutcome",
    "SkipCause",
    # Lifecycle
    "CancellationSnapshot",
    "CancellationSource",
    "CancellationToken",
    "Deadline",
    "ExecutionControl",
    "ExecutionControlRegistry",
    "LifecyclePolicy",
    "current_execution_control",
    "lifecycle_scope",
    "ExecutionResult",
    "ExecutionState",
    "ExecutionStatus",
    # Idempotency and retries
    "HandlerContext",
    "current_handler_context",
    "ActionAttempt",
    "IdempotencyRecord",
    "IdempotencyReservation",
    "IdempotencyStatus",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "action_fingerprint",
    "ErrorClass",
    "RetryPolicy",
    "classify_error",
    "CircuitBreaker",
    "CircuitBreakerPolicy",
    "CircuitState",
    "execute_with_resilience",
    # Adapter contracts
    "AdapterCapabilities",
    "AdapterErrorKind",
    "AdapterErrorView",
    "AdapterHandle",
    "AdapterHealth",
    "AdapterHealthStatus",
    "AdapterKind",
    "AdapterRequirements",
    "AdapterSelection",
    "CapabilitySupport",
    "ConnectivityRequirement",
    "PrioritySelectionPolicy",
    "SelectionPolicy",
    "select_adapter",
    # Memory
    "UniversalMemory",
    "PrivateMemory",
    # Ethics
    "EthicalFilter",
    "FilterVerdict",
    "EthicalRuleset",
    "EthicalRule",
    "HarmCategory",
    "default_ruleset",
    "ContextAnalyzer",
    "ContextAnalysis",
    "SafetyAssessment",
    # Risk system
    "RiskLevel",
    "RiskProfile",
    "EnvironmentalContext",
    # Biometric system
    "BiometricReading",
    # LLM
    "LLMBase",
    # TTS/STT
    "TTSBase",
    "STTBase",
    # Sentiment
    "SentimentBase",
    "SentimentResult",
    "SentenceSentiment",
    # Exceptions
    "SETTError",
    "SETTAdapterError",
    "SETTEthicalFilterRejectedError",
    "SETTEthicalFilterWarningError",
    "SETTMemoryAccessDeniedError",
    "SETTAgentNotFoundError",
    "SETTExpertNotFoundError",
    "SETTLLMAdapterError",
    "SETTServiceAdapterError",
    "SETTConfigurationError",
    "SETTValidationError",
    "SETTCancelledError",
    "SETTTimeoutError",
    "SETTDependencyError",
    "SETTIdempotencyConflictError",
]
