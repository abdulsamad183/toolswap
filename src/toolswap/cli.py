"""Foundation commands for the local ToolSwap CLI."""

from pathlib import Path

import typer
from pydantic import ValidationError

from toolswap.config import InputError, parse_yaml_record, resolve_config, validation_summary
from toolswap.contracts import ToolContract
from toolswap.determinism import canonical_json

app = typer.Typer(
    help="Local reliability benchmark for stateful tool failures.", add_completion=False
)
config_app = typer.Typer(help="Resolve run configuration.")
contract_app = typer.Typer(help="Validate versioned tool contracts.")
app.add_typer(config_app, name="config")
app.add_typer(contract_app, name="contract")


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


def main() -> None:
    app()
