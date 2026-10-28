"""Unit tests for Statutory Knowledge Base & RAG Corpus."""

import pytest

from hrms_plugin.rag.store import Jurisdiction, StatutoryKnowledgeBase


@pytest.fixture
def kb():
    return StatutoryKnowledgeBase()


def test_statutory_corpus_coverage(kb):
    """Verify that all target jurisdictions have statutory entries."""
    in_citations = kb.get_citations_by_jurisdiction(Jurisdiction.INDIA)
    ae_citations = kb.get_citations_by_jurisdiction(Jurisdiction.UAE)
    sa_citations = kb.get_citations_by_jurisdiction(Jurisdiction.SAUDI_ARABIA)
    us_citations = kb.get_citations_by_jurisdiction(Jurisdiction.USA)

    assert len(in_citations) >= 4  # Wages, PF, ESI, Gratuity, Maternity
    assert len(ae_citations) >= 3  # Decree-Law 33, EOSB, WPS
    assert len(sa_citations) >= 1  # Royal Decree M/51
    assert len(us_citations) >= 1  # FLSA


def test_rag_search_keyword_and_jurisdiction(kb):
    """Test hybrid search with jurisdiction filtering."""
    # Search India PF
    results = kb.search(query="provident fund statutory ceiling", jurisdiction=Jurisdiction.INDIA)
    assert len(results) >= 1
    top = results[0]
    assert "Provident Funds" in top.act_name
    assert top.jurisdiction == Jurisdiction.INDIA
    assert top.statutory_rules.get("statutory_wage_ceiling_inr") == 15000.0

    # Search UAE WPS
    ae_results = kb.search(query="wages protection system deadline", jurisdiction=Jurisdiction.UAE)
    assert len(ae_results) >= 1
    assert "Wages Protection System" in ae_results[0].title


def test_citation_formatting(kb):
    """Test standardized citation formatting output."""
    ae_citations = kb.get_citations_by_jurisdiction(Jurisdiction.UAE)
    formatted = kb.format_citation(ae_citations[0])

    assert formatted.startswith("[AE]")
    assert "Official Gazette" in formatted
