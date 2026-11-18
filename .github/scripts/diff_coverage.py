"""Print, as Markdown, the coverage of the executable lines a change adds.

Information, never a gate: a move or a fix tested elsewhere scores low for
reasons a reviewer judges. The XML lists every statement of the measured
package, so a line it omits is not executable; the diff numbers lines as the
measured merge tree does. A changed module the XML never names is listed,
lest a path-spelling mismatch read as nothing measured. A path git quotes
(tab, quote, backslash) exits 1 rather than be misread. `--self-test` runs
the parsers over inline fixtures; ci-gate.yml's aggregate job runs it, as
no suite under tests/ covers this script.
"""
import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

MEASURED_PACKAGE = 'session_archivers/'
_HUNK = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')


def executable_lines(coverage_xml: str | bytes) -> dict[str, dict[int, int]]:
    root = ET.fromstring(coverage_xml)
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
        subject = 'measured added executable lines' if unmeasured else 'added executable lines'
        out += [f'**{percent:.1f}%** of {subject} covered ({covered}/{total}).', '',
                '| File | Covered | Added | Missed lines |', '| --- | ---: | ---: | --- |', *rows]
    elif unmeasured:
        out.append('Nothing was measured: the coverage report does not name the changed modules below.')
    else:
        out.append(f'No executable line was added under `{MEASURED_PACKAGE}`, the only code the coverage job measures.')
    if unmeasured:
        out += ['', 'Changed package modules the coverage report does not name:', *(f'- `{path}`' for path in unmeasured)]
    return '\n'.join(out) + '\n'


SELF_TEST_DIFF = '''\
diff --git a/session_archivers/a_gone.py b/session_archivers/a_gone.py
deleted file mode 100644
--- a/session_archivers/a_gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-gone = 1
--- a
diff --git a/session_archivers/old.py b/session_archivers/new name.py
similarity index 80%
rename from session_archivers/old.py
rename to session_archivers/new name.py
--- a/session_archivers/old.py
+++ b/session_archivers/new name.py\t
@@ -3 +3 @@ y = 2
-z = 3
\\ No newline at end of file
+z = 33
\\ No newline at end of file
diff --git a/session_archivers/mod.py b/session_archivers/mod.py
--- a/session_archivers/mod.py
+++ b/session_archivers/mod.py
@@ -2,0 +3,2 @@ b = 2
+++ b
+c = 3
@@ -5,2 +7,3 @@
 e = 5
+d = 4
 f = 6
'''


def self_test() -> None:
    added = added_lines(SELF_TEST_DIFF)
    assert added == {'session_archivers/new name.py': {3}, 'session_archivers/mod.py': {3, 4, 8}}, added
    added.update({'session_archivers/data.json': {1}, 'tests/test_x.py': {1}})
    measured = executable_lines(
        '<coverage><class filename="session_archivers/mod.py"><lines><line number="3" hits="1"/>'
        '<line number="4" hits="0"/><line number="8" hits="2"/></lines></class>'
        '<class filename="session_archivers/mod.py"><lines><line number="8" hits="0"/></lines></class></coverage>')
    report = render(measured, added)
    assert '**66.7%** of measured added executable lines covered (2/3).' in report, report
    assert '| `session_archivers/mod.py` | 2 | 3 | 4 |' in report and '- `session_archivers/new name.py`' in report, report
    assert '- `session_archivers/mod.py`' not in report and '| `session_archivers/new name.py`' not in report, report
    assert 'data.json' not in report and 'test_x.py' not in report, report
    report = render(executable_lines('<coverage><class filename="session_archivers/other.py"><lines>'
                                     '<line number="1" hits="1"/></lines></class></coverage>'), added)
    assert 'Nothing was measured' in report and '- `session_archivers/mod.py`' in report and '%' not in report, report
    report = render({'m.py': {**dict.fromkeys(range(1, 2000), 1), 2000: 0}}, {'m.py': set(range(1, 2001))})
    assert '**99.9%**' in report, report
    line = '<line number="{}" hits="{}"/>'
    for bad in ('<coverage/>', '<coverage><class filename="m.py"><lines/></class></coverage>',
                f'<report><class filename="m.py">{line.format(1, 1)}</class></report>',
                f'<coverage><class filename="m.py">{line.format(0, 1)}</class></coverage>',
                f'<coverage><class filename="m.py">{line.format(1, -1)}</class></coverage>', '+++ "b/m\\tn.py"'):
        try:
            (added_lines if bad.startswith('+') else executable_lines)(bad)
        except ValueError:
            continue
        raise AssertionError(f'accepted {bad}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--coverage', type=Path, help='Cobertura XML from `coverage xml`')
    parser.add_argument('--diff', type=Path, help='unified diff of the measured tree against its base')
    parser.add_argument('--self-test', action='store_true', help='check the parsers on inline fixtures')
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print('diff_coverage self-test passed')
        return 0
    if args.coverage is None or args.diff is None:
        parser.error('--coverage and --diff are required')
    try:
        # Only line numbers reach the report.
        added = added_lines(args.diff.read_bytes().decode('utf-8', errors='replace'))
        report = render(executable_lines(args.coverage.read_bytes()), added)
    except (OSError, ET.ParseError, ValueError) as error:
        print(f'diff coverage not reported: {error}', file=sys.stderr)
        return 1
    sys.stdout.write(report)
    return 0


if __name__ == '__main__':
    sys.exit(main())
