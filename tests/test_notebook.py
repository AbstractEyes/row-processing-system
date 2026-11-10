"""The Colab notebook: its structure, its safety rules, and that its cells run."""

import ast
import contextlib
import io
import json
import sys
import types
from pathlib import Path

import huggingface_hub
import pytest

from rps import runtime
from rps.config import DEFAULT_WORKERS, Config, RouteConfig
from rps.events import replay
from rps.keys import SECRET_ENV_MAP, looks_like_secret
from rps.store.local import LocalStore

from fakes import FakeBucketApi

NOTEBOOK_PATH = Path(__file__).resolve().parents[1] / "notebooks" / "controller_colab.ipynb"
COLAB_URL = "https://colab.research.google.com/github/AbstractEyes/row-processing-system/blob/main/notebooks/controller_colab.ipynb"
INSTALL_COMMAND = 'pip install "git+https://github.com/AbstractEyes/row-processing-system@v0.4.2"'


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


def test_the_install_step_says_colab_may_ask_for_a_restart_and_where_to_go_on(notebook):
    intro = source(next(cell for cell in notebook["cells"] if cell["id"] == "intro"))
    step_one = next(line for line in intro.splitlines() if line.startswith("1. "))
    assert "Colab may ask to restart the session" in step_one and "go on from cell 2" in step_one
    install = source(code_cells(notebook)[0])
    comment = " ".join(line.lstrip("# ") for line in install.splitlines() if line.startswith("#"))
    assert "Colab may ask to restart the session" in comment and "Restart, then go on from cell 2" in comment
    assert "replaces packages Colab" in step_one and "replaces packages Colab" in comment  # and says why


def test_the_code_cells_come_in_the_documented_order(notebook):
    assert [cell["id"] for cell in code_cells(notebook)] == ["install", "secrets", "dryrun", "config", "tasks", "seat", "run", "status"]


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
    tasks_file = tmp_path / "work" / "state" / "control" / "tasks.json"  # the tasks file is in the state store
    assert tasks_file.exists() and not (tmp_path / "work" / "tasks.json").exists()
    assert json.loads(tasks_file.read_text(encoding="utf-8"))[0]["purpose"] == "test"
    assert "tasks file: store:control/tasks.json" in printed


def test_the_dry_run_cell_sends_nothing_unless_a_route_is_named(notebook, tmp_path, monkeypatch):
    from rps import dryrun

    calls = []
    monkeypatch.setattr(dryrun, "dry_run", lambda *args, **kwargs: calls.append((args, kwargs)))
    namespace, _printed = execute(notebook, tmp_path, monkeypatch, {}, only=("dryrun",))
    assert namespace["DRY_RUN_ROUTE"] is None and calls == []
    # naming a route runs the dry run, five calls, through the function (no argument parser)
    cell = next(cell for cell in code_cells(notebook) if cell["id"] == "dryrun")
    text = runnable(source(cell)).replace("DRY_RUN_ROUTE = None", 'DRY_RUN_ROUTE = "luna-sync"')
    exec(compile(text, "dryrun", "exec"), {})
    assert calls == [(("luna-sync",), {"calls": 5})]


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


# ------------------------------------------------------------------ what the notebook says about GPT, the tasks and the bucket


def cell_named(notebook, cell_id):
    return next(cell for cell in notebook["cells"] if cell["id"] == cell_id)


def test_the_example_route_for_gpt_is_the_one_call_route_and_the_notebook_never_suggests_a_gpt_batch(notebook):
    config_text = source(cell_named(notebook, "config"))
    assert "GPT is used one call at a time in this project" in config_text
    assert "GPT is used one call at a time in this project" in source(cell_named(notebook, "intro"))
    assert "luna-batch" not in "\n".join(source(cell) for cell in notebook["cells"])
    # The commented example is a real route: uncomment it and it loads.
    start = config_text.index('# "luna-sync": {')
    block = config_text[start : config_text.index("# },", start) + len("# },")]
    uncommented = "\n".join(line.strip().removeprefix("# ") for line in block.splitlines())
    ((name, spec),) = ast.literal_eval("{" + uncommented + "}").items()
    route = RouteConfig.from_dict(name, spec)
    assert (name, route.provider, route.model, route.kind) == ("luna-sync", "openai", "gpt-6-luna", "api")
    assert route.workers == DEFAULT_WORKERS == 32 and '"workers": 32' in block


def test_the_tasks_file_is_a_file_in_the_state_store_and_the_install_names_this_release(notebook, tmp_path, monkeypatch):
    namespace, printed = execute(notebook, tmp_path, monkeypatch, {}, only=("config", "tasks"))
    assert namespace["config"].tasks_file == "store:control/tasks.json" and namespace["config"].tasks_store_path() == "control/tasks.json"
    assert "tasks file: store:control/tasks.json" in printed
    assert "version 0.4.2" in source(cell_named(notebook, "intro")) and "@v0.4.2" in source(cell_named(notebook, "install"))


