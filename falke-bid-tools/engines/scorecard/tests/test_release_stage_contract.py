"""RELEASE-STAGE CONTRACT — the suite must pass in the tree that actually SHIPS.

Floyd's v0.4.2 C-1 ride-along. The canonical tree and the shipped tree are not
the same tree, and this file pins the difference.

WHAT HAPPENED
-------------
`test_cli_required_params.py` pointed `--matrix` at
`tests/fixtures/create_matrix_2bidders.xlsx` — a real workbook that exists on a
developer machine. `*.xlsx` is gitignored BY DESIGN, and `release.sh` stages a
`git archive` of TRACKED files only, so the fixture is absent from the stage.
P1-1's new unreadable-matrix pre-flight then fired before the band gate and the
test asserted the wrong exit code.

    267 tests green.  release.sh aborted at [6/8].  The release could not be cut.

That asymmetry IS the finding. It is the P0-2 lesson — "a green suite over a
tree that isn't the one that ships" — recurring one layer up: P0-2 was fixtures
frozen against a producer that no longer existed; this is a fixture that never
existed where it counted.

THE RULE THIS PINS
------------------
    A test may not CONSTRUCT a path under tests/fixtures/*.xlsx unless it
    self-selects with a skip guard. Building your own file in tmp_path is the
    other way out, and it satisfies this trivially: such a module never
    constructs the path at all, so it is never scanned.

`test_create_matrix_compat.py` already does this correctly, and has all along:
it guards on `os.path.exists(...)` and self-skips in the stage. That is the
pattern; this file makes it a rule rather than a habit.

HONEST LIMITS OF THIS PIN
-------------------------
It is a static tripwire, not a proof. It cannot know that a module's skip guard
covers the specific fixture it references — only that a module reaching for an
untracked fixture has *a* guard in it. The real proof is `release.sh` itself,
which runs the suite against the scrubbed stage at [6/8]. This pin exists so the
failure surfaces in a normal test run, at the moment the mistake is made, rather
than at the release that cannot be cut.

Both halves of it were proven before being trusted (Floyd's §5 rule: prove the
instrument on a known-positive and a known-negative first). Doing so caught two
false readings in the pin itself — see `_SELF_SELECTS` and `_code_only`.
"""
from __future__ import annotations

import io
import os
import re
import tokenize

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(TESTS_DIR, "fixtures")

# A path being CONSTRUCTED into tests/fixtures/ and ending in .xlsx — the shape
# of the mistake.
_FIXTURE_XLSX_USE = re.compile(
    r'["\']fixtures["\']\s*,\s*[^)\n]*\.xlsx|'          # os.path.join(..., "fixtures", "x.xlsx")
    r'FIXTURES\s*,\s*[^)\n]*\.xlsx|'                    # os.path.join(FIXTURES, f"...xlsx")
    r'["\'][^"\'\n]*fixtures/[^"\'\n]*\.xlsx["\']'      # a literal "…fixtures/x.xlsx"
)


def _code_only(src: str) -> str:
    """Source with comments and docstrings removed — what the module DOES, not
    what it says about itself.

    Load-bearing, and learned twice now. The first version of this pin scanned
    raw text and flagged `test_cli_required_params.py` — for the docstring that
    *describes* the very mistake it no longer makes. A checker that cannot tell
    a description from a dependency reports the file documenting the fix as the
    file containing the bug. (Same shape as the HTML `_visible()` helper in
    test_producer_live_compat: a comment naming the thing is not the thing.)

    Docstrings here are triple-quoted; path literals in code are not — so
    dropping COMMENT tokens and triple-quoted STRINGs leaves exactly the
    dependencies.
    """
    out = []
    try:
        toks = tokenize.generate_tokens(io.StringIO(src).readline)
        for tok_type, tok_str, _s, _e, _line in toks:
            if tok_type == tokenize.COMMENT:
                continue
            if tok_type == tokenize.STRING and (
                    tok_str.lstrip("rbufRBUF").startswith(('"""', "'''"))):
                continue
            out.append(tok_str)
    except tokenize.TokenError:      # pragma: no cover - unparseable test file
        return src
    return "\n".join(out)
