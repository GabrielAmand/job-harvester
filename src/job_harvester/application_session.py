from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from typing import Callable, TextIO

from job_harvester.applications import (
    Application,
    ApplicationError,
    ApplicationStore,
    AppliedResult,
    mark_applied,
    synchronize_evaluation,
)
from job_harvester.career_ops import prepare_application_artifacts
from job_harvester.config import CareerOpsConfig


Input = Callable[[str], str]
Opener = Callable[[str | Path], bool]
Copier = Callable[[str], bool]
PathConverter = Callable[[Path], str | None]


def _read_action(input_fn: Input, prompt: str) -> str:
    try:
        return input_fn(prompt).strip().casefold()
    except (EOFError, KeyboardInterrupt):
        return "q"


def open_external(target: str | Path) -> bool:
    """Best-effort OS/WSL open. Failure is deliberately non-fatal."""
    value = str(target)
    for executable in ("wslview", "explorer.exe", "xdg-open"):
        command = shutil.which(executable)
        if command is None:
            continue
        try:
            process = subprocess.run(
                [command, value], capture_output=True, text=True, check=False
            )
        except OSError:
            continue
        if process.returncode == 0:
            return True
    return False


def copy_windows_clipboard(value: str) -> bool:
    """Copy literal text through Windows interop without invoking a shell."""
    executable = shutil.which("clip.exe")
    if executable is None:
        return False
    try:
        process = subprocess.run(
            [executable], input=value, capture_output=True, text=True, check=False
        )
    except OSError:
        return False
    return process.returncode == 0


