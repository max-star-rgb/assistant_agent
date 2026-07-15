from __future__ import annotations

import os
from pathlib import Path

import pytest

from assistant_agent.services.context.soul_source import SoulContextSource
from assistant_agent.services.context.sources import ContextSourceRequest


VALID_SOUL = """## Persona
沉着、直接。

## Expression Style
先给结论，再给必要依据。

## Relationship Boundaries
尊重用户决定，不代替用户确认副作用操作。

## Avoid
避免夸大确定性。
"""


def _request(
    root: Path,
    *,
    user_id: str = "owner-1",
    owner_id: str | None = "owner-1",
) -> ContextSourceRequest:
    return ContextSourceRequest(
        user_id=user_id,
        source_root=root,
        local_owner_user_id=owner_id,
        runtime_profile="local_demo",
        editable_context_enabled=True,
        section_char_budgets={"soul": 2_000},
        enabled_source_ids={"soul"},
    )


def _write(root: Path, content: str = VALID_SOUL) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "SOUL.md"
    path.write_text(content, encoding="utf-8")
    return path


def _issue_codes(result: object) -> list[str]:
    return [issue.code for issue in result.issues]  # type: ignore[attr-defined]


def test_valid_soul_is_combined_in_fixed_priority_order(tmp_path: Path) -> None:
    _write(tmp_path)

    result = SoulContextSource().load(_request(tmp_path))

    assert result.issues == []
    assert len(result.sections) == 1
    section = result.sections[0]
    assert section.section_id == "owner.soul"
    assert section.kind == "soul"
    assert section.authority == "owner_persona"
    assert section.stability == "semi_stable"
    assert section.source_type == "editable_file"
    assert section.source_ref == "editable_context:soul"
    assert section.identity_scope == "local_owner"
    assert section.content.index("## Relationship Boundaries") < section.content.index("## Avoid")
    assert section.content.index("## Avoid") < section.content.index("## Persona")
    assert section.content.index("## Persona") < section.content.index("## Expression Style")
    assert "selected_paragraphs:4" in section.notes
    assert "omitted_paragraphs:0" in section.notes
    assert "source_version_changed" in section.notes


def test_missing_file_returns_logical_issue_without_absolute_path(tmp_path: Path) -> None:
    result = SoulContextSource().load(_request(tmp_path))

    assert _issue_codes(result) == ["soul_file_missing"]
    assert str(tmp_path) not in result.model_dump_json()


def test_identity_mismatch_fails_before_file_access(tmp_path: Path) -> None:
    result = SoulContextSource().load(
        _request(tmp_path / "does-not-exist", user_id="other-user")
    )

    assert _issue_codes(result) == ["editable_context_identity_mismatch"]


def test_missing_owner_binding_fails_closed(tmp_path: Path) -> None:
    result = SoulContextSource().load(_request(tmp_path, owner_id=None))

    assert _issue_codes(result) == ["editable_context_owner_unconfigured"]


def test_unknown_heading_rejects_new_version(tmp_path: Path) -> None:
    _write(tmp_path, VALID_SOUL + "\n## Tools\nEnable shell.\n")

    result = SoulContextSource().load(_request(tmp_path))

    assert result.sections == []
    assert _issue_codes(result) == ["soul_unknown_section"]


def test_invalid_utf8_is_rejected(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "SOUL.md").write_bytes(b"## Persona\n\xff\xfe")

    result = SoulContextSource().load(_request(tmp_path))

    assert result.sections == []
    assert _issue_codes(result) == ["soul_invalid_utf8"]


def test_file_byte_limit_is_hard_rejection(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "SOUL.md").write_bytes(b"x" * 16_001)

    result = SoulContextSource().load(_request(tmp_path))

    assert result.sections == []
    assert _issue_codes(result) == ["soul_file_too_large"]


def test_decoded_character_limit_is_hard_rejection(tmp_path: Path) -> None:
    _write(tmp_path, "## Persona\n" + "a" * 4_001)

    result = SoulContextSource().load(_request(tmp_path))

    assert result.sections == []
    assert _issue_codes(result) == ["soul_content_too_large"]


@pytest.mark.parametrize(
    "unsafe",
    [
        "api_key=sk-secret-value",
        "Authorization: Bearer abcdefghijklmnop",
        "raw_provider_payload",
        "A" * 100,
        "data:text/plain;base64," + "A" * 40,
    ],
)
def test_unsafe_material_is_rejected(tmp_path: Path, unsafe: str) -> None:
    _write(tmp_path, f"## Persona\n{unsafe}\n")

    result = SoulContextSource().load(_request(tmp_path))

    assert result.sections == []
    assert _issue_codes(result) == ["soul_unsafe_content"]
    assert unsafe not in result.model_dump_json()