# Self-selection means a SKIP GUARD, and only that.
#
# NOT `tmp_path`: the first draft of this pin accepted it, and the pin then
# failed its own known-positive — I re-introduced C-1 verbatim and it passed,
# because `tmp_path` appears in nearly every module (it is the fixture name in
# half the signatures here). A signal that is always present is not a signal.
#
# The tighter rule is also the truer one: a module that BUILDS its own file
# never constructs a tests/fixtures/*.xlsx path in the first place, so it is
# never scanned. Constructing that path IS the dependency, and the only honest
# mitigation for it is skipping when the file is absent.
_SELF_SELECTS = re.compile(r"skipif|pytest\.skip")


def _test_modules():
    for name in sorted(os.listdir(TESTS_DIR)):
        if name.startswith("test_") and name.endswith(".py"):
            yield name, os.path.join(TESTS_DIR, name)


def test_no_test_hard_depends_on_a_gitignored_fixture():
    """Every module reaching for a tests/fixtures/*.xlsx must self-select.

    If this fails: do NOT satisfy it by committing the fixture (*.xlsx is
    gitignored deliberately — client workbooks must never enter the release
    archive). Build what you need in tmp_path, or skip when it is absent.
    """
    offenders = []
    for name, path in _test_modules():
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        code = _code_only(src)
        if _FIXTURE_XLSX_USE.search(code) and not _SELF_SELECTS.search(code):
            offenders.append(name)
    assert not offenders, (
        f"These test modules depend on a gitignored tests/fixtures/*.xlsx "
        f"without self-selecting: {offenders}. Those files do NOT exist in "
        f"release.sh's staged tree (git archive of tracked files; .gitignore "
        f"carries '*.xlsx'), so the suite would be green here and RED in the "
        f"tree that ships. Build the file in tmp_path, or skip when absent — "
        f"see test_create_matrix_compat.py for the guard pattern."
    )


def test_fixture_workbooks_are_untracked_by_design():
    """The premise the rule rests on, pinned so it cannot drift silently.

    If someone ever un-ignores tests/fixtures/*.xlsx, the rule above becomes
    unnecessary — and, far more importantly, client workbooks would start
    entering the release archive. This asserts the .gitignore contract still
    says what the rule assumes it says.
    """
    gitignore = os.path.abspath(
        os.path.join(TESTS_DIR, "..", "..", "..", ".gitignore"))
    with open(gitignore, encoding="utf-8") as fh:
        rules = [ln.strip() for ln in fh]
    assert "*.xlsx" in rules, (
        "plugin/.gitignore no longer carries '*.xlsx'. Either the release "
        "archive now carries workbooks (a confidentiality regression), or this "
        "contract moved and test_release_stage_contract.py needs revisiting.")
    # the only xlsx that SHIP are the operator templates, un-ignored explicitly
    unignored = [r for r in rules if r.startswith("!") and r.endswith(".xlsx")]
    assert unignored, "expected the shipped operator templates to be un-ignored"
    assert all("templates/" in r for r in unignored), unignored


def test_the_stub_matrix_pattern_actually_satisfies_the_preflight():
    """The pre-flight only stats — it never opens. That property is what makes a
    zero-byte stub sufficient, and it is load-bearing for the C-1 fix, so pin it
    rather than trusting the comment."""
    import inspect

    from scorecard import cli
    src = inspect.getsource(cli.main)
    assert "os.path.isfile(args.matrix)" in src, (
        "the matrix pre-flight no longer stats-without-opening; "
        "test_cli_required_params._stub_matrix (a zero-byte file) may no "
        "longer be sufficient.")
