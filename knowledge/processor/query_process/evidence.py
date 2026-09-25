"""Evidence calibration, canonical grouping, and claim/citation verification."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from knowledge.processor.query_process.config import QueryConfig


_CITATION = re.compile(r"\[(\d+)]")
_NUMBER = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_/-]{1,}")
_STOP_TERMS = {
    "什么", "怎么", "如何", "多少", "是否", "可以", "能不能", "需要", "关于",
    "使用", "设备", "产品", "分别", "对应", "说明", "给出", "当前", "问题",
}
_CONSTRAINT_MARKERS = re.compile(
    r"至少|最多|不超过|不得|不能|不要|不含|必须|确保|等待|冷却|熄灭|超过|不到|低于|高于"
)
_BOUNDARY_MARKERS = re.compile(r"至少|最多|不超过|超过|不到|低于|高于|大于|小于")
_LIST_PREFIX = re.compile(r"^\s*(?:[\u2022\u25cf\u25aa\uf06e\-]+|[a-zA-Z0-9]+[.)、])\s*")
_CAPABILITY_QUESTION = re.compile(
    r"支持|具备|是否(?:可以|支持|有)|有没有.{0,12}(?:功能|接口|模式)|能否通过|可以通过"
)
_CAPABILITY_STOP_TERMS = {
    "hak", "支持", "具备", "是否", "可以", "通过", "有没有", "功能", "接口", "模式",
}


def complete_structured_constraints(
    answer: str,
    docs: Sequence[Dict[str, Any]],
    query: str,
    config: QueryConfig,
) -> Tuple[str, Dict[str, Any]]:
    """Complete omitted sibling constraints from the strongest evidence.

    This is deliberately narrow: it activates only for purpose, boundary, or
    safety questions and only extracts explicit constraint sentences from the
    top-ranked evidence. Appended text is still passed through the normal
    claim/evidence verifier, so this step cannot bypass citation integrity.
    """

    modes: List[str] = []
    query_text = str(query or "")
    if re.search(r"避免|防止|确保安全", query_text):
        modes.append("purpose")
    if re.search(r"边界|至少|最多|上限|下限|太厚|太薄", query_text):
        modes.append("boundary")
    if re.search(r"能不能|立即|安全|危险|卡纸|高温|烫", query_text):
        modes.append("safety")

    audit: Dict[str, Any] = {
        "version": "structured-constraint-completion-v1",
        "enabled": bool(config.structured_constraint_completion),
        "modes": modes,
        "evidence_index": 1 if docs else None,
        "candidate_count": 0,
        "appended_count": 0,
        "appended_claims": [],
    }
    if not config.structured_constraint_completion or not modes or not docs:
        return str(answer or ""), audit

    top_doc = docs[0]
    content = str(top_doc.get("content") or "")
    candidates = _constraint_candidates(content, modes)
    audit["candidate_count"] = len(candidates)
    if not candidates:
        return str(answer or ""), audit

    body, image_block = _split_image_block(str(answer or ""))
    coverage_text = body
    appended: List[str] = []
    limit = max(0, int(config.structured_constraint_max_claims))
    for candidate in candidates:
        if len(appended) >= limit:
            break
        if _claim_is_covered(candidate, coverage_text, config.structured_constraint_coverage):
            continue
        appended.append(candidate)
        coverage_text += "\n" + candidate

    if not appended:
        return str(answer or ""), audit

    rendered = body.rstrip()
    if rendered and not re.search(r"[。！？；]\s*$", rendered):
        rendered += "。"
    for claim in appended:
        rendered += ("\n" if rendered else "") + claim.rstrip("。！？；") + "[1]。"
    audit["appended_count"] = len(appended)
    audit["appended_claims"] = [
        {
            "claim": claim,
            "evidence_index": 1,
            "evidence_group_id": str(
                top_doc.get("evidence_group_id") or canonical_evidence_id(top_doc)
            ),
        }
        for claim in appended
    ]
    return rendered + image_block, audit


def _constraint_candidates(content: str, modes: Sequence[str]) -> List[str]:
    normalized = str(content or "").replace("\r", "\n")
    fragments = re.split(r"(?<=[。！？；])|\n+|(?=\|)|(?<=\|)", normalized)
    candidates: List[str] = []
    for fragment in fragments:
        raw_claim = _LIST_PREFIX.sub("", fragment).strip(" \t|")
        # A list heading can itself contain words such as "确保". It supplies
        # context but is not an independently actionable sibling constraint.
        if raw_claim.endswith(("：", ":")):
            continue
        claim = raw_claim.strip("：:")
        if len(claim) < 4:
            continue
        if not _CONSTRAINT_MARKERS.search(claim):
            continue
        if modes == ["boundary"] and not (_NUMBER.search(claim) and _BOUNDARY_MARKERS.search(claim)):
            continue
        # Boundary queries may also be safety/purpose queries. A numerical
        # boundary candidate remains relevant; non-numerical sibling rules are
        # admitted only by the purpose/safety modes.
        if "boundary" in modes and not ({"purpose", "safety"} & set(modes)):
            if not (_NUMBER.search(claim) and _BOUNDARY_MARKERS.search(claim)):
                continue
        claim = claim.strip()
        if claim and claim not in candidates:
            candidates.append(claim)
    return candidates


def _claim_is_covered(candidate: str, answer: str, threshold: float) -> bool:
    candidate_norm = _semantic_normalize(candidate)
    answer_norm = _semantic_normalize(answer)
    candidate_numbers = _normalized_numbers(candidate_norm)
    answer_numbers = _normalized_numbers(answer_norm)
    candidate_terms = _terms(candidate_norm)
    answer_terms = _terms(answer_norm)
    overlap_count = len(candidate_terms & answer_terms)

    if candidate_numbers:
        return candidate_numbers <= answer_numbers and overlap_count > 0
    if not candidate_terms:
        return candidate_norm in answer_norm
    marker_overlap = bool(
        set(_CONSTRAINT_MARKERS.findall(candidate_norm))
        & set(_CONSTRAINT_MARKERS.findall(answer_norm))
    )
    return (
        overlap_count / len(candidate_terms) >= max(0.0, min(1.0, float(threshold)))
        or (marker_overlap and overlap_count >= 2)
    )


def _semantic_normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    normalized = normalized.replace("平方米", "m2").replace("平方毫米", "mm2")
    return re.sub(r"\s+", "", normalized)


def canonical_evidence_id(doc: Dict[str, Any]) -> str:
    """Return a chunking-robust identity without manufacturing duplicate chunks."""

    if str(doc.get("source") or "local") == "web" or doc.get("url"):
        raw_url = str(doc.get("url") or "").strip()
        try:
            parsed = urlsplit(raw_url)
            normalized_url = urlunsplit(
                (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, "")
            )
        except ValueError:
            normalized_url = raw_url
        return "web:" + (normalized_url or _text_fingerprint(doc))

    citation = _json_object(doc.get("citation"))
    document = str(
        doc.get("document_id") or citation.get("document_id") or doc.get("file_title") or "unknown"
    ).strip()
    section = str(
        doc.get("section_id")
        or citation.get("section_id")
        or doc.get("parent_title")
        or doc.get("title")
        or ""
    ).strip()
    if section:
        return f"local:{document}:section:{section}"

    lineage = citation.get("block_lineage_ids") or doc.get("block_lineage_ids") or []
    if isinstance(lineage, list) and lineage:
        return f"local:{document}:lineage:{'|'.join(sorted(map(str, lineage)))}"
    return f"local:{document}:content:{_text_fingerprint(doc)}"


def annotate_evidence(
    doc: Dict[str, Any],
    query: str,
    query_features: Dict[str, Any],
    config: QueryConfig,
) -> Dict[str, Any]:
    result = dict(doc)
    source = str(result.get("source") or "local")
    result["source"] = source
    result["evidence_group_id"] = canonical_evidence_id(result)
    result["source_type"] = str(result.get("source_type") or _source_type(result))
    result["domain"] = str(result.get("domain") or _domain(result.get("url")))
    result["authority"] = _safe_unit(
        result.get("authority"),
        config.local_authority if source == "local" else config.web_default_authority,
    )
    result["freshness"] = _safe_unit(
        result.get("freshness"),
        0.4 if source == "local" else (1.0 if query_features.get("freshness") else 0.5),
    )
    result["structure_match"] = structure_match(result, query, query_features)
    result["evidence_coverage"] = evidence_coverage(query, [result])

    raw_score = result.get("score")
    try:
        raw_score = float(raw_score) if raw_score is not None else None
    except (TypeError, ValueError):
        raw_score = None
    result["rerank_raw_score"] = raw_score
    result["calibrated_relevance"] = calibrate_rerank_score(raw_score, config)

    # Ranking and refusal are separate: ranking uses per-document source signals;
    # refusal is calculated later from the selected evidence set and score margin.
    result["ranking_score"] = round(
        0.55 * result["calibrated_relevance"]
        + 0.20 * result["authority"]
        + 0.15 * result["structure_match"]
        + 0.10 * result["freshness"],
        6,
    )
    return result


def calibrate_rerank_score(raw_score: float | None, config: QueryConfig) -> float:
    if raw_score is None:
        return 0.5
    scale = max(abs(float(config.rerank_calibration_scale)), 1e-6)
    exponent = -(float(raw_score) - float(config.rerank_calibration_center)) / scale
    exponent = max(-60.0, min(60.0, exponent))
    return round(1.0 / (1.0 + math.exp(exponent)), 6)


def build_evidence_decision(
    docs: Sequence[Dict[str, Any]],
    graph_evidence: Sequence[Any],
    query: str,
    config: QueryConfig,
) -> Dict[str, Any]:
    if not docs and not graph_evidence:
        return {
            "version": "evidence-decision-v1",
            "should_answer": False,
            "reason": "empty_context",
            "answer_confidence": 0.0,
            "evidence_coverage": 0.0,
            "evidence_group_count": 0,
        }

    ranked = list(docs)
    top = ranked[0] if ranked else {}
    second = ranked[1] if len(ranked) > 1 else {}
    top_relevance = _safe_unit(top.get("calibrated_relevance"), 0.5 if ranked else 0.0)
    second_relevance = _safe_unit(second.get("calibrated_relevance"), 0.0)
    margin = max(0.0, top_relevance - second_relevance)
    authority = _safe_unit(top.get("authority"), 0.6 if graph_evidence else 0.0)
    structure = _safe_unit(top.get("structure_match"), 0.5 if graph_evidence else 0.0)
    coverage = evidence_coverage(query, ranked[:3]) if ranked else (0.5 if graph_evidence else 0.0)

    weights = {
        "relevance": config.confidence_relevance_weight,
        "margin": config.confidence_margin_weight,
        "authority": config.confidence_authority_weight,
        "structure": config.confidence_structure_weight,
        "coverage": config.confidence_coverage_weight,
    }
    weight_sum = sum(max(0.0, float(value)) for value in weights.values()) or 1.0
    confidence = (
        top_relevance * weights["relevance"]
        + margin * weights["margin"]
        + authority * weights["authority"]
        + structure * weights["structure"]
        + coverage * weights["coverage"]
    ) / weight_sum
    confidence = round(max(0.0, min(1.0, confidence)), 6)
    groups = {str(doc.get("evidence_group_id") or canonical_evidence_id(doc)) for doc in ranked}
    capability_terms = _capability_feature_terms(query)
    capability_coverage = _authoritative_capability_coverage(capability_terms, ranked)
    capability_guard_triggered = bool(
        config.capability_evidence_guard_enabled
        and capability_terms
        and capability_coverage < config.capability_authoritative_min_coverage
    )
    should_answer = confidence >= config.refusal_min_confidence and not capability_guard_triggered
    if capability_guard_triggered:
        reason = "unsupported_capability_evidence"
    else:
        reason = "sufficient_evidence" if should_answer else "low_evidence_confidence"
    return {
        "version": "evidence-decision-v2",
        "should_answer": should_answer,
        "reason": reason,
        "answer_confidence": confidence,
        "threshold": config.refusal_min_confidence,
        "top1_raw_score": top.get("rerank_raw_score"),
        "top1_calibrated_relevance": round(top_relevance, 6),
        "top2_calibrated_relevance": round(second_relevance, 6),
        "score_margin": round(margin, 6),
        "source_authority": round(authority, 6),
        "structure_match": round(structure, 6),
        "evidence_coverage": round(coverage, 6),
        "source_type": str(top.get("source_type") or ("knowledge_graph" if graph_evidence else "")),
        "evidence_group_count": len(groups),
        "capability_guard_enabled": bool(config.capability_evidence_guard_enabled),
        "capability_guard_triggered": capability_guard_triggered,
        "capability_term_count": len(capability_terms),
        "capability_authoritative_coverage": round(capability_coverage, 6),
        "capability_authoritative_min_coverage": round(
            float(config.capability_authoritative_min_coverage), 6
        ),
    }


def _capability_feature_terms(query: str) -> set[str]:
    """Extract the capability itself, excluding product and question boilerplate."""

    text = str(query or "")
    if not _CAPABILITY_QUESTION.search(text):
        return set()
    normalized = text
    for term in _CAPABILITY_STOP_TERMS:
        normalized = re.sub(re.escape(term), "", normalized, flags=re.IGNORECASE)
    # Product model numbers are retrieval scope, not proof of a capability.
    normalized = re.sub(r"\b[A-Za-z]{2,}[\s_-]*\d+[A-Za-z0-9_-]*\b", " ", normalized)
    normalized = re.sub(r"[吗呢么嘛？?]", " ", normalized)
    return {
        term for term in _terms(normalized)
        if term not in _CAPABILITY_STOP_TERMS and not term.isdigit()
    }


def _authoritative_capability_coverage(
    required_terms: set[str], docs: Sequence[Dict[str, Any]]
) -> float:
    if not required_terms:
        return 1.0
    evidence_terms: set[str] = set()
    for doc in docs:
        source = str(doc.get("source") or "local")
        source_type = str(doc.get("source_type") or "")
        if source == "web" and source_type != "official_web":
            continue
        evidence_terms |= _terms(
            " ".join(
                str(doc.get(field) or "")
                for field in (
                    "title", "parent_title", "file_title", "content", "snippet", "preview"
                )
            )
        )
    return len(required_terms & evidence_terms) / len(required_terms)


def verify_claim_citations(
    answer: str,
    docs: Sequence[Dict[str, Any]],
    config: QueryConfig,
) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    """Validate citations per claim, prune unsupported claims, and renumber sources."""

    text = str(answer or "")
    body, image_block = _split_image_block(text)
    requested_images = set(re.findall(r"https?://[^\s<>]+", image_block))
    body = re.sub(r"([。！？；])\s*(?=\[\d+])", "", body)
    segments = [segment for segment in re.split(r"(?<=[。！？；\n])", body) if segment]
    kept: List[str] = []
    claims: List[Dict[str, Any]] = []
    cited_order: List[int] = []

    for segment in segments:
        citations = [int(value) for value in _CITATION.findall(segment)]
        valid = [value for value in citations if 1 <= value <= len(docs)]
        claim_text = _CITATION.sub("", segment).strip()
        factual = _is_factual_claim(claim_text)
        # Treat model citations as suggestions. Verification searches the full
        # selected evidence set and deterministically binds the highest-ranked
        # supporting record, so a correct claim is not deleted merely because
        # the model omitted or mis-numbered its citation. One strongest source
        # is preferred over redundant related citations.
        supporting_candidates = [
            value
            for value in range(1, len(docs) + 1)
            if evidence_supports_claim(claim_text, docs[value - 1], config.citation_min_overlap)
            or (
                bool(re.search(r"(?:图|图片|示意|面板|位置)", claim_text))
                and bool(requested_images & _doc_image_urls(docs[value - 1]))
            )
        ]
        supported = supporting_candidates[:1]
        image_supported = [
            value
            for value in valid
            if requested_images & _doc_image_urls(docs[value - 1])
        ][:1]

        action = "kept"
        output_segment = segment
        if factual and supported:
            output_segment = _render_bound_claim(segment, supported)
            for value in supported:
                if value not in cited_order:
                    cited_order.append(value)
            if citations == supported:
                action = "kept"
            elif citations and set(supported).issubset(valid):
                action = "pruned_redundant_citations"
            elif citations:
                action = "rebound_evidence"
            else:
                action = "bound_missing_citation"
        elif factual and config.require_claim_citations and docs:
            output_segment = ""
            action = "removed_unsupported_claim"
        elif image_supported:
            supported = image_supported
            output_segment = _render_bound_claim(segment, supported)
            for value in supported:
                if value not in cited_order:
                    cited_order.append(value)
            action = "kept_image_binding"
        elif citations:
            output_segment = _CITATION.sub("", segment)
            action = "removed_invalid_citations"

        if output_segment:
            kept.append(output_segment)
        if factual or citations:
            claims.append(
                {
                    "claim": claim_text[:500],
                    "citations": citations,
                    "supported_citations": supported,
                    "evidence_groups": [
                        str(docs[value - 1].get("evidence_group_id") or canonical_evidence_id(docs[value - 1]))
                        for value in supported
                    ],
                    "action": action,
                }
            )

    if not segments and body.strip():
        kept.append(body.strip())

    supported_claims_by_index: Dict[int, List[str]] = {}
    for claim in claims:
        if claim["action"] not in {
            "kept", "pruned_redundant_citations", "rebound_evidence",
            "bound_missing_citation", "kept_image_binding",
        }:
            continue
        for index in claim["supported_citations"]:
            supported_claims_by_index.setdefault(index, []).append(claim["claim"])

    filtered_docs = []
    for index in cited_order:
        doc = dict(docs[index - 1])
        # Export only claims already accepted by the verifier. This gives the
        # public citation record a compact, auditable claim-evidence binding
        # without exposing the full prompt context or relying on a truncated
        # generic preview during offline evaluation.
        doc["supported_claims"] = list(dict.fromkeys(supported_claims_by_index.get(index, [])))
        filtered_docs.append(doc)
    remap = {old: new for new, old in enumerate(cited_order, 1)}
    verified_body = "".join(kept).strip()
    verified_body = _CITATION.sub(
        lambda match: f"[{remap[int(match.group(1))]}]" if int(match.group(1)) in remap else "",
        verified_body,
    )
    if docs and not verified_body:
        verified_body = "当前证据不足以支持可核验的具体结论。"
    supported_images = {
        image
        for doc in filtered_docs
        for image in _doc_image_urls(doc)
    }
    verified_answer = verified_body + _verified_image_block(image_block, supported_images)
    audit = {
        "version": "claim-evidence-v1",
        "claim_count": len(claims),
        "kept_claim_count": sum(
            claim["action"] in {
                "kept", "pruned_redundant_citations", "rebound_evidence",
                "bound_missing_citation", "kept_image_binding",
            }
            for claim in claims
        ),
        "removed_claim_count": sum(claim["action"].startswith("removed_") for claim in claims),
        "cited_evidence_count": len(filtered_docs),
        "claims": claims,
    }
    return verified_answer, filtered_docs, audit


def evidence_supports_claim(claim: str, doc: Dict[str, Any], min_overlap: float) -> bool:
    claim_clean = _CITATION.sub("", str(claim or ""))
    evidence = " ".join(
        (
            " ".join(str(value) for value in doc.get(field) if str(value).strip())
            if isinstance(doc.get(field), list)
            else str(doc.get(field) or "")
        )
        # Product/model identifiers are part of the evidence scope.  Without
        # item_name, a grounded claim such as "HAK 180 ... 350 g/m2" is
        # rejected because the numeric-integrity check sees model number 180
        # in the claim but not in the chunk body, even though Milvus already
        # constrained the hit to item_name="HAK 180".
        for field in (
            "item_name", "item_names", "title", "parent_title", "file_title",
            "content", "snippet", "preview", "retrieved_at", "retrieved_date",
            "publication_date", "url", "domain", "source_type",
            "supported_claims",
        )
    )
    if not evidence.strip():
        return False
    # A low lexical-overlap threshold is useful for paraphrases, but it must
    # not let a merely topical product paragraph support a price claim. Bind
    # high-risk semantic attributes to evidence containing that attribute.
    if re.search(r"售价|价格|金额|面议|报价|人民币", claim_clean) and not re.search(
        r"售价|价格|金额|面议|报价|人民币", evidence
    ):
        return False
    # Retrieval time proves when we observed a page, not when the publisher
    # authored it. A publication-date claim needs an explicit publication date.
    if re.search(r"发布(?:时间|日期)|发表于", claim_clean) and not str(
        doc.get("publication_date") or ""
    ).strip():
        return False
    claim_numbers = _normalized_numbers(claim_clean)
    evidence_numbers = _normalized_numbers(evidence)
    if claim_numbers and not claim_numbers <= evidence_numbers:
        return False
    terms = _terms(claim_clean)
    if not terms:
        return bool(claim_numbers)
    evidence_terms = _terms(evidence)
    overlap = len(terms & evidence_terms) / len(terms)
    return overlap >= min_overlap


def _normalized_numbers(text: str) -> set[str]:
    """Treat date/unit formatting zeros as presentation, not new facts."""

    values: set[str] = set()
    for raw in _NUMBER.findall(str(text or "")):
        try:
            values.add(format(Decimal(raw).normalize(), "f"))
        except InvalidOperation:
            values.add(raw)
    return values


def evidence_coverage(query: str, docs: Iterable[Dict[str, Any]]) -> float:
    query_terms = _terms(query)
    if not query_terms:
        return 0.0
    evidence_terms: set[str] = set()
    for doc in docs:
        evidence_terms |= _terms(
            " ".join(str(doc.get(field) or "") for field in ("title", "parent_title", "content", "snippet"))
        )
    return round(len(query_terms & evidence_terms) / len(query_terms), 6)


def structure_match(doc: Dict[str, Any], query: str, features: Dict[str, Any]) -> float:
    values: List[float] = []
    if features.get("table"):
        values.append(1.0 if doc.get("has_table") or doc.get("source_type") == "table" else 0.0)
    if features.get("image"):
        values.append(1.0 if doc.get("has_image") or doc.get("image_urls") else 0.0)
    if features.get("safety"):
        heading = str(doc.get("title") or "") + str(doc.get("parent_title") or "")
        values.append(1.0 if re.search(r"警告|安全|危险|注意|卡纸|冷却|锁定", heading) else 0.0)
    if values:
        return round(max(values), 6)
    return min(1.0, evidence_coverage(query, [doc]) * 1.5)


def _terms(text: str) -> set[str]:
    normalized = re.sub(r"\[[^]]+]", " ", str(text or "")).lower()
    terms = {word.lower() for word in _WORD.findall(normalized)}
    for run in _CJK_RUN.findall(normalized):
        for stop in _STOP_TERMS:
            run = run.replace(stop, "")
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[index:index + 2] for index in range(len(run) - 1))
    return {term for term in terms if term and term not in _STOP_TERMS}


def _source_type(doc: Dict[str, Any]) -> str:
    if str(doc.get("source") or "local") == "web":
        return "web"
    heading = str(doc.get("title") or "") + str(doc.get("parent_title") or "")
    if doc.get("has_table"):
        return "table"
    if doc.get("has_image"):
        return "image"
    if re.search(r"警告|安全|危险|注意", heading):
        return "safety_manual"
    if "说明书" in str(doc.get("file_title") or ""):
        return "product_manual"
    return "knowledge_base"


def _domain(value: Any) -> str:
    try:
        return (urlsplit(str(value or "")).hostname or "").lower()
    except ValueError:
        return ""


def _text_fingerprint(doc: Dict[str, Any]) -> str:
    text = " ".join(str(doc.get(field) or "") for field in ("title", "parent_title", "content"))
    normalized = " ".join(text.lower().split())[:1000]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _safe_unit(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    return max(0.0, min(1.0, number))


def _is_factual_claim(text: str) -> bool:
    normalized = str(text or "").strip(" \t\r\n-*#：:")
    if len(normalized) < 4:
        return False
    if any(marker in normalized for marker in ("没有足够依据", "证据不足", "无法提供", "请提供", "请补充")):
        return False
    return bool(re.search(r"[\u3400-\u9fffA-Za-z0-9]", normalized))


def _split_image_block(answer: str) -> Tuple[str, str]:
    if "【图片】" not in answer:
        return answer, ""
    body, images = answer.split("【图片】", 1)
    return body, "\n【图片】" + images


def _doc_image_urls(doc: Dict[str, Any]) -> set[str]:
    values = {
        str(value)
        for value in doc.get("image_urls") or []
        if isinstance(value, str)
    }
    values.update(
        re.findall(
            r"!\[[^\]]*\]\(\s*<?(https?://[^\s<>\)]+)",
            str(doc.get("content") or ""),
        )
    )
    return values


def _render_bound_claim(segment: str, citations: Sequence[int]) -> str:
    clean = _CITATION.sub("", segment).rstrip()
    suffix = "".join(f"[{value}]" for value in citations)
    match = re.search(r"([。！？；]\s*)$", clean)
    if match:
        return clean[: match.start()] + suffix + match.group(1)
    return clean + suffix


def _verified_image_block(image_block: str, supported_images: set[str]) -> str:
    """Keep image links only when one of the claim-bound evidence records owns them."""

    if not image_block or not supported_images:
        return ""
    filtered = image_block
    for url in set(re.findall(r"https?://[^\s<>\)]+", image_block)) - supported_images:
        filtered = filtered.replace(url, "")
    if not any(url in filtered for url in supported_images):
        return ""
    lines = [line.rstrip() for line in filtered.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)