def test_the_bucket_setting_can_name_a_folder_and_the_tasks_cell_writes_into_that_folder(notebook, tmp_path, monkeypatch):
    config_text = source(cell_named(notebook, "config"))
    assert "owner/rps-state/test" in config_text and "owner/rps-state/prod" in config_text
    api = FakeBucketApi()
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda token=None: api)
    monkeypatch.setenv("HF_TOKEN", "a-token-for-this-test")
    namespace = {}
    for cell_id in ("config", "tasks"):
        text = runnable(source(cell_named(notebook, cell_id))).replace("/content/rps_work", (tmp_path / "work").as_posix())
        if cell_id == "tasks":  # the person sets the bucket in the configuration, folder included
            namespace["config"] = Config.from_dict({**namespace["CONFIG"], "bucket": "owner/rps-state/test"})
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(text, cell_id, "exec"), namespace)
    assert list(api.files) == ["test/control/tasks.json"]
    assert json.loads(api.files["test/control/tasks.json"])[0]["batch"] == "demo-1"
    assert api.created == [{"bucket_id": "owner/rps-state", "private": True, "exist_ok": True}]  # the bucket, not the folder


# ------------------------------------------------------------------ the optional cell that starts an API review seat


def seat_cell_text(notebook, seat):
    """The seat cell's Python with the setting filled in."""
    return runnable(source(cell_named(notebook, "seat"))).replace("SEAT = None", f"SEAT = {seat!r}", 1)


def test_the_seat_cell_sits_between_the_tasks_cell_and_the_run_cell_and_the_introduction_lists_it(notebook):
    cell_ids = [cell["id"] for cell in notebook["cells"]]
    assert cell_ids.index("config") < cell_ids.index("tasks") < cell_ids.index("seat") < cell_ids.index("run")
    steps = [line for line in source(cell_named(notebook, "intro")).splitlines() if line[:2] in {f"{number}." for number in range(1, 10)}]
    assert len(steps) == 8 and "Optionally start an automated reviewer" in steps[5] and steps[6].startswith("7. Run the controller")


def test_the_seat_cell_does_nothing_until_a_seat_is_named(notebook, tmp_path, monkeypatch):
    from rps.review import api_seat

    monkeypatch.setattr(api_seat, "start_in_thread", lambda *args, **kwargs: pytest.fail("a seat was started"))
    namespace, printed = execute(notebook, tmp_path, monkeypatch, {})
    assert namespace["SEAT"] is None and namespace["seat"] is None
    assert "reviewer reviewer-1" not in printed  # the cell printed nothing about a seat


def test_the_seat_cell_starts_a_seat_on_a_thread_from_a_small_dict_and_prints_its_status(notebook, tmp_path, monkeypatch):
    from rps.review import api_seat
    from seat_helpers import FakeAnthropic

    monkeypatch.setattr(api_seat, "make_client", lambda: FakeAnthropic())
    namespace, _printed = execute(notebook, tmp_path, monkeypatch, {}, only=("config", "tasks"))
    text = seat_cell_text(notebook, {"reviewer_id": "reviewer-1", "model": "claude-haiku-5-5", "effort": "high", "budget_usd": 5.0})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(text, "seat", "exec"), namespace)
    seat = namespace["seat"]
    try:
        assert seat.alive
        assert "reviewer reviewer-1 (claude-haiku-5-5, effort high): running" in out.getvalue() and "spent $0.00 of $5.00" in out.getvalue()
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(text, "seat", "exec"), namespace)  # run again: it reports, it does not start another
        assert namespace["seat"] is seat
    finally:
        seat.stop(timeout=30)
    assert not seat.alive


def test_a_seat_started_by_the_cell_answers_the_controllers_packets_and_the_run_cell_reads_its_verdicts(notebook, tmp_path, monkeypatch):
    import time

    from rps import rules
    from rps.review import api_seat
    from seat_helpers import FakeAnthropic

    api = FakeAnthropic()
    api.batches.auto_end = True
    monkeypatch.setattr(api_seat, "make_client", lambda: api)
    namespace, _printed = execute(notebook, tmp_path, monkeypatch, {}, only=("config", "tasks"))
    # The pretend run loads no rule pool, so its packets carry review.brief_version: make it the brief this package renders.
    review = {**namespace["CONFIG"]["review"], "brief_version": rules.rules_version()}
    namespace["config"] = Config.from_dict({**namespace["CONFIG"], "review": review})
    run_cell = runnable(source(cell_named(notebook, "run")))
    seat_text = seat_cell_text(notebook, {"reviewer_id": "reviewer-1", "model": "claude-haiku-5-5", "budget_usd": 5.0, "poll_seconds": 0.01})
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(seat_text, "seat", "exec"), namespace)
        exec(compile(run_cell, "run", "exec"), namespace)  # the controller publishes the packets
    seat = namespace["seat"]
    try:
        deadline = time.monotonic() + 30
        while seat.status()["packets_answered"] < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seat.status()["packets_answered"] == 1
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(run_cell, "run", "exec"), namespace)  # run again: the controller reads the verdicts
    finally:
        seat.stop(timeout=30)
    events = replay(LocalStore(tmp_path / "work" / "state")).events
    verdicts = [event.payload for event in events if event.kind == "verdict_received" and event.payload["reviewer_id"] == "reviewer-1"]
    assert len(verdicts) == 3 and {payload["verdict"] for payload in verdicts} == {"keep"}
    assert namespace["controller"].state.counts()["packets_open"] == 1  # reviewer-2's packet waits for its desk seat