def windows_path(path: Path) -> str | None:
    """Return the Windows/UNC representation WSL exposes to file pickers."""
    if not path.is_file():
        return None
    executable = shutil.which("wslpath")
    if executable is None:
        return None
    try:
        process = subprocess.run(
            [executable, "-w", str(path.resolve())],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    converted = process.stdout.strip()
    return converted if process.returncode == 0 and converted else None


def artifact_path(repository: Path | None, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute() or repository is None:
        return path
    return repository.resolve() / path


def render_card(
    database: Path, config: CareerOpsConfig, item: Application,
    position: int, total: int, output: TextIO,
) -> None:
    with ApplicationStore(database) as store:
        job = store.job(item.job_id)
    report = artifact_path(config.repository_path, item.report_path)
    cv = artifact_path(config.repository_path, item.cv_pdf_path)
    mode = "full remote" if job.full_remote else job.work_mode.replace("_", " ")
    remote = mode if job.remote_scope == "unknown" else f"{mode} ({job.remote_scope})"
    score = f"{item.score:g} / 5" if item.score is not None else "—"
    print("-" * 50, file=output)
    print(f"[{position}/{total}] #{item.job_id} {item.company} — {item.title}", file=output)
    print(f"Source:         {item.source}", file=output)
    print(f"Location:       {job.location or '—'}", file=output)
    print(f"Work mode:      {remote}", file=output)
    print(f"Score:          {score}", file=output)
    print(f"Category:       {item.category}", file=output)
    print(f"Recommendation: {item.recommendation}", file=output)
    print(f"State:          {item.state}", file=output)
    print(f"Preparation:    {item.preparation_status or 'not started'}", file=output)
    if item.preparation_error:
        print(f"Prep error:     {item.preparation_error}", file=output)
    print(f"Report:         {report or '—'}", file=output)
    print(f"CV:             {cv or '—'}", file=output)
    print(f"Apply:          {item.url or '—'}", file=output)
    print("-" * 50, file=output)


def _open(target: str | Path | None, label: str, opener: Opener, output: TextIO) -> None:
    if not target:
        print(f"{label}: unavailable", file=output)
    elif not opener(target):
        print(f"{label}: {target}", file=output)


def _prepare(
    database: Path, config: CareerOpsConfig, job_id: int
) -> Application:
    try:
        item = synchronize_evaluation(database, config, job_id)
    except ApplicationError as error:
        if "completed Career-Ops evaluation not found" not in str(error):
            raise
        prepare_application_artifacts(database, config, job_id)
        with ApplicationStore(database) as store:
            return store.get(job_id)
    if item.state != "ready_to_apply":
        prepare_application_artifacts(database, config, job_id)
    with ApplicationStore(database) as store:
        return store.get(job_id)


def run_session(
    database: Path,
    config: CareerOpsConfig,
    *,
    input_fn: Input = input,
    output: TextIO,
    opener: Opener = open_external,
    copier: Copier = copy_windows_clipboard,
    path_converter: PathConverter = windows_path,
) -> int:
    with ApplicationStore(database) as store:
        snapshot = store.list_session()
    if not snapshot:
        print("No applications currently require action.", file=output)
        return 0

    applied_count = declined_count = deferred_count = 0
    index = 0
    while index < len(snapshot):
        original = snapshot[index]
        with ApplicationStore(database) as store:
            item = store.get(original.job_id)
        render_card(database, config, item, index + 1, len(snapshot), output)
        cv = artifact_path(config.repository_path, item.cv_pdf_path)
        cv_ready = cv is not None and cv.is_file()
        if item.state == "needs_review":
            actions = "[o] open application  [u] copy URL  [r] open report  [a] decide apply  [s] decline  [n] next  [q] quit"
        elif item.state == "ready_to_apply":
            actions = "[o] open application  [u] copy URL  [c] open CV  [p] copy CV path  [f] open CV folder  [r] open report  [a] applied  [s] decline  [n] next  [q] quit"
        else:
            actions = "[o] open application  [u] copy URL  [p] prepare CV  [r] open report  [n] next  [q] quit"
        print(actions, file=output)
        action = _read_action(input_fn, "Action: ")
        if action == "q":
            break
        if action == "n":
            deferred_count += 1
            index += 1
            continue
        if action == "o":
            _open(item.url, "Application URL", opener, output)
            continue
        if action == "u":
            if item.url and copier(item.url):
                print("Application URL copied to clipboard.", file=output)
            else:
                print(f"Could not copy application URL.\n{item.url or 'Application URL unavailable.'}", file=output)
            continue
        if action == "c":
            if not cv_ready:
                print("CV not ready.", file=output)
            else:
                _open(cv, "CV", opener, output)
            continue
        if action == "p" and cv_ready:
            converted = path_converter(cv)
            if converted and copier(converted):
                print("CV Windows path copied to clipboard.", file=output)
            else:
                print(f"Could not copy CV Windows path.\nCV: {cv}", file=output)
            continue
        if action == "f" and cv_ready:
            _open(cv.parent, "CV folder", opener, output)
            continue
        if action == "r":
            _open(artifact_path(config.repository_path, item.report_path), "Report", opener, output)
            continue
        if action == "s" and item.state in {"needs_review", "ready_to_apply"}:
            with ApplicationStore(database) as store:
                store.decline(item.job_id)
            declined_count += 1
            print(f"Declined: {item.company} — {item.title}", file=output)
            index += 1
            continue
        if action in {"a", "p"} and item.state != "ready_to_apply":
            try:
                if action == "a" and item.state == "needs_review":
                    with ApplicationStore(database) as store:
                        store.decide(item.job_id, "apply")
                elif item.state == "needs_review":
                    print("Choose apply before preparing a review-only CV.", file=output)
                    continue
                prepared = _prepare(database, config, item.job_id)
                print(f"Job {item.job_id}: {prepared.state}.", file=output)
            except Exception as error:
                print(f"Preparation failed: {error}", file=output)
                with ApplicationStore(database) as store:
                    if store.is_expired(item.job_id):
                        print("Job is no longer available; moving on.", file=output)
                        index += 1
            continue
        if action == "a" and item.state == "ready_to_apply":
            if not item.url.strip():
                print(
                    "Could not record Applied: no application/job URL is available.",
                    file=output,
                )
                continue
            confirmation = _read_action(input_fn,
                f"Confirm you submitted {item.company} — {item.title}? [y/N] "
            )
            if confirmation not in {"y", "yes"}:
                print("Applied status not recorded.", file=output)
                continue
            try:
                result: AppliedResult = mark_applied(database, config, item.job_id)
            except Exception as error:
                print(f"Could not record Applied: {error}", file=output)
                with ApplicationStore(database) as store:
                    if store.is_expired(item.job_id):
                        print("Job is no longer available; moving on.", file=output)
                        index += 1
                continue
            applied_count += 1
            print(f"Applied: {item.company} — {item.title}", file=output)
            print(f"Recorded at: {result.application.applied_at.isoformat()}", file=output)
            tracker = "synchronized" if result.tracker_synchronized else f"pending ({result.tracker_error})"
            print(f"Career-Ops tracker: {tracker}", file=output)
            print("Loading next opportunity...", file=output)
            index += 1
            continue
        print("Unknown action.", file=output)

    print(
        f"Applied this session: {applied_count} | Declined: {declined_count} | Deferred: {deferred_count}",
        file=output,
    )
    return 0
