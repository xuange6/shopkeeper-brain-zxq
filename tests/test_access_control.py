from __future__ import annotations

import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from knowledge.app import create_app
from knowledge.processor.query_process.nodes.search_embedding import SearchEmbeddingNode
from knowledge.processor.query_process.nodes.search_embedding_hyde import (
    SearchEmbeddingHydeNode,
)
from knowledge.processor.query_process.nodes.query_kg import (
    KG_CHUNK_OUTPUT_FIELDS,
    _ChunkBackfiller,
    _CYPHER_LOOKUP_CHUNK,
    _CYPHER_ONE_HOP_RELATIONS,
)
from knowledge.schema.query_schema import QueryRequest
from knowledge.processor.import_process.config import ImportConfig
from knowledge.processor.import_process.exceptions import LLMError
from knowledge.processor.import_process.nodes.kg_graph_node import (
    CYPHER_CHUNK_SCOPE_CONSTRAINT,
    CYPHER_ENTITY_SCOPE_CONSTRAINT,
    KnowledgeGraphNode,
    ProcessingStats,
)
from knowledge.security.access_control import (
    AccessContext,
    AccessContextError,
    build_milvus_access_filter,
    normalize_access_metadata,
    scope_session_id,
    sign_access_context_token,
    verify_access_context_token,
)


