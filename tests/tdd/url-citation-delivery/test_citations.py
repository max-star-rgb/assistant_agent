from __future__ import annotations

from assistant_agent.runtime.chat_adapter import ProviderSearchSource
from assistant_agent.runtime.citations import build_url_citation_annotations


def test_build_annotations_maps_each_cited_occurrence_without_rewriting_text() -> None:
    text = "杭州 😀 [5]，苏州 [ref_2]，再次杭州 [5]。"
    sources = [
        ProviderSearchSource(index=2, title="苏州来源", url="https://example.com/2"),
        ProviderSearchSource(index=5, title="杭州来源", url="https://example.com/5"),
        ProviderSearchSource(index=7, title="未引用来源", url="https://example.com/7"),
    ]

    annotations = build_url_citation_annotations(text, sources)

    assert [text[item.start_index:item.end_index] for item in annotations] == [
        "[5]",
        "[ref_2]",
        "[5]",
    ]
    assert [item.source_id for item in annotations] == [
        "source_5",
        "source_2",
        "source_5",
    ]
    assert [item.title for item in annotations] == ["杭州来源", "苏州来源", "杭州来源"]
    assert [item.url for item in annotations] == [
        "https://example.com/5",
        "https://example.com/2",
        "https://example.com/5",
    ]


def test_build_annotations_ignores_unsafe_or_already_linked_markers() -> None:
    text = "缺失 [9]，已有链接 [1](https://linked.example)，双括号 [[2]]，零 [0]，不安全 [3]。"
    sources = [
        ProviderSearchSource(index=1, title="已有链接", url="https://example.com/1"),
        ProviderSearchSource(index=2, title="双括号", url="https://example.com/2"),
        ProviderSearchSource(index=3, title="不安全", url="file:///etc/passwd"),
        ProviderSearchSource(index=4, title="未引用", url="https://example.com/4"),
    ]

    assert build_url_citation_annotations(text, sources) == []
