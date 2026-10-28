"""``python -m plurai_mockenv`` — the canonical standalone entrypoint.

Builds a service via :func:`plurai_mockenv.build_service` and blocks in
``serve_forever``. The deployed/generated env keeps a thin ``run.py`` shim that
calls ``build_service`` directly (it owns the YAML loading + typed-RPC registry
wiring); this module is the engine's own convenience CLI
for a standalone boot.
"""

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from .builder import build_service


def _load_exposed_fields(path: Path) -> Mapping[str, Iterable[str]]:
    """Load the exposed-fields whitelist from a JSON or YAML file.

    JSON parses with the stdlib. YAML is parsed only if PyYAML happens to be
    installed — it is NOT a runtime dependency of plurai_mockenv (the runtime
    closure stays at stdlib + pydantic + fastapi/uvicorn), so a YAML
    file with PyYAML absent raises a clear error rather than silently degrading.
    """
    text = path.read_text()
    if path.suffix in ('.json',):
        data = json.loads(text)
    else:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise SystemExit(
                f'cannot load {path}: PyYAML is not installed and the file is not .json. Install pyyaml or pass a JSON exposed-fields file.'
            ) from exc
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise SystemExit(f'exposed-fields file {path} must contain a mapping at the top level')
    return data


def _load_env_file(path: Path) -> None:
    """Best-effort ``.env`` loader (KEY=VALUE lines) using the stdlib only.

    Mirrors the generated run.py's env-file behavior without adding a
    python-dotenv runtime dep. Existing environment values are NOT overridden.
    """
    import os

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog='python -m plurai_mockenv')
    parser.add_argument('--host', default='127.0.0.1', help='bind address (default: loopback)')
    parser.add_argument('--port', type=int, default=8199, help='bind port (default: 8199)')
    parser.add_argument('--env-file', type=Path, default=None, help='KEY=VALUE env file to load before importing mocks')
    parser.add_argument('--mocks', default=None, help='dotted module path whose @mock/@observe decorators to import')
    parser.add_argument('--exposed-fields', type=Path, default=None, help='JSON/YAML exposed-fields whitelist')
    args = parser.parse_args(argv)

    if args.env_file is not None:
        if not args.env_file.exists():
            raise SystemExit(f'--env-file {args.env_file} does not exist')
        _load_env_file(args.env_file)

    exposed: Mapping[str, Iterable[str]] | None = None
    if args.exposed_fields is not None:
        if not args.exposed_fields.exists():
            raise SystemExit(f'--exposed-fields {args.exposed_fields} does not exist')
        exposed = _load_exposed_fields(args.exposed_fields)

    service = build_service(
        host=args.host,
        port=args.port,
        mocks_module=args.mocks,
        exposed_state_fields=exposed,
    )
    print(f'Listening on http://{args.host}:{args.port}', file=sys.stderr)
    service.serve_forever()


if __name__ == '__main__':  # pragma: no cover — entry-point guard
    main()
