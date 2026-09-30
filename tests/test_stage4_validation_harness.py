"""Offline tests for the E2E-S4.0A validation harness (tools/stage4_validation)."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
from pathlib import Path

import pytest

from tools import stage4_validation as harness


def test_windows_absolute_detection():
    assert harness.is_windows_absolute(r"C:\Users\x\repo\data\a.json")
    assert harness.is_windows_absolute("d:/repo/a.json")
    assert harness.is_windows_absolute(r"\\server\share\a.json")
    assert not harness.is_windows_absolute(r"data\state\portfolio_cio.db")
    assert not harness.is_windows_absolute("data/state/portfolio_cio.db")


def test_locate_source_rebases_foreign_absolute_path_onto_repository(tmp_path):
    repo = tmp_path / "agentic_portfolio_stage4_0"
    persisted = (
        r"C:\Users\someone\OneDrive\Investimenti\agentic_portfolio_stage4_0"
        r"\data\cache\scanner\report.json"
    )
    located = harness.locate_source(persisted, repo)
    if os.name == "nt":
        assert located == Path(persisted)
    else:
        assert located == repo / "data" / "cache" / "scanner" / "report.json"


def test_locate_source_normalizes_relative_windows_separators(tmp_path):
    located = harness.locate_source(r"data\state\portfolio_cio.db", tmp_path)
    assert located == tmp_path / "data" / "state" / "portfolio_cio.db"


@pytest.mark.skipif(os.name == "nt", reason="foreign absolute paths are native on Windows")
def test_locate_source_refuses_unrelated_foreign_absolute_path(tmp_path):
    with pytest.raises(harness.HarnessError) as error:
        harness.locate_source(r"C:\elsewhere\report.json", tmp_path / "repo")
    assert error.value.code == "FOREIGN_ABSOLUTE_PATH"


def test_sandbox_target_keeps_persisted_string_verbatim(tmp_path):
    target = harness.sandbox_target(tmp_path, r"data\state\portfolio_cio.db")
    assert target == Path(os.path.join(str(tmp_path), r"data\state\portfolio_cio.db"))
    absolute = str(tmp_path / "x.json")
    assert harness.sandbox_target(tmp_path, absolute) is None


def test_block_network_refuses_and_restores():
    original = socket.socket.connect
    with harness.block_network() as attempts:
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", 9))
        with socket.socket() as raw, pytest.raises(ConnectionRefusedError):
            raw.connect(("127.0.0.1", 9))
    assert len(attempts) == 2
    assert socket.socket.connect is original


def test_sqlite_copy_is_consistent_and_source_is_untouched(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("create table ai_inferences (id integer)")
        connection.execute("insert into ai_inferences values (1), (2)")
    digest = harness.sha256_file(source)
    target = tmp_path / "copy" / "state.db"
    harness.sqlite_consistent_copy(source, target)
    counts = harness.table_counts(target)
    assert counts["ai_inferences"] == 2
    assert counts["trade_opportunities"] is None
    assert harness.sha256_file(source) == digest


def _report(**configuration):
    base = {
        "database_path": r"data\state\portfolio_cio.db",
        "eligibility_report_path": "eligibility.json",
        "mapping_report_path": "mapping.json",
        "history_report_paths": ["history.json"],
        "selected_parameters": None,
    }
    base.update(configuration)
    return {
        "configuration": base,
        "request": {"portfolio_file": "portfolio.xlsx", "portfolio_file_fingerprint": "x"},
        "run": {"run_id": "s4a-test", "watch_universe_report_path": None},
    }


def test_materialize_refuses_absolute_database_path(tmp_path):
    report = _report(database_path=r"C:\prod\portfolio_cio.db")
    with pytest.raises(harness.HarnessError) as error:
        harness.materialize_sandbox(report, tmp_path / "sandbox", tmp_path)
    assert error.value.code == "ABSOLUTE_DB_PATH_REFUSED"


def test_materialize_refuses_operator_overlay(tmp_path):
    report = _report(selected_parameters={"requested_exposure_eur": 1000})
    with pytest.raises(harness.HarnessError) as error:
        harness.materialize_sandbox(report, tmp_path / "sandbox", tmp_path)
    assert error.value.code == "SELECTED_PARAMETERS_NOT_SUPPORTED"


def test_materialize_copies_inputs_and_database(tmp_path):
    repo = tmp_path / "repo"
    (repo / "data" / "state").mkdir(parents=True)
    with sqlite3.connect(repo / "data" / "state" / "portfolio_cio.db") as connection:
        connection.execute("create table ai_inferences (id integer)")
    for name in ("eligibility.json", "mapping.json", "history.json"):
        (repo / name).write_text("{}", encoding="utf-8")
    (repo / "portfolio.xlsx").write_bytes(b"portfolio")
    report = _report()
    report["request"]["portfolio_file_fingerprint"] = harness.sha256_file(repo / "portfolio.xlsx")

    sandbox = harness.materialize_sandbox(report, tmp_path / "sandbox", repo)

    assert sandbox["portfolio_fingerprint_matches"] is True
    assert Path(sandbox["sandbox_database"]).is_file()
    assert {item["mode"] for item in sandbox["files"]} == {"COPIED"}


def test_main_reports_missing_root_report_as_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(harness, "REPO_ROOT", tmp_path)

    def missing(run_id, repo_root=tmp_path):
        raise harness.HarnessError("ROOT_REPORT_NOT_FOUND", "none")

    monkeypatch.setattr(harness, "find_root_report", missing)
    out = tmp_path / "replay.json"
    code = harness.main(["replay", "--workdir", str(tmp_path / "w"), "--out", str(out)])
    assert code == 1
    assert json.loads(out.read_text(encoding="utf-8"))["error"] == "ROOT_REPORT_NOT_FOUND"
    capsys.readouterr()


def _inspection_db(path, *, adapter_version, volatility_text, research_unknowns):
    with sqlite3.connect(path) as connection:
        for table, column in (
            ("scanner_evidence_bundles", "created_at"),
            ("opportunity_research", "created_at"),
            ("opportunity_scores", "created_at"),
            ("scanner_research_outcomes", "updated_at"),
        ):
            connection.execute(f"create table {table} ({column} text, payload_json text)")
        technical_text = "RSI14: 50.0000." + (
            " 20-session annualized volatility: 23.4000%." if volatility_text else ""
        )
        bundle = {
            "bundle_id": "evidence-1",
            "ticker": "UCG.MI",
            "items": [{
                "kind": "TECHNICAL",
                "evidence": {"text": technical_text},
                "metadata": {"adapter_version": adapter_version},
                "source": {"metadata": {"canonical_technical_contract": "ai-8c3-canonical-technical-v2"}},
            }],
        }
        research = {
            "research_id": "RES-1",
            "ticker": "UCG.MI",
            "research_status": "PARTIAL",
            "evidence_quality": "MEDIUM",
            "unknowns": research_unknowns,
        }
        outcome = {
            "hypothesis_id": "hyp-1",
            "status": "EXCLUDED",
            "reason": "RESEARCH_NOT_COMPLETE",
            "research_id": "RES-1",
            "evidence_bundle_id": "evidence-1",
        }
        stamp = "2026-09-30T12:30:00Z"
        connection.execute("insert into scanner_evidence_bundles values (?, ?)", (stamp, json.dumps(bundle)))
        connection.execute("insert into opportunity_research values (?, ?)", (stamp, json.dumps(research)))
        connection.execute("insert into scanner_research_outcomes values (?, ?)", (stamp, json.dumps(outcome)))


def test_inspection_accepts_r1_when_volatility_is_stated_and_not_unknown(tmp_path):
    database = tmp_path / "state.db"
    _inspection_db(
        database,
        adapter_version="stage4-research-technical-v2",
        volatility_text=True,
        research_unknowns=["Sector peer comparison is unavailable.", "Management tone."],
    )
    result = harness.inspect_research(database, "2026-09-30T12:00:00Z")
    assert result["r1_acceptance"] == "PASS"
    row = result["research"][0]
    assert row["bundle_states_volatility"] is True
    assert row["volatility_listed_unknown"] is False
    assert row["material_unknowns"] == ["Sector peer comparison is unavailable."]
    assert result["outcome_reason_counts"] == {"EXCLUDED/RESEARCH_NOT_COMPLETE": 1}


def test_inspection_rejects_r1_when_volatility_unknown_despite_evidence(tmp_path):
    database = tmp_path / "state.db"
    _inspection_db(
        database,
        adapter_version="stage4-research-technical-v2",
        volatility_text=True,
        research_unknowns=["Technical volatility metrics are unknown."],
    )
    result = harness.inspect_research(database, "2026-09-30T12:00:00Z")
    assert result["r1_acceptance"] == "FAIL"


def test_inspection_rejects_r1_for_v1_evidence_and_ignores_older_rows(tmp_path):
    database = tmp_path / "state.db"
    _inspection_db(
        database,
        adapter_version="stage4-research-technical-v1",
        volatility_text=False,
        research_unknowns=[],
    )
    assert harness.inspect_research(database, "2026-09-30T12:00:00Z")["r1_acceptance"] == "FAIL"
    later = harness.inspect_research(database, "2026-09-30T13:00:00Z")
    assert later["technical_evidence"] == [] and later["research"] == []
