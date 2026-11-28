"""Commands for the local ToolSwap CLI."""

import re
import sqlite3
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from toolswap.config import InputError, parse_yaml_record, resolve_config, validation_summary
from toolswap.contracts import ToolContract
from toolswap.determinism import canonical_json
from toolswap.recovery.evidence import verify_bundle
from toolswap.replay import ArtifactError
from toolswap.scenario import run_scenario

app = typer.Typer(
    help="Local reliability benchmark for stateful tool failures.", add_completion=False
)
config_app = typer.Typer(help="Resolve run configuration.")
contract_app = typer.Typer(help="Validate versioned tool contracts.")
scenario_app = typer.Typer(help="Run a local mock scenario.")
trace_app = typer.Typer(help="Inspect local recovery evidence.")
app.add_typer(config_app, name="config")
app.add_typer(contract_app, name="contract")
app.add_typer(scenario_app, name="scenario")
app.add_typer(trace_app, name="trace")


def _failure(error: InputError | ValidationError | OSError) -> typer.Exit:
    if isinstance(error, OSError):
        reason = error.strerror or type(error).__name__
        typer.echo(f"Could not read or resolve input ({reason}).", err=True)
        return typer.Exit(3)
    message = validation_summary(error) if isinstance(error, ValidationError) else str(error)
    typer.echo(message, err=True)
    return typer.Exit(2)


@config_app.command("resolve")
def config_resolve(path: Path) -> None:
    """Print the canonical JSON for a YAML run configuration."""
    try:
        manifest = resolve_config(path, Path.cwd())
    except (InputError, ValidationError, OSError) as exc:
        raise _failure(exc) from None
    typer.echo(canonical_json(manifest.model_dump(mode="json")).decode("utf-8"))


@contract_app.command("validate")
def contract_validate(path: Path) -> None:
    """Check a YAML tool contract and print its version."""
    try:
        contract = parse_yaml_record(path, ToolContract)
    except (InputError, ValidationError, OSError) as exc:
        raise _failure(exc) from None
    typer.echo(f"{contract.tool_id}@{contract.version}")


@scenario_app.command("run")
def scenario_run(manifest: Annotated[Path, typer.Option("--manifest")]) -> None:
    """Run a bundled scenario and write replay evidence."""
    root = Path.cwd()
    try:
        result = run_scenario(manifest, root)
    except ArtifactError as exc:
        typer.echo(f"Artifact integrity error: {exc}", err=True)
        raise typer.Exit(3) from None
    except sqlite3.Error as exc:
        typer.echo(f"SQLite infrastructure failure ({type(exc).__name__}).", err=True)
        raise typer.Exit(3) from None
    except (InputError, ValidationError, OSError) as exc:
        raise _failure(exc) from None
    outcome = "ERROR" if result.infrastructure_failure else "PASS" if result.passed else "FAIL"
    typer.echo(f"Outcome: {outcome}")
    typer.echo(f"Artifacts: {result.run_dir.relative_to(root.resolve())}")
    typer.echo(f"State hash: {result.state_hash}")
    typer.echo(f"Trace hash: {result.trace_hash}")
    if result.infrastructure_failure:
        raise typer.Exit(3)
    if not result.passed:
        raise typer.Exit(1)


@trace_app.command("verify")
def trace_verify(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    expected_trace_hash: Annotated[str | None, typer.Option("--expected-trace-hash")] = None,
) -> None:
    """Validate a sealed recovery bundle offline."""
    if expected_trace_hash is not None and not re.fullmatch(
        r"sha256:[0-9a-f]{64}", expected_trace_hash
    ):
        typer.echo("Expected a sha256: digest with 64 lowercase hexadecimal digits.", err=True)
        raise typer.Exit(2)
    try:
        digest = verify_bundle(run_dir, expected_trace_hash)
    except OSError as exc:
        reason = str(exc) if isinstance(exc, ArtifactError) else type(exc).__name__
        typer.echo(f"Artifact integrity error: {reason}", err=True)
        raise typer.Exit(3) from None
    typer.echo(f"Integrity: PASS\nTrace hash: {digest}")


def main() -> None:
    app()
