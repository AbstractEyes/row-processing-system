"""The Colab notebook: its structure, its safety rules, and that its cells run."""

import ast
import contextlib
import io
import json
import sys
import types
from pathlib import Path

import pytest

from rps import runtime
from rps.events import replay
from rps.keys import SECRET_ENV_MAP, looks_like_secret
from rps.store.local import LocalStore

NOTEBOOK_PATH = Path(__file__).resolve().parents[1] / "notebooks" / "controller_colab.ipynb"
COLAB_URL = "https://colab.research.google.com/github/AbstractEyes/row-processing-system/blob/main/notebooks/controller_colab.ipynb"
INSTALL_COMMAND = 'pip install "git+https://github.com/AbstractEyes/row-processing-system@v0.2.0"'


@pytest.fixture(scope="module")
def notebook():
    return json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8"))


def source(cell):
    return "".join(cell["source"])


def code_cells(notebook):
    return [cell for cell in notebook["cells"] if cell["cell_type"] == "code"]


def runnable(text):
    """A cell's Python with the shell and magic lines removed."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("!", "%")))


# ------------------------------------------------------------------ structure


def test_the_notebook_is_a_version_4_notebook_with_no_saved_output(notebook):
    assert notebook["nbformat"] == 4
    for cell in notebook["cells"]:
        assert cell["cell_type"] in ("markdown", "code")
        assert isinstance(cell["source"], list) and all(isinstance(line, str) for line in cell["source"])
        if cell["cell_type"] == "code":
            assert cell["outputs"] == [] and cell["execution_count"] is None


def test_the_first_cell_is_the_open_in_colab_badge(notebook):
    first = notebook["cells"][0]
    assert first["cell_type"] == "markdown"
    text = source(first)
    assert text.startswith("[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)]")
    assert text.endswith(f"({COLAB_URL})")


def test_the_install_cell_installs_the_tagged_release_from_github(notebook):
    assert INSTALL_COMMAND in source(code_cells(notebook)[0])


def test_the_code_cells_come_in_the_documented_order(notebook):
    assert [cell["id"] for cell in code_cells(notebook)] == ["install", "secrets", "dryrun", "config", "tasks", "run", "status"]


def test_cells_are_whole_and_colab_safe(notebook):
    for cell in notebook["cells"]:
        text = source(cell)
        assert "argparse" not in text and "__file__" not in text and "sys.argv" not in text
    for cell in code_cells(notebook):
        ast.parse(runnable(source(cell)), filename=cell["id"])  # every code cell is valid Python on its own


def test_the_notebook_holds_no_key_and_never_reads_the_environment_itself(notebook):
    text = "\n".join(source(cell) for cell in notebook["cells"])
    assert not looks_like_secret(text)
    code = "\n".join(source(cell) for cell in code_cells(notebook))
    assert "os.environ" not in code and "environ[" not in code and "getenv" not in code
    for secret_name, _env_name in SECRET_ENV_MAP:
        assert secret_name in text  # the secrets cell and the introduction name every secret


# ------------------------------------------------------------------ running the cells


class FakeUserdata:
    """Stands in for google.colab.userdata."""

    def __init__(self, values):
        self.values = values

    def get(self, name):
        if name not in self.values:
            raise KeyError(f"secret {name} does not exist")
        return self.values[name]


def execute(notebook, tmp_path, monkeypatch, secrets, skip=("install",), namespace=None, only=None):
    """Run the notebook's code cells in one namespace, with Colab pretended. Returns (namespace, printed text)."""
    fake_colab = types.ModuleType("google.colab")
    fake_colab.userdata = FakeUserdata(secrets)
    monkeypatch.setitem(sys.modules, "google.colab", fake_colab)
    for _secret_name, env_name in SECRET_ENV_MAP:
        monkeypatch.setenv(env_name, "")  # registers the variable, so whatever the cells set is undone afterwards
    original = runtime.build_controller

    def build_without_waiting(config, **options):
        options.setdefault("sleep", lambda seconds: None)
        return original(config, **options)

    monkeypatch.setattr(runtime, "build_controller", build_without_waiting)
    namespace = {} if namespace is None else namespace
    work = (tmp_path / "work").as_posix()
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        for cell in code_cells(notebook):
            if cell["id"] in skip or (only and cell["id"] not in only):
                continue
            text = runnable(source(cell)).replace("/content/rps_work", work)
            exec(compile(text, cell["id"], "exec"), namespace)
            if cell["id"] == "config":
                namespace["TIME_BUDGET_SECONDS"] = 60  # a run that does not end by itself stops, rather than hanging the test
    return namespace, buffer.getvalue()


