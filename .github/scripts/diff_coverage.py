"""Print, as Markdown, the coverage of the executable lines a change adds.

Information, never a gate: a move or a fix tested elsewhere scores low for
reasons a reviewer judges. The XML lists every statement of the measured
package, so a line it omits is not executable; the diff numbers lines as the
measured merge tree does. A changed module the XML never names is listed,
lest a path-spelling mismatch read as nothing measured. A path git quotes
(tab, quote, backslash) exits 1 rather than be misread.
"""
import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

MEASURED_PACKAGE = 'session_archivers/'
_HUNK = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')


def executable_lines(coverage_xml: Path) -> dict[str, dict[int, int]]:
    root = ET.parse(coverage_xml).getroot()
    if root.tag != 'coverage':
        raise ValueError('root is not <coverage>')
    measured: dict[str, dict[int, int]] = {}
    for node in root.iter('class'):
        lines = measured.setdefault(node.get('filename') or '', {})
        for line in node.iter('line'):
            number, hits = int(line.get('number', '')), int(line.get('hits', ''))
            if number <= 0 or hits < 0:
                raise ValueError(f'bad line record {line.attrib} in {node.get("filename")!r}')
            # A file can span several <class> nodes.
            lines[number] = max(lines.get(number, 0), hits)
    if not any(measured.values()):
        raise ValueError('no line records')
    return measured


def added_lines(diff_text: str) -> dict[str, set[int]]:
    added: dict[str, set[int]] = {}
    path = ''
    number = new_left = 0
    for line in diff_text.split('\n'):
        # Before the header test: an added `++ x` reads `+++ x`.
        if new_left > 0 and line[:1] in ('+', ' '):
            if line[0] == '+':
                added.setdefault(path, set()).add(number)
            number, new_left = number + 1, new_left - 1
        elif line.startswith('+++ '):
            path = line[4:].split('\t', 1)[0].removeprefix('b/')
            if path.startswith('"'):
                raise ValueError(f'quoted path not supported: {path}')
        elif hunk := _HUNK.match(line):
            number, new_left = int(hunk[1]), int(hunk[2] or 1)
    return added


def _ranges(numbers: list[int]) -> str:
    spans: list[list[int]] = []
    for number in sorted(numbers):
        if spans and number == spans[-1][1] + 1:
            spans[-1][1] = number
        else:
            spans.append([number, number])
    return ', '.join(str(low) if low == high else f'{low}-{high}' for low, high in spans)


def render(measured: dict[str, dict[int, int]], added: dict[str, set[int]]) -> str:
    rows: list[str] = []
    covered = total = 0
    for path in sorted(added):
        lines = measured.get(path, {})
        touched = sorted(number for number in added[path] if number in lines)
        if touched:
            missed = [number for number in touched if lines[number] == 0]
            rows.append(f'| `{path}` | {len(touched) - len(missed)} | {len(touched)} | {_ranges(missed) or "—"} |')
            covered += len(touched) - len(missed)
            total += len(touched)
    unmeasured = sorted(path for path in added if path.startswith(MEASURED_PACKAGE) and path.endswith('.py') and path not in measured)
    out = ['### Coverage of this change', '']
    if total:
        percent = min(100.0 * covered / total, 100.0 if covered == total else 99.9)
        out += [f'**{percent:.1f}%** of added executable lines covered ({covered}/{total}).', '',
                '| File | Covered | Added | Missed lines |', '| --- | ---: | ---: | --- |', *rows]
    else:
        out.append(f'No executable line was added under `{MEASURED_PACKAGE}`, the only code the coverage job measures.')
    if unmeasured:
        out += ['', 'Changed package modules the coverage report does not name:', *(f'- `{path}`' for path in unmeasured)]
    return '\n'.join(out) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--coverage', required=True, type=Path, help='Cobertura XML from `coverage xml`')
    parser.add_argument('--diff', required=True, type=Path, help='unified diff of the measured tree against its base')
    args = parser.parse_args()
    try:
        # Only line numbers reach the report.
        added = added_lines(args.diff.read_bytes().decode('utf-8', errors='replace'))
        report = render(executable_lines(args.coverage), added)
    except (OSError, ET.ParseError, ValueError) as error:
        print(f'diff coverage not reported: {error}', file=sys.stderr)
        return 1
    sys.stdout.write(report)
    return 0


if __name__ == '__main__':
    sys.exit(main())