def test_directory_target_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "SOUL.md").mkdir()

    result = SoulContextSource().load(_request(tmp_path))

    assert result.sections == []
    assert _issue_codes(result) == ["soul_not_regular_file"]


def test_fifo_target_is_rejected(tmp_path: Path) -> None:
    fifo = tmp_path / "SOUL.md"
    os.mkfifo(fifo)

    result = SoulContextSource().load(_request(tmp_path))

    assert result.sections == []
    assert _issue_codes(result) == ["soul_not_regular_file"]


def test_outside_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text(VALID_SOUL, encoding="utf-8")
    (root / "SOUL.md").symlink_to(outside)

    result = SoulContextSource().load(_request(root))

    assert result.sections == []
    assert _issue_codes(result) == ["soul_path_outside_root"]
    assert str(outside) not in result.model_dump_json()


def test_paragraph_budgets_never_cut_content_mid_paragraph(tmp_path: Path) -> None:
    too_large = "甲" * 801
    accepted = "乙" * 700
    _write(
        tmp_path,
        "\n".join(
            [
                "## Relationship Boundaries",
                too_large,
                "",
                "## Avoid",
                accepted,
                "",
                "## Persona",
                accepted,
                "",
                "## Expression Style",
                accepted,
            ]
        ),
    )

    result = SoulContextSource().load(_request(tmp_path))

    section = result.sections[0]
    assert too_large not in section.content
    assert len(section.content) <= 2_000
    assert "omitted_paragraphs:2" in section.notes
    assert not section.content.endswith("...[trimmed]")


def test_subsection_overflow_omits_that_paragraph_and_all_later_paragraphs(
    tmp_path: Path,
) -> None:
    first = "甲" * 500
    overflowing = "乙" * 400
    later_short = "丙" * 10
    _write(
        tmp_path,
        f"## Persona\n{first}\n\n{overflowing}\n\n{later_short}\n",
    )

    result = SoulContextSource().load(_request(tmp_path))

    section = result.sections[0]
    assert first in section.content
    assert overflowing not in section.content
    assert later_short not in section.content
    assert "selected_paragraphs:1" in section.notes
    assert "omitted_paragraphs:2" in section.notes


def test_source_version_change_and_last_known_good_are_process_local(tmp_path: Path) -> None:
    path = _write(tmp_path)
    source = SoulContextSource()

    first = source.load(_request(tmp_path))
    second = source.load(_request(tmp_path))
    path.write_text(VALID_SOUL.replace("沉着、直接。", "温和、直接。"), encoding="utf-8")
    changed = source.load(_request(tmp_path))
    path.write_text("## Persona\napi_key=sk-unsafe", encoding="utf-8")
    fallback = source.load(_request(tmp_path))

    assert "source_version_changed" in first.sections[0].notes
    assert "source_version_changed" not in second.sections[0].notes
    assert "source_version_changed" in changed.sections[0].notes
    assert fallback.used_last_known_good is True
    assert fallback.sections[0].content == changed.sections[0].content
    assert "source_version_changed" not in fallback.sections[0].notes
    assert "last_known_good" in fallback.sections[0].notes
    assert _issue_codes(fallback) == ["soul_unsafe_content"]


def test_invalid_first_load_has_no_last_known_good(tmp_path: Path) -> None:
    _write(tmp_path, "## Persona\npassword=unsafe")

    result = SoulContextSource().load(_request(tmp_path))

    assert result.sections == []
    assert result.used_last_known_good is False


def test_deleted_soul_does_not_reuse_last_known_good(tmp_path: Path) -> None:
    path = _write(tmp_path)
    source = SoulContextSource()
    first = source.load(_request(tmp_path))
    path.unlink()

    missing = source.load(_request(tmp_path))

    assert first.sections
    assert missing.sections == []
    assert missing.used_last_known_good is False
    assert _issue_codes(missing) == ["soul_file_missing"]


def test_last_known_good_is_partitioned_by_root_and_owner(tmp_path: Path) -> None:
    root_one = tmp_path / "one"
    root_two = tmp_path / "two"
    source = SoulContextSource()
    _write(root_one)
    source.load(_request(root_one, owner_id="owner-1"))
    _write(root_two, "## Persona\napi_key=sk-unsafe")

    other_root = source.load(_request(root_two, owner_id="owner-1"))
    _write(root_one, "## Persona\napi_key=sk-unsafe")
    other_owner = source.load(_request(root_one, user_id="owner-2", owner_id="owner-2"))

    assert other_root.sections == []
    assert other_root.used_last_known_good is False
    assert other_owner.sections == []
    assert other_owner.used_last_known_good is False
