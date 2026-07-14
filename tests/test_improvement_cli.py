from pathlib import Path

from scripts.run_improvement_lab import build_parser, main


def test_cli_help_exposes_offline_inputs_only() -> None:
    help_text = build_parser().format_help()

    assert "--trace-id" in help_text
    assert "--eval-report" in help_text
    assert "--proposal-mode" in help_text
    assert "--dry-run" in help_text
    assert "--run-allowlisted-evals" in help_text
    assert "--deploy" not in help_text
    assert "--apply" not in help_text


def test_cli_no_opportunity_returns_zero_and_writes_report(tmp_path: Path) -> None:
    output = tmp_path / "reports"

    exit_code = main(["--dry-run", "--output", str(output)])

    assert exit_code == 0
    reports = list(output.glob("*.md"))
    assert len(reports) == 1
    assert "No production mutation occurred" in reports[0].read_text(encoding="utf-8")


def test_cli_report_write_failure_returns_nonzero(tmp_path: Path) -> None:
    output_file = tmp_path / "not-a-directory"
    output_file.write_text("occupied", encoding="utf-8")

    exit_code = main(["--dry-run", "--output", str(output_file)])

    assert exit_code == 1