class AccessControlTests(unittest.TestCase):
    SECRET = "stage2-test-secret-that-is-at-least-32-bytes"

    def test_query_body_cannot_self_assert_tenant_or_roles(self) -> None:
        with self.assertRaises(ValidationError):
            QueryRequest.model_validate(
                {
                    "query": "HAK 180 如何使用？",
                    "tenant_id": "victim-tenant",
                    "roles": ["admin"],
                }
            )

    def test_signed_short_lived_access_context_round_trip(self) -> None:
        now = int(time.time())
        token = sign_access_context_token(
            {
                "sub": "user-17",
                "tenant": "tenant-a",
                "roles": ["support"],
                "groups": ["east"],
                "exp": now + 300,
            },
            self.SECRET,
        )
        context = verify_access_context_token(token, self.SECRET, now=now)
        self.assertEqual(context.subject_id, "user-17")
        self.assertEqual(context.tenant_id, "tenant-a")
        self.assertTrue(context.authenticated)
        self.assertEqual(
            context.reader_tokens(),
            ("subject:user-17", "role:support", "group:east"),
        )

    def test_tampered_and_expired_access_tokens_fail_closed(self) -> None:
        now = int(time.time())
        valid = sign_access_context_token(
            {"sub": "user-17", "tenant": "tenant-a", "exp": now + 30},
            self.SECRET,
        )
        tampered = valid[:-1] + ("A" if valid[-1] != "A" else "B")
        with self.assertRaises(AccessContextError):
            verify_access_context_token(tampered, self.SECRET, now=now)

        payload_part, signature_part = valid.split(".", 1)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        last_index = alphabet.index(signature_part[-1])
        alias_index = (last_index & ~3) | ((last_index + 1) & 3)
        noncanonical_alias = (
            payload_part + "." + signature_part[:-1] + alphabet[alias_index]
        )
        with self.assertRaisesRegex(AccessContextError, "malformed"):
            verify_access_context_token(noncanonical_alias, self.SECRET, now=now)

        expired = sign_access_context_token(
            {"sub": "user-17", "tenant": "tenant-a", "exp": now - 1},
            self.SECRET,
        )
        with self.assertRaisesRegex(AccessContextError, "expired"):
            verify_access_context_token(expired, self.SECRET, now=now)

    def test_anonymous_filter_is_same_tenant_public_only(self) -> None:
        expression = build_milvus_access_filter(AccessContext.public("tenant-a"))
        self.assertIn('tenant_id == "tenant-a"', expression)
        self.assertIn('visibility == "public"', expression)
        self.assertNotIn('visibility == "tenant"', expression)
        self.assertNotIn("array_contains_any", expression)

    def test_authenticated_filter_contains_subject_role_and_group_acl(self) -> None:
        principal = AccessContext(
            subject_id="user-17",
            tenant_id="tenant-a",
            roles=("support",),
            groups=("east",),
            authenticated=True,
        )
        expression = build_milvus_access_filter(principal)
        self.assertIn('tenant_id == "tenant-a"', expression)
        self.assertIn('visibility == "tenant"', expression)
        self.assertIn('visibility == "private"', expression)
        self.assertIn('"subject:user-17"', expression)
        self.assertIn('"role:support"', expression)
        self.assertIn('"group:east"', expression)

    def test_direct_and_hyde_apply_item_and_acl_before_search(self) -> None:
        principal = AccessContext(
            subject_id="user-17",
            tenant_id="tenant-a",
            roles=("support",),
            authenticated=True,
        ).to_state()
        for builder in (
            SearchEmbeddingNode._build_filter_expr,
            SearchEmbeddingHydeNode._build_filter_expr,
        ):
            with self.subTest(builder=builder.__qualname__):
                expression = builder(["HAK 180"], principal) or ""
                self.assertIn('item_name in ["HAK 180"]', expression)
                self.assertIn('tenant_id == "tenant-a"', expression)
                self.assertIn("array_contains_any", expression)

    def test_kg_queries_filter_both_ends_and_backfilled_chunks(self) -> None:
        for query in (_CYPHER_ONE_HOP_RELATIONS, _CYPHER_LOOKUP_CHUNK):
            with self.subTest(query=query[:40]):
                self.assertIn("tenant_id = $tenant_id", query)
                self.assertIn("graph_version = $graph_version", query)
                self.assertIn("visibility = 'private'", query)
                self.assertIn("$access_tokens", query)
        self.assertIn("seed.tenant_id", _CYPHER_ONE_HOP_RELATIONS)
        self.assertIn("nbr.tenant_id", _CYPHER_ONE_HOP_RELATIONS)
        self.assertIn("e.tenant_id", _CYPHER_LOOKUP_CHUNK)
        self.assertIn("c.tenant_id", _CYPHER_LOOKUP_CHUNK)

    def test_kg_backfill_requests_complete_citation_lineage(self) -> None:
        required = {
            "chunk_id",
            "document_id",
            "revision_id",
            "source_uri",
            "section_id",
            "block_ids",
            "page_numbers",
            "page_uids",
            "block_lineage_ids",
            "citation",
        }
        self.assertTrue(required.issubset(set(KG_CHUNK_OUTPUT_FIELDS)))

    def test_kg_backfill_marks_hydrated_rows_as_graph_evidence(self) -> None:
        row = {
            "chunk_id": "chk-1",
            "document_id": "doc-1",
            "section_id": "sec-1",
            "content": "evidence",
        }
        client = Mock()
        client.describe_collection.return_value = {
            "fields": [{"name": name} for name in KG_CHUNK_OUTPUT_FIELDS]
        }
        client.query.return_value = [row]
        with patch(
            "knowledge.utils.milvus_utils.get_milvus_client",
            return_value=client,
        ):
            result = _ChunkBackfiller(
                "stage2-chunks",
                AccessContext.public("public"),
            ).backfill(
                [{"entity": {"chunk_id": "chk-1"}, "distance": 2.0}]
            )

        entity = result[0]["entity"]
        self.assertEqual(entity["document_id"], "doc-1")
        self.assertEqual(entity["section_id"], "sec-1")
        self.assertEqual(entity["source"], "local")
        self.assertEqual(entity["source_type"], "knowledge_graph")

    def test_private_metadata_requires_explicit_reader(self) -> None:
        with self.assertRaisesRegex(AccessContextError, "at least one ACL reader"):
            normalize_access_metadata(
                tenant_id="tenant-a", visibility="private", acl_readers=[]
            )
        metadata = normalize_access_metadata(
            tenant_id="tenant-a",
            visibility="private",
            acl_readers=["role:support", "role:support"],
        )
        self.assertEqual(metadata["acl_readers"], ["role:support"])

    def test_audit_summary_never_exposes_raw_subject(self) -> None:
        summary = AccessContext(
            subject_id="sensitive-user-id",
            tenant_id="tenant-a",
            authenticated=True,
        ).audit_summary()
        self.assertNotIn("sensitive-user-id", str(summary))
        self.assertEqual(len(summary["subject_hash"]), 12)

    def test_chat_history_storage_key_is_bound_to_tenant_and_subject(self) -> None:
        first = scope_session_id(
            "browser-session",
            AccessContext(subject_id="u1", tenant_id="tenant-a", authenticated=True),
        )
        other_user = scope_session_id(
            "browser-session",
            AccessContext(subject_id="u2", tenant_id="tenant-a", authenticated=True),
        )
        other_tenant = scope_session_id(
            "browser-session",
            AccessContext(subject_id="u1", tenant_id="tenant-b", authenticated=True),
        )
        self.assertNotEqual(first, other_user)
        self.assertNotEqual(first, other_tenant)
        self.assertNotIn("browser-session", first)
        self.assertNotIn("tenant-a", first)
        self.assertNotIn("u1", first)

    def test_api_rejects_untrusted_auth_header_before_query_execution(self) -> None:
        client = TestClient(create_app())
        response = client.post(
            "/query",
            headers={"Authorization": "Bearer caller-controlled"},
            json={"query": "HAK 180 如何使用？", "is_stream": False},
        )
        self.assertEqual(response.status_code, 401)

    def test_api_rejects_identity_claims_in_request_body(self) -> None:
        client = TestClient(create_app())
        response = client.post(
            "/query",
            json={
                "query": "HAK 180 如何使用？",
                "tenant_id": "victim-tenant",
                "roles": ["admin"],
                "is_stream": False,
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_kg_import_fails_closed_and_records_partial_stats(self) -> None:
        config = ImportConfig(
            entity_name_collection="shadow-entities",
            kg_graph_version="shadow-graph-v1",
            kg_fail_on_partial=True,
            kg_require_nonempty=True,
        )
        node = KnowledgeGraphNode(config=config)
        state = {
            "item_name": "HAK 180",
            "chunks": [
                {"chunk_id": "chunk-1", "item_name": "HAK 180", "content": "步骤"}
            ],
            "graph_version": "shadow-graph-v1",
        }

        def mark_partial(stats: ProcessingStats, *_args) -> None:
            stats.processed_chunks = 0
            stats.failed_chunks = 1
            stats.errors.append("chunk-1 failed")

        with (
            patch(
                "knowledge.processor.import_process.nodes.kg_graph_node.get_milvus_client",
                return_value=object(),
            ),
            patch(
                "knowledge.processor.import_process.nodes.kg_graph_node.get_neo4j_driver",
                return_value=object(),
            ),
            patch.object(node, "_clear_existing_data"),
            patch.object(node, "_process_chunks_concurrently", side_effect=mark_partial),
            self.assertRaisesRegex(LLMError, "incomplete"),
        ):
            node.process(state)

        self.assertEqual(state["kg_import_stats"]["failed_chunks"], 1)
        self.assertEqual(state["graph_version"], "shadow-graph-v1")

    def test_kg_import_rejects_mixed_product_scope(self) -> None:
        node = KnowledgeGraphNode(
            config=ImportConfig(entity_name_collection="shadow-entities")
        )
        with self.assertRaisesRegex(ValueError, "exactly one item_name"):
            node._validate_get_inputs(
                {
                    "chunks": [
                        {"chunk_id": "1", "item_name": "A", "content": "a"},
                        {"chunk_id": "2", "item_name": "B", "content": "b"},
                    ]
                }
            )

    def test_invalid_kg_json_is_not_silently_counted_as_success(self) -> None:
        node = KnowledgeGraphNode(
            config=ImportConfig(entity_name_collection="shadow-entities")
        )
        with self.assertRaisesRegex(LLMError, "invalid JSON"):
            node._parse_and_clean("not-json")

    def test_kg_schema_has_scoped_uniqueness_constraints(self) -> None:
        self.assertIn("n.name, n.item_name, n.tenant_id, n.graph_version", CYPHER_ENTITY_SCOPE_CONSTRAINT)
        self.assertIn("n.id, n.item_name, n.tenant_id, n.graph_version", CYPHER_CHUNK_SCOPE_CONSTRAINT)


if __name__ == "__main__":
    unittest.main()
