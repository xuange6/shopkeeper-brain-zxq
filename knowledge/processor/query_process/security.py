"""Security policy helpers for untrusted query, retrieval, and model output text.

The policy is deliberately deterministic at the authorization boundary.  It
uses more than literal regular expressions: Unicode/control normalization,
bounded URL/Base64/hex inspection, compact-token matching, fuzzy typo matching,
multi-label signals, and a second inspection of any sanitized business query.
Models may assist classification in the future, but they must never grant
access to sensitive material.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from difflib import SequenceMatcher
import html
import re
import unicodedata
from typing import Any, Dict, Iterable, List
from urllib.parse import unquote


_DIRECT_INJECTION = re.compile(
    r"(?:"
    r"忽略(?:此前|之前|以上|所有|系统)?(?:的)?(?:要求|指令|提示|规则)?|"
    r"无视(?:此前|之前|以上|所有|系统)?(?:的)?(?:要求|指令|提示|规则)?|"
    r"(?:绕过|覆盖|取消)(?:安全|系统|此前|之前)?(?:规则|限制|指令|提示)|"
    r"切换到(?:开发者|管理员|越狱)模式|越狱|"
    r"ignore\s+(?:all\s+)?(?:previous|prior|above|system)(?:\s+instructions?)?|"
    r"(?:bypass|override|disregard)\s+(?:the\s+)?(?:safety|system|previous|instructions?)|"
    r"you\s+are\s+now\s+(?:in\s+)?developer\s+mode|"
    r"STAGE\d+_INJECTION_SUCCEEDED"
    r")",
    re.IGNORECASE,
)

_FRESHNESS = re.compile(
    r"(?:截至(?:今天|现在)|今天|当前|最新|实时|现价|售价|价格|库存|汇率|新闻|版本)",
    re.IGNORECASE,
)

_PROTECTIVE_CONTEXT = re.compile(
    r"(?:如何防止|如何防范|如何保护|避免|检测|识别|审计|轮换|吊销|撤销|脱敏|"
    r"安全存储|泄露后|被泄露|什么是|解释|防御|缓解|"
    r"prevent|protect|detect|audit|rotate|revoke|redact|securely\s+store|"
    r"what\s+is|explain|mitigat)",
    re.IGNORECASE,
)

_EXFILTRATION_ACTION = re.compile(
    r"(?:给我|告诉我|发给我|展示给我|输出|显示|打印|导出|读取|泄露|窃取|"
    r"show\s+me|tell\s+me|give\s+me|reveal|dump|print|export|steal|extract)",
    re.IGNORECASE,
)

_POSSESSION_REQUEST = re.compile(
    r"(?:给我|告诉我|发给我|展示给我|输出|显示|打印|导出|读取|窃取|"
    r"show\s+me|tell\s+me|give\s+me|reveal|dump|print|export|steal|extract)",
    re.IGNORECASE,
)

_CREDENTIAL_VALUE_PATTERNS = (
    ("openai_style_key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")),
    ("aws_access_key", re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "assigned_secret",
        re.compile(
            r"(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd)"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+\-=]{8,}",
            re.IGNORECASE,
        ),
    ),
)

_SECRET_COMPACT_TERMS = (
    "apikey",
    "accesstoken",
    "clientsecret",
    "privatekey",
    "systemprompt",
    "developermessage",
    "environmentvariable",
    "数据库密码",
    "系统提示词",
    "开发者消息",
    "环境变量",
    "密码",
    "口令",
    "密钥",
    "令牌",
)

_POSSESSION_COMPACT_TERMS = (
    "给我",
    "告诉我",
    "发给我",
    "展示给我",
    "输出",
    "显示",
    "打印",
    "导出",
    "读取",
    "窃取",
    "showme",
    "tellme",
    "giveme",
    "reveal",
    "dump",
    "print",
    "export",
    "steal",
    "extract",
)

_FUZZY_TARGETS = {
    "ignore": ("ignore",),
    "previous": ("previous", "prior", "above"),
    "instructions": ("instruction", "instructions", "rules"),
    "bypass": ("bypass", "override", "disregard"),
    "system": ("system",),
    "prompt": ("prompt",),
    "reveal": ("reveal", "dump", "export", "steal"),
}


@dataclass(frozen=True)
class NormalizedSecurityText:
    normalized: str
    inspection_text: str
    compact: str
    tokens: tuple[str, ...]
    metadata: Dict[str, Any]


def normalize_security_text(
    value: Any,
    *,
    max_chars: int = 4000,
    inspect_encodings: bool = True,
    max_decoded_payloads: int = 4,
) -> NormalizedSecurityText:
    """Return bounded normalized/decoded views without retaining decoded secrets."""

    raw = str(value or "")[: max(1, int(max_chars))]
    normalized = unicodedata.normalize("NFKC", html.unescape(raw))
    removed_controls = 0
    cleaned: List[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category in {"Cf", "Cc"} and character not in {"\n", "\r", "\t"}:
            removed_controls += 1
            continue
        cleaned.append(" " if character in {"\n", "\r", "\t"} else character)
    normalized = "".join(cleaned).strip()

    decoded: List[str] = []
    decoded_types: List[str] = []
    if inspect_encodings:
        url_decoded = unquote(normalized)
        if url_decoded != normalized:
            decoded.append(unicodedata.normalize("NFKC", url_decoded))
            decoded_types.append("url")
        for kind, candidate in _encoded_candidates(normalized):
            if len(decoded) >= max(0, int(max_decoded_payloads)):
                break
            text = _decode_candidate(kind, candidate)
            if text and text not in decoded:
                decoded.append(unicodedata.normalize("NFKC", text)[:max_chars])
                decoded_types.append(kind)

    inspection_text = "\n".join([normalized, *decoded])
    folded = inspection_text.casefold()
    compact = re.sub(r"[\W_]+", "", folded, flags=re.UNICODE)
    tokens = tuple(re.findall(r"[a-z0-9]+", folded))
    return NormalizedSecurityText(
        normalized=normalized,
        inspection_text=inspection_text,
        compact=compact,
        tokens=tokens,
        metadata={
            "normalization": "nfkc-control-strip-v1",
            "modified": normalized != raw,
            "removed_control_count": removed_controls,
            "decoded_payload_count": len(decoded),
            "decoded_payload_types": sorted(set(decoded_types)),
            "truncated": len(str(value or "")) > max_chars,
        },
    )


def inspect_security_text(
    value: Any,
    *,
    max_chars: int = 4000,
    inspect_encodings: bool = True,
    max_decoded_payloads: int = 4,
    fuzzy_threshold: float = 0.82,
) -> Dict[str, Any]:
    """Produce non-secret multi-label security signals for policy decisions."""

    normalized = normalize_security_text(
        value,
        max_chars=max_chars,
        inspect_encodings=inspect_encodings,
        max_decoded_payloads=max_decoded_payloads,
    )
    exact_injection = bool(_DIRECT_INJECTION.search(normalized.inspection_text))
    fuzzy_labels = _fuzzy_labels(normalized.tokens, fuzzy_threshold)
    fuzzy_injection = (
        {"ignore", "previous", "instructions"}.issubset(fuzzy_labels)
        or ({"bypass", "system"}.issubset(fuzzy_labels))
    )
    secret_target = any(term in normalized.compact for term in _SECRET_COMPACT_TERMS)
    fuzzy_secret_target = {"system", "prompt"}.issubset(fuzzy_labels)
    compact_possession = any(term in normalized.compact for term in _POSSESSION_COMPACT_TERMS)
    exfiltration_action = bool(
        _EXFILTRATION_ACTION.search(normalized.inspection_text)
    ) or compact_possession
    possession_request = bool(
        _POSSESSION_REQUEST.search(normalized.inspection_text)
    ) or compact_possession
    fuzzy_exfiltration = "reveal" in fuzzy_labels
    protective_context = bool(_PROTECTIVE_CONTEXT.search(normalized.inspection_text))
    credential_rules = [
        rule_id
        for rule_id, pattern in _CREDENTIAL_VALUE_PATTERNS
        if pattern.search(normalized.inspection_text)
    ]
    permission_violation = (
        (secret_target or fuzzy_secret_target)
        and (exfiltration_action or fuzzy_exfiltration)
        and not (protective_context and not possession_request)
    )
    injection = exact_injection or fuzzy_injection
    security_discussion = protective_context and not permission_violation

    labels: List[str] = []
    rules: List[str] = []
    if injection:
        labels.append("prompt_injection")
        rules.append("injection_exact" if exact_injection else "injection_fuzzy")
    if permission_violation:
        labels.append("sensitive_information_exfiltration")
        rules.append("secret_target_and_exfiltration_action")
    if credential_rules:
        labels.append("credential_material")
        rules.extend(credential_rules)
    if security_discussion:
        labels.append("security_education")
        rules.append("protective_context")
    if _FRESHNESS.search(normalized.inspection_text):
        labels.append("freshness")
        rules.append("freshness_terms")

    return {
        "labels": labels,
        "matched_rules": sorted(set(rules)),
        "injection": injection,
        "permission_violation": permission_violation,
        "credential_material": bool(credential_rules),
        "security_discussion": security_discussion,
        "freshness": "freshness" in labels,
        "normalization": normalized.metadata,
        "_normalized_text": normalized.normalized,
    }


def classify_user_query(query: str, config: Any) -> Dict[str, Any]:
    """Classify a user query and enforce deny-over-allow authorization policy."""

    options = _inspection_options(config)
    original = inspect_security_text(query, **options)
    normalized_text = str(original.pop("_normalized_text", ""))
    injection_match = _DIRECT_INJECTION.search(normalized_text)

    if original["injection"] and not original["security_discussion"]:
        sanitized = _extract_business_part(
            normalized_text,
            injection_match.start() if injection_match else None,
        )
        sanitized_signals: Dict[str, Any] | None = None
        if sanitized:
            sanitized_signals = inspect_security_text(sanitized, **options)
            sanitized = str(sanitized_signals.pop("_normalized_text", ""))
            # This second pass closes the combined attack where an exfiltration
            # request appears before an injection marker.
            if sanitized_signals["permission_violation"] or sanitized_signals["credential_material"]:
                return _decision(
                    config,
                    intent="permission_sensitive",
                    action="deny",
                    reason="sensitive_information_exfiltration_after_sanitization",
                    sanitized_query="",
                    original=original,
                    sanitized=sanitized_signals,
                    score=1.0,
                )
            if not sanitized_signals["injection"]:
                return _decision(
                    config,
                    intent="prompt_injection",
                    action="sanitize_and_continue",
                    reason="untrusted_instruction_removed",
                    sanitized_query=sanitized,
                    original=original,
                    sanitized=sanitized_signals,
                    score=0.98 if injection_match else 0.9,
                )

        if original["permission_violation"] or original["credential_material"]:
            reason = (
                "credential_material_in_input"
                if original["credential_material"]
                else "sensitive_information_exfiltration"
            )
            return _decision(
                config,
                intent="permission_sensitive",
                action="deny",
                reason=reason,
                sanitized_query="",
                original=original,
                sanitized=sanitized_signals,
                score=1.0,
            )
        return _decision(
            config,
            intent="prompt_injection",
            action="deny",
            reason="prompt_injection_without_safe_business_query",
            sanitized_query="",
            original=original,
            sanitized=sanitized_signals,
            score=0.95 if injection_match else 0.85,
        )

    if original["permission_violation"] or original["credential_material"]:
        reason = (
            "credential_material_in_input"
            if original["credential_material"]
            else "sensitive_information_exfiltration"
        )
        return _decision(
            config,
            intent="permission_sensitive",
            action="deny",
            reason=reason,
            sanitized_query="",
            original=original,
            score=1.0,
        )

    return _decision(
        config,
        intent="freshness" if original["freshness"] else "business_qa",
        action="allow",
        reason="time_sensitive_query" if original["freshness"] else "normal_business_query",
        sanitized_query=normalized_text,
        original=original,
        score=0.9,
    )


def inspect_untrusted_context(text: Any, config: Any) -> Dict[str, Any]:
    """Fail closed on active instructions embedded in retrieved/history content."""

    signals = inspect_security_text(text, **_inspection_options(config))
    signals.pop("_normalized_text", None)
    blocked = bool(
        (signals["injection"] and not signals["security_discussion"])
        or signals["credential_material"]
    )
    return {
        "version": "untrusted-context-guard-v1",
        "blocked": blocked,
        "labels": signals["labels"],
        "matched_rules": signals["matched_rules"],
        "normalization": signals["normalization"],
    }


def inspect_output_security(text: Any, config: Any) -> Dict[str, Any]:
    """Detect concrete credential material in a generated answer."""

    if not bool(getattr(config, "security_output_guard_enabled", True)):
        return {"version": "output-dlp-v1", "enabled": False, "blocked": False, "matched_rules": []}
    normalized = normalize_security_text(
        text,
        max_chars=int(getattr(config, "security_max_input_chars", 4000)) * 2,
        inspect_encodings=False,
    )
    rules = [
        rule_id
        for rule_id, pattern in _CREDENTIAL_VALUE_PATTERNS
        if pattern.search(normalized.inspection_text)
    ]
    return {
        "version": "output-dlp-v1",
        "enabled": True,
        "blocked": bool(rules),
        "matched_rules": sorted(set(rules)),
        "normalization": normalized.metadata,
    }


def _decision(
    config: Any,
    *,
    intent: str,
    action: str,
    reason: str,
    sanitized_query: str,
    original: Dict[str, Any],
    score: float,
    sanitized: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    original_public = {key: value for key, value in original.items() if not key.startswith("_")}
    sanitized_public = (
        {key: value for key, value in sanitized.items() if not key.startswith("_")}
        if isinstance(sanitized, dict)
        else {}
    )
    return {
        "version": str(getattr(config, "security_policy_version", "intent-policy-v2")),
        "intent": intent,
        "action": action,
        "reason": reason,
        "sanitized_query": sanitized_query,
        "injection_detected": bool(original_public.get("injection")),
        "risk_labels": list(original_public.get("labels") or []),
        "risk_score": round(float(score), 3),
        "decision_source": "deterministic_multi_signal_policy",
        "original_inspection": original_public,
        "sanitized_inspection": sanitized_public,
    }


def _inspection_options(config: Any) -> Dict[str, Any]:
    return {
        "max_chars": int(getattr(config, "security_max_input_chars", 4000)),
        "inspect_encodings": bool(getattr(config, "security_inspect_encodings", True)),
        "max_decoded_payloads": int(getattr(config, "security_max_decoded_payloads", 4)),
        "fuzzy_threshold": float(getattr(config, "security_fuzzy_threshold", 0.82)),
    }


def _extract_business_part(text: str, marker_start: int | None) -> str:
    if marker_start is not None:
        prefix = text[:marker_start].strip(" \t\r\n，,。；;：:'\"“”‘’")
        if len(prefix) >= 4:
            return prefix

    suffix_patterns = (
        r"(?:再|然后|最后)(?:回答|处理)(?:正常|合法)?问题[：:]?\s*(.+)$",
        r"(?:then|finally)\s+(?:answer|handle)\s+(?:the\s+)?(?:legitimate|normal)?\s*question\s*[:：]?\s*(.+)$",
    )
    for pattern in suffix_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            suffix = match.group(1).strip(" \t\r\n，,。；;：:'\"“”‘’")
            if suffix and not _DIRECT_INJECTION.search(suffix):
                return suffix
    return ""


def _fuzzy_labels(tokens: Iterable[str], threshold: float) -> set[str]:
    labels: set[str] = set()
    bounded_threshold = min(0.99, max(0.7, float(threshold)))
    for token in tokens:
        if len(token) < 4:
            continue
        for label, targets in _FUZZY_TARGETS.items():
            if any(_fuzzy_equal(token, target, bounded_threshold) for target in targets):
                labels.add(label)
    return labels


def _fuzzy_equal(value: str, target: str, threshold: float) -> bool:
    if value == target:
        return True
    if len(value) < 4 or value[0] != target[0] or value[-1] != target[-1]:
        return False
    return SequenceMatcher(None, value, target).ratio() >= threshold


def _encoded_candidates(text: str) -> Iterable[tuple[str, str]]:
    for candidate in re.findall(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{16,512}={0,2}", text):
        yield "base64", candidate
    for candidate in re.findall(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}){8,256}(?![0-9A-Fa-f])", text):
        yield "hex", candidate


def _decode_candidate(kind: str, candidate: str) -> str:
    try:
        if kind == "hex":
            raw = bytes.fromhex(candidate)
        else:
            padded = candidate + "=" * ((4 - len(candidate) % 4) % 4)
            raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        decoded = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return ""
    if not decoded or len(decoded) > 4000:
        return ""
    printable = sum(character.isprintable() or character.isspace() for character in decoded)
    return decoded if printable / len(decoded) >= 0.85 else ""
