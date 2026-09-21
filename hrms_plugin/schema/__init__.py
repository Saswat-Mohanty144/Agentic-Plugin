"""Schema package for hrms-agentic-plugin: Introspection, Canonical Models, Synthesis, and Probing."""

from hrms_plugin.schema.canonical import (
    CanonicalAsset,
    CanonicalCandidate,
    CanonicalEmployee,
    CanonicalError,
    CanonicalLeaveBalance,
    CanonicalLeaveRequest,
    CanonicalPunch,
    CanonicalRequisition,
    EntityType,
    SourceRef,
)
from hrms_plugin.schema.introspector import (
    DiscoveredEntity,
    DiscoveredField,
    FieldDataType,
    IntrospectionResult,
    SchemaIntrospector,
)
from hrms_plugin.schema.mapping import (
    TRANSFORMS,
    EntityMapping,
    FieldMap,
    MappingError,
    resolve_path,
)
from hrms_plugin.schema.prober import (
    IssueSeverity,
    ProbeIssue,
    ProbeReport,
    ShadowProber,
)
from hrms_plugin.schema.synthesizer import (
    MappingSynthesizer,
    SynthesisReport,
    SynthesizedFieldMap,
)

__all__ = [
    # Canonical
    "CanonicalAsset",
    "CanonicalCandidate",
    "CanonicalEmployee",
    "CanonicalError",
    "CanonicalLeaveBalance",
    "CanonicalLeaveRequest",
    "CanonicalPunch",
    "CanonicalRequisition",
    "EntityType",
    "SourceRef",
    # Introspector
    "DiscoveredEntity",
    "DiscoveredField",
    "FieldDataType",
    "IntrospectionResult",
    "SchemaIntrospector",
    # Mapping
    "EntityMapping",
    "FieldMap",
    "MappingError",
    "TRANSFORMS",
    "resolve_path",
    # Synthesizer
    "MappingSynthesizer",
    "SynthesisReport",
    "SynthesizedFieldMap",
    # Prober
    "IssueSeverity",
    "ProbeIssue",
    "ProbeReport",
    "ShadowProber",
]
