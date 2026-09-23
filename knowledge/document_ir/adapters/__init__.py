"""Parser adapters that project source-specific output into DocumentIR."""

from knowledge.document_ir.adapters.markdown import MarkdownAdapter
from knowledge.document_ir.adapters.mineru import MinerUAdapter

__all__ = ["MarkdownAdapter", "MinerUAdapter"]
