import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_contracts import decision_data
from typer.testing import CliRunner

from toolswap.cli import app
from toolswap.config import InputError, resolve_config, scoped_path
from toolswap.determinism import canonical_json
from toolswap.trust import MAX_PROPOSAL_BYTES, ProposalError, parse_proposal

EXAMPLE = "schema_version: 1.0.0\nscenario: {id: service_ops.case_read, version: 1.0.0}\n"
ALLOWED = {"work_orders.get_by_idempotency_key"}


def test_config_defaults_and_equivalent_yaml_have_identical_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "original.yaml"
    explicit = tmp_path / "explicit.yaml"
    original.write_text(EXAMPLE)
    explicit.write_text(
        "seed: 0\nartifacts_dir: ./artifacts//\npolicy: stub\ndomain: service_ops\n" + EXAMPLE
    )
    monkeypatch.setenv("TOOLSWAP_SEED", "9")
    monkeypatch.setenv("TOOLSWAP_POLICY", "untrusted")
    left, right = [resolve_config(path, tmp_path) for path in (original, explicit)]
    assert canonical_json(left.model_dump(mode="json")) == canonical_json(
        right.model_dump(mode="json")
    )
    assert (left.seed, left.policy, left.artifacts_dir) == (0, "stub", "artifacts")


@pytest.mark.parametrize(
    "contents, error_type, message",
    [
        (EXAMPLE.replace("1.0.0", "2.0.0", 1), InputError, "unsupported schema_version"),
        (EXAMPLE + "seed: 0\nseed: 1\n", InputError, "duplicate YAML mapping key"),
        (EXAMPLE + "seed: &s 3\nother: *s\n", InputError, "YAML aliases"),
        (EXAMPLE + "8: bad\n", InputError, "mapping keys must be strings"),
        (EXAMPLE + "seed: '3'\n", ValidationError, "seed"),
        (EXAMPLE + "mystery: 1\n", ValidationError, "mystery"),
        (EXAMPLE + "seed: .inf\n", InputError, "representable as JSON"),
        ("[invalid:", InputError, "invalid YAML"),
        ("[]", InputError, "must be a mapping"),
        (" " * (128 * 1024 + 1), InputError, "size limit"),
    ],
)
def test_invalid_config(tmp_path: Path, contents: str, error_type: type, message: str) -> None:
    manifest = tmp_path / "bad.yaml"
    manifest.write_text(contents)
    with pytest.raises(error_type, match=message):
        resolve_config(manifest, tmp_path)


@pytest.mark.parametrize(
    "value", ["/tmp/bad", "../bad", ".git/refs", ".GIT/refs", "https://host/path", "C:\\path", "."]
)
def test_out_of_scope_paths_are_rejected(tmp_path: Path, value: str) -> None:
    with pytest.raises(InputError, match="within the workspace"):
        scoped_path(tmp_path, value)


def test_symlink_escape_and_git_alias_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    (root / "metadata").symlink_to(root / ".git", target_is_directory=True)
    for value in ["escape/output", "metadata/refs"]:
        with pytest.raises(InputError, match="outside its allowed scope"):
            scoped_path(root, value)


def test_cli_reports_field_and_does_not_echo_input(tmp_path: Path) -> None:
    invalid = tmp_path / "private-case.yaml"
    invalid.write_text(EXAMPLE + "seed: private-case\n")
    runner = CliRunner()
    result = runner.invoke(app, ["config", "resolve", str(invalid)])
    assert result.exit_code == 2
    assert "seed:" in result.stderr
    assert "private-case" not in result.stderr
    assert runner.invoke(app, ["config", "resolve", str(tmp_path / "missing")]).exit_code == 3


def test_untrusted_text_is_inert_and_the_allowlist_is_enforced(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel"
    payload = f"__import__('pathlib').Path({str(sentinel)!r}).touch()"
    data = decision_data(evidence=[payload])
    result = parse_proposal(json.dumps(data).encode(), ALLOWED)
    assert result.evidence == (payload,)
    assert not sentinel.exists()
    data["tool"] = "shell.execute"
    with pytest.raises(ProposalError, match="undeclared_tool"):
        parse_proposal(json.dumps(data).encode(), ALLOWED)


@pytest.mark.parametrize(
    "body, category",
    [
        (b"```json\n{}\n```", "malformed_json"),
        (b'{"action":"STOP","action":"RETRY"}', "malformed_json"),
        (b'{"confidence":NaN}', "malformed_json"),
        (b'{"confidence":1e999}', "malformed_json"),
        (b"{}", "invalid_decision"),
        (b" " * (MAX_PROPOSAL_BYTES + 1), "overlong_output"),
    ],
)
def test_invalid_model_output_classification(body: bytes, category: str) -> None:
    with pytest.raises(ProposalError, match=category):
        parse_proposal(body, ALLOWED)