def test_the_cells_run_as_a_dry_run_and_print_only_secret_names(notebook, tmp_path, monkeypatch):
    claude_key = "sk-ant-" + "q" * 40
    hub_token = "hf_" + "q" * 34
    namespace, printed = execute(notebook, tmp_path, monkeypatch, {"CLAUDE_API": claude_key, "HF_TOKEN": hub_token})
    for expected in (
        "CLAUDE_API: ready",
        "OPENAI_API: not available",
        "GEMINI_API: not available",
        "DEEPSEEK_API: not available",
        "HF_TOKEN: ready",
    ):
        assert expected in printed
    assert claude_key not in printed and hub_token not in printed and "qqqq" not in printed
    assert "run ended: idle" in printed
    assert "job 6 lines" in printed and "time spent" in printed and "time left" in printed
    for expected in ("demo", "(route total)", "production", "rebound", "prices dated"):
        assert expected in printed  # the status cell
    controller = namespace["controller"]
    assert controller.state.counts()["packets_open"] == 2  # the six rows now wait for two reviewers
    assert controller.state.spend("demo", "test").calls == 6
    assert (tmp_path / "work" / "tasks.json").exists()
    assert json.loads((tmp_path / "work" / "tasks.json").read_text(encoding="utf-8"))[0]["purpose"] == "test"


def test_the_dry_run_cell_sends_nothing_unless_a_route_is_named(notebook, tmp_path, monkeypatch):
    from rps import dryrun

    calls = []
    monkeypatch.setattr(dryrun, "dry_run", lambda *args, **kwargs: calls.append((args, kwargs)))
    namespace, _printed = execute(notebook, tmp_path, monkeypatch, {}, only=("dryrun",))
    assert namespace["DRY_RUN_ROUTE"] is None and calls == []
    # naming a route runs the dry run, five calls, through the function (no argument parser)
    cell = next(cell for cell in code_cells(notebook) if cell["id"] == "dryrun")
    text = runnable(source(cell)).replace("DRY_RUN_ROUTE = None", 'DRY_RUN_ROUTE = "luna-batch"')
    exec(compile(text, "dryrun", "exec"), {})
    assert calls == [(("luna-batch",), {"calls": 5})]


def test_the_secrets_cell_reports_a_secret_that_is_not_shared_without_failing(notebook, tmp_path, monkeypatch):
    _namespace, printed = execute(notebook, tmp_path, monkeypatch, {}, only=("secrets",))
    assert printed.splitlines() == [f"{secret_name}: not available" for secret_name, _env in SECRET_ENV_MAP]


def test_running_the_run_cell_again_resumes_without_repeating_a_call(notebook, tmp_path, monkeypatch):
    namespace, _printed = execute(notebook, tmp_path, monkeypatch, {})
    before = len(replay(LocalStore(tmp_path / "work" / "state")).events)
    again, printed = execute(notebook, tmp_path, monkeypatch, {}, namespace=namespace, only=("run",))
    events = replay(LocalStore(tmp_path / "work" / "state")).events
    assert len(events) > before  # the second run recorded that it started and stopped
    assert [event.kind for event in events].count("call_prepared") == 6
    assert [event.kind for event in events].count("batch_submitted") == 1
    assert "run ended: idle" in printed
    assert again["controller"].state.spend("demo", "test").calls == 6
