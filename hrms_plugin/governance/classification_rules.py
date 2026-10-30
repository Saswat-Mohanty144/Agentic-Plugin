"""PII classification rules table loader and evaluator (piece K1, decisions K1.1-K1.2).

Loads the YAML decision table once (cached) with yaml.safe_load and evaluates candidate
mappings against canonical rules and fallback identifier / money detectors.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

import yaml

from hrms_plugin.security import pii

DATA_CLASSES = ("PII", "COMPENSATION", "INTERNAL", "PUBLIC")
SENSITIVE_CLASSES = ("PII", "COMPENSATION")

DEFAULT_RULES_PATH = Path(__file__).with_name("classification_rules.yaml")
ALLOWED_WHEN_KEYS = frozenset({"entity_in", "target_in", "target_kind_in"})
ALLOWED_THEN_KEYS = frozenset({"data_class", "llm_visible"})
ALLOWED_TARGET_KINDS = frozenset({"IDENTIFIER", "MONEY"})

_CACHED_RULES: Optional[ClassificationRules] = None


class ClassificationRulesError(Exception):
    """Raised when classification rules cannot be loaded or parsed."""


class ClassificationRules:
    """Parsed classification rules table with first-hit evaluation."""

    def __init__(self, version: int, hit_policy: str, rules: List[Dict[str, Any]]) -> None:
        self.version = version
        self.hit_policy = hit_policy
        self.rules = rules

    def evaluate(self, target: str, entity_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Evaluate a target field (and optional entity type) against the rules in order."""
        target_clean = str(target or "").strip()
        if not target_clean:
            return None

        for rule in self.rules:
            when = rule["when"]

            # 1. entity_in
            if "entity_in" in when:
                if not entity_type:
                    continue
                allowed_entities = {e.upper() for e in when["entity_in"]}
                if entity_type.upper() not in allowed_entities:
                    continue

            # 2. target_in
            if "target_in" in when:
                allowed_targets = {t.lower() for t in when["target_in"]}
                if target_clean.lower() not in allowed_targets:
                    continue

            # 3. target_kind_in
            if "target_kind_in" in when:
                kinds: Set[str] = set()
                if pii.identifier_kind(target_clean) is not None:
                    kinds.add("IDENTIFIER")
                if pii.is_compensation_key(target_clean):
                    kinds.add("MONEY")
                if not (set(when["target_kind_in"]) & kinds):
                    continue

            then = rule["then"]
            return {
                "data_class": then["data_class"],
                "llm_visible": then["llm_visible"],
                "rule_id": rule["id"],
                "why": rule["why"],
            }

        return None


def _parse_rules_file(path: Path) -> ClassificationRules:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ClassificationRulesError(f"could not read rules file: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ClassificationRulesError(f"invalid YAML syntax: {exc}") from exc

    if not isinstance(data, dict):
        raise ClassificationRulesError("rules root must be a mapping")

    version = data.get("version")
    if version != 1:
        raise ClassificationRulesError(f"invalid version: expected 1, got {version}")

    hit_policy = data.get("hit_policy")
    if hit_policy != "first":
        raise ClassificationRulesError(f"unknown hit_policy: {hit_policy!r}, expected 'first'")

    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list):
        raise ClassificationRulesError("rules must be a list")

    seen_ids: Set[str] = set()
    validated_rules: List[Dict[str, Any]] = []

    for idx, rule in enumerate(raw_rules):
        if not isinstance(rule, dict):
            raise ClassificationRulesError(f"rule #{idx} must be a mapping")

        rule_id = rule.get("id")
        if not rule_id or not isinstance(rule_id, str):
            raise ClassificationRulesError(f"rule #{idx} missing valid id")
        if rule_id in seen_ids:
            raise ClassificationRulesError(f"duplicate rule id: {rule_id!r}")
        seen_ids.add(rule_id)

        why = rule.get("why")
        if not why or not isinstance(why, str):
            raise ClassificationRulesError(f"rule {rule_id!r} missing why explanation")

        when = rule.get("when")
        if not isinstance(when, dict) or not when:
            raise ClassificationRulesError(f"empty when in rule {rule_id!r}")

        unknown_when = set(when) - ALLOWED_WHEN_KEYS
        if unknown_when:
            raise ClassificationRulesError(f"unknown when keys in rule {rule_id!r}: {sorted(unknown_when)}")

        if "target_kind_in" in when:
            kinds = when["target_kind_in"]
            if not isinstance(kinds, (list, tuple, set)) or not kinds:
                raise ClassificationRulesError(f"invalid target_kind_in in rule {rule_id!r}")
            unknown_kinds = set(kinds) - ALLOWED_TARGET_KINDS
            if unknown_kinds:
                raise ClassificationRulesError(f"unknown target_kind_in values in {rule_id!r}: {sorted(unknown_kinds)}")

        then = rule.get("then")
        if not isinstance(then, dict):
            raise ClassificationRulesError(f"rule {rule_id!r} missing then mapping")

        if set(then) != ALLOWED_THEN_KEYS:
            unknown_then = set(then) - ALLOWED_THEN_KEYS
            missing_then = ALLOWED_THEN_KEYS - set(then)
            raise ClassificationRulesError(
                f"unknown or missing then keys in rule {rule_id!r}: "
                f"unknown={sorted(unknown_then)}, missing={sorted(missing_then)}"
            )

        data_class = then["data_class"]
        if data_class not in DATA_CLASSES:
            raise ClassificationRulesError(f"unknown class in rule {rule_id!r}: {data_class!r}")

        llm_visible = then["llm_visible"]
        if not isinstance(llm_visible, bool):
            raise ClassificationRulesError(f"llm_visible must be a bool in rule {rule_id!r}")

        if llm_visible and data_class in SENSITIVE_CLASSES:
            raise ClassificationRulesError(
                f"llm_visible: true not allowed for sensitive class {data_class} in {rule_id!r}"
            )

        validated_rules.append({
            "id": rule_id,
            "when": when,
            "then": {"data_class": data_class, "llm_visible": llm_visible},
            "why": why,
        })

    return ClassificationRules(version=version, hit_policy=hit_policy, rules=validated_rules)


def load_rules(path: Optional[Union[Path, str]] = None) -> ClassificationRules:
    """Load and validate rules from YAML file. Default rules file is cached once loaded."""
    global _CACHED_RULES
    if path is None:
        if _CACHED_RULES is None:
            _CACHED_RULES = _parse_rules_file(DEFAULT_RULES_PATH)
        return _CACHED_RULES

    resolved = Path(path).resolve()
    if resolved == DEFAULT_RULES_PATH.resolve():
        if _CACHED_RULES is None:
            _CACHED_RULES = _parse_rules_file(DEFAULT_RULES_PATH)
        return _CACHED_RULES

    return _parse_rules_file(resolved)


def suggest_classification(
    target: str,
    entity_type: Optional[str] = None,
    rules: Optional[ClassificationRules] = None,
) -> Optional[Dict[str, Any]]:
    """Suggest data_class and llm_visible for a target canonical field based on governance rules."""
    active_rules = rules or load_rules()
    return active_rules.evaluate(target, entity_type)
