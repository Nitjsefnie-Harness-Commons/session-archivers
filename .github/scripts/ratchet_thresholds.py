"""Print the coverage floor in the thresholds record, or stage a raise of it.

`stage` writes a raised record only for a total over RAISE_HYSTERESIS above
the recorded one, so the raise commit stages nothing; the floor trails the
total by CALIBRATION_GAP and never falls. No file means no raise, so bad
input exits 1.
"""
import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
from typing import Any

CALIBRATION_GAP = Decimal('1.5')
RAISE_HYSTERESIS = Decimal('1.5')


def percent(text: str, field: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f'{field} is not a number: {text!r}') from None
    # The range coverage enforces on --fail-under.
    if not value.is_finite() or not 0 <= value <= 100:
        raise ValueError(f'{field} is not a percentage from 0 to 100: {text!r}')
    return value


def recorded(value: Any, field: str) -> Decimal:
    if type(value) not in (int, float):
        raise ValueError(f'{field} is not a JSON number: {value!r}')
    return percent(str(value), field)


def load(path: Path) -> tuple[Any, Decimal, Decimal]:
    record = json.loads(path.read_text(encoding='utf-8'))
    version = record['schema_version']
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValueError('schema_version is not 1')
    python = record['coverage']['python']
    if python['cell'] != 'ubuntu-latest':
        raise ValueError('coverage.python.cell is not "ubuntu-latest", the cell the coverage job measures')
    return (record, recorded(python['floor'], 'coverage.python.floor'),
            recorded(python['measured'], 'coverage.python.measured'))


def stage(path: Path, measured_text: str, out_dir: Path) -> None:
    record, floor, last = load(path)
    measured = percent(measured_text, '--measured')
    if measured - last <= RAISE_HYSTERESIS:
        print(f'No raise: {measured} is not more than {RAISE_HYSTERESIS} above the recorded {last}.')
        return
    python = record['coverage']['python']
    if measured - CALIBRATION_GAP > floor:
        python['floor'] = float(measured - CALIBRATION_GAP)
    python['measured'] = float(measured)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'ci-thresholds.json').write_text(json.dumps(record, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(f'Staged: floor {floor} -> {python["floor"]}, measured {last} -> {measured}.')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('floor').add_argument('thresholds', type=Path)
    staging = commands.add_parser('stage')
    staging.add_argument('thresholds', type=Path)
    staging.add_argument('--measured', required=True)
    staging.add_argument('--out-dir', required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'floor':
            print(load(args.thresholds)[1])
        else:
            stage(args.thresholds, args.measured, args.out_dir)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'{args.thresholds}: {error!r}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
