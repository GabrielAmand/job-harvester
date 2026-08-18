from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Callable

from job_harvester.batches import BatchError, BatchStore
from job_harvester.config import CareerOpsConfig
from job_harvester.storage import _timestamp
from job_harvester.revalidation import JobRevalidator


CATEGORY_DECISIONS = {
    "automatic_skip": "skip",
    "review": "needs_review",
    "good_candidate": "good_candidate",
    "priority_candidate": "priority_candidate",
}


class CareerOpsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CareerOpsEvaluation:
    job_id: int
    position: int
    company: str
    title: str
    status: str
    score: float | None
    category: str | None
    recommendation: str | None
    tracker_id: int | None
    result_path: str
    report_path: str | None
    cv_payload_path: str | None
    cv_html_path: str | None
    cv_pdf_path: str | None
    evaluated_at: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class CareerOpsBatchResult:
    evaluations: tuple[CareerOpsEvaluation, ...]
    already_completed: int

    @property
    def failed(self) -> int:
        return sum(item.status == "failed" for item in self.evaluations)


Runner = Callable[..., subprocess.CompletedProcess[str]]
Progress = Callable[[int, int, CareerOpsEvaluation], None]


def external_id(job_id: int) -> str:
    return f"job-harvester:{job_id}"


def result_path(job_id: int) -> str:
    return f"output/evaluations/job-harvester-{job_id}.json"


def evaluate_open_batch(
    database: str | Path,
    config: CareerOpsConfig,
    *,
    limit: int | None = None,
    force: bool = False,
    runner: Runner = subprocess.run,
    progress: Progress | None = None,
    revalidator: JobRevalidator | None = None,
) -> CareerOpsBatchResult:
    repository, script = _validate_integration(config)
    effective_limit = config.batch_size if limit is None else limit
    if effective_limit <= 0:
        raise CareerOpsError("evaluation limit must be greater than zero")

    with BatchStore(database) as store:
        batch = store.current()
        if batch is None:
            raise BatchError("there is no open batch to evaluate")
        rows = store.connection.execute(
            """
            SELECT j.id, bj.position, j.company, j.title, j.url,
                   e.career_ops_status
            FROM batch_jobs bj
            JOIN jobs j ON j.id = bj.job_id
            LEFT JOIN career_ops_evaluations e ON e.job_id = j.id
            WHERE bj.batch_id = ?
            ORDER BY bj.position
            """,
            (batch.id,),
        ).fetchall()

    completed = 0 if force else sum(row[5] == "completed" for row in rows)
    pending = [row for row in rows if force or row[5] != "completed"][:effective_limit]
    evaluations: list[CareerOpsEvaluation] = []
    total = len(pending)
    for index, row in enumerate(pending, 1):
        job_id, position, company, title, url = row[:5]
        relative_result = result_path(job_id)
        command = [
            config.node_command,
            str(script),
            "--url", str(url),
            "--external-id", external_id(job_id),
            "--json-out", relative_result,
        ]
        try:
            process = runner(
                command,
                cwd=repository,
                capture_output=True,
                text=True,
                check=False,
            )
            if process.returncode != 0:
                try:
                    document = _read_result(repository / relative_result)
                    parsed = _parse_result(
                        document, job_id, position, company, title, relative_result
                    )
                    evaluation = (
                        parsed if parsed.status == "failed" else _failed_evaluation(
                            job_id, position, company, title, relative_result,
                            _process_error(process),
                        )
                    )
                except (OSError, json.JSONDecodeError, CareerOpsError):
                    evaluation = _failed_evaluation(
                        job_id, position, company, title, relative_result,
                        _process_error(process),
                    )
            else:
                document = _read_result(repository / relative_result)
                evaluation = _parse_result(
                    document, job_id, position, company, title, relative_result
                )
        except (OSError, json.JSONDecodeError, CareerOpsError) as error:
            evaluation = _failed_evaluation(
                job_id, position, company, title, relative_result, str(error)
            )
        _persist_evaluation(database, evaluation)
        if evaluation.status == "completed":
            from job_harvester.applications import synchronize_evaluation
            synchronize_evaluation(
                database, config, evaluation.job_id, revalidator=revalidator
            )
        evaluations.append(evaluation)
        if progress is not None:
            progress(index, total, evaluation)
    return CareerOpsBatchResult(tuple(evaluations), completed)


def prepare_application_artifacts(
    database: str | Path,
    config: CareerOpsConfig,
    job_id: int,
    *,
    runner: Runner = subprocess.run,
    revalidator: JobRevalidator | None = None,
) -> CareerOpsEvaluation:
    """Ask Career-Ops to generate artifacts; Job Harvester never generates them."""
    repository, script = _validate_integration(config)
    from job_harvester.applications import ApplicationError, ApplicationStore
    with ApplicationStore(database) as application_store:
        application = application_store.get(job_id)
    if application.category == "automatic_skip":
        raise ApplicationError(f"job {job_id} is an automatic skip")
    if application.state in {"applied", "declined"}:
        raise ApplicationError(f"job {job_id} is {application.state}")
    if application.category == "review" and application.decision != "apply":
        raise ApplicationError(f"job {job_id} needs an explicit apply decision first")
    with BatchStore(database) as store:
        row = store.connection.execute(
            """
            SELECT j.id, COALESCE(bj.position, 0), j.company, j.title, j.url
            FROM jobs j
            LEFT JOIN batch_jobs bj ON bj.job_id=j.id
            WHERE j.id=? ORDER BY bj.batch_id DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
    if row is None:
        raise CareerOpsError(f"job not found: {job_id}")
    relative_result = result_path(job_id)
    command = [
        config.node_command, str(script), "--url", str(row[4]),
        "--external-id", external_id(job_id), "--json-out", relative_result,
        "--force-artifacts",
    ]
    try:
        process = runner(
            command, cwd=repository, capture_output=True, text=True, check=False
        )
        document = _read_result(repository / relative_result)
        evaluation = _parse_result(
            document, int(row[0]), int(row[1]), str(row[2]), str(row[3]), relative_result
        )
        if process.returncode != 0 and evaluation.status != "failed":
            evaluation = _failed_evaluation(
                int(row[0]), int(row[1]), str(row[2]), str(row[3]),
                relative_result, _process_error(process),
            )
    except (OSError, json.JSONDecodeError, CareerOpsError) as error:
        evaluation = _failed_evaluation(
            int(row[0]), int(row[1]), str(row[2]), str(row[3]),
            relative_result, str(error),
        )
    _persist_evaluation(database, evaluation)
    if evaluation.status == "failed":
        raise CareerOpsError(evaluation.error or "Career-Ops preparation failed")
    from job_harvester.applications import synchronize_evaluation
    synchronize_evaluation(database, config, job_id, revalidator=revalidator)
    return evaluation


def _validate_integration(config: CareerOpsConfig) -> tuple[Path, Path]:
    if not config.enabled:
        raise CareerOpsError("Career-Ops integration is disabled")
    if config.repository_path is None:
        raise CareerOpsError("Career-Ops repository_path is not configured")
    repository = config.repository_path.resolve()
    if not repository.is_dir():
        raise CareerOpsError(f"Career-Ops repository not found: {repository}")
    script = repository / "evaluate-job.mjs"
    if not script.is_file():
        raise CareerOpsError(f"Career-Ops evaluator not found: {script}")
    return repository, script


def _read_result(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CareerOpsError(f"Career-Ops result was not created: {path}") from error
    except OSError as error:
        raise CareerOpsError(f"could not read Career-Ops result {path}: {error}") from error


def _parse_result(
    document: object,
    job_id: int,
    position: int,
    company: str,
    title: str,
    relative_result: str,
) -> CareerOpsEvaluation:
    if not isinstance(document, dict):
        raise CareerOpsError("Career-Ops result must be a JSON object")
    if document.get("schema_version") != "1.0":
        raise CareerOpsError("Career-Ops result schema_version must be 1.0")
    if document.get("external_id") != external_id(job_id):
        raise CareerOpsError("Career-Ops result external_id does not match the job")
    if document.get("status") == "failed":
        meta = document.get("meta")
        evaluated_at = meta.get("evaluated_at") if isinstance(meta, dict) else None
        if evaluated_at is not None and not isinstance(evaluated_at, str):
            raise CareerOpsError("Career-Ops meta.evaluated_at must be a string or null")
        return CareerOpsEvaluation(
            job_id=job_id, position=position, company=company, title=title,
            status="failed", score=None, category=None, recommendation=None,
            tracker_id=None, result_path=relative_result, report_path=None,
            cv_payload_path=None, cv_html_path=None, cv_pdf_path=None,
            evaluated_at=evaluated_at,
            error=str(document.get("error") or "Career-Ops evaluation failed"),
        )
    if document.get("status") != "completed":
        raise CareerOpsError("Career-Ops result status must be completed or failed")
    evaluation = document.get("evaluation")
    artifacts = document.get("artifacts")
    tracker = document.get("tracker")
    meta = document.get("meta")
    if not all(isinstance(value, dict) for value in (evaluation, artifacts, tracker, meta)):
        raise CareerOpsError("Career-Ops result is missing required objects")
    category = evaluation.get("category")
    if category not in CATEGORY_DECISIONS:
        raise CareerOpsError(f"unsupported Career-Ops category: {category}")
    score = evaluation.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise CareerOpsError("Career-Ops evaluation score must be numeric")
    recommendation = evaluation.get("recommendation")
    if not isinstance(recommendation, str):
        raise CareerOpsError("Career-Ops recommendation must be a string")
    tracker_id = tracker.get("id")
    if not isinstance(tracker_id, int) or isinstance(tracker_id, bool):
        raise CareerOpsError("Career-Ops tracker.id must be an integer")
    evaluated_at = meta.get("evaluated_at")
    if not isinstance(evaluated_at, str) or not evaluated_at:
        raise CareerOpsError("Career-Ops meta.evaluated_at must be a string")
    paths = {}
    for field in ("report_path", "cv_payload_path", "cv_html_path", "cv_pdf_path"):
        value = artifacts.get(field)
        if value is not None and not isinstance(value, str):
            raise CareerOpsError(f"Career-Ops artifacts.{field} must be a string or null")
        paths[field] = value
    return CareerOpsEvaluation(
        job_id=job_id, position=position, company=company, title=title,
        status="completed", score=float(score), category=category,
        recommendation=recommendation, tracker_id=tracker_id,
        result_path=relative_result, report_path=paths["report_path"],
        cv_payload_path=paths["cv_payload_path"],
        cv_html_path=paths["cv_html_path"], cv_pdf_path=paths["cv_pdf_path"],
        evaluated_at=evaluated_at, error=None,
    )


def _failed_evaluation(
    job_id: int,
    position: int,
    company: str,
    title: str,
    relative_result: str,
    error: str,
) -> CareerOpsEvaluation:
    return CareerOpsEvaluation(
        job_id=job_id, position=position, company=company, title=title,
        status="failed", score=None, category=None, recommendation=None,
        tracker_id=None, result_path=relative_result, report_path=None,
        cv_payload_path=None, cv_html_path=None, cv_pdf_path=None,
        evaluated_at=_timestamp(datetime.now(timezone.utc)), error=error,
    )


def _process_error(process: subprocess.CompletedProcess[str]) -> str:
    detail = (process.stderr or process.stdout or "").strip()
    message = f"Career-Ops exited with code {process.returncode}"
    return f"{message}: {detail}" if detail else message


def _persist_evaluation(database: str | Path, evaluation: CareerOpsEvaluation) -> None:
    decision = (
        CATEGORY_DECISIONS[evaluation.category]
        if evaluation.status == "completed" and evaluation.category is not None
        else None
    )
    reviewed_at = (
        evaluation.evaluated_at if evaluation.status == "completed" else None
    )
    review_state = "reviewed" if evaluation.status == "completed" else "in_review"
    with BatchStore(database) as store, store.connection:
        store.connection.execute(
            """
            INSERT INTO career_ops_evaluations (
                job_id, career_ops_status, career_ops_score, career_ops_category,
                career_ops_recommendation, career_ops_tracker_id,
                career_ops_result_path, career_ops_report_path,
                career_ops_cv_payload_path, career_ops_cv_html_path,
                career_ops_cv_pdf_path, career_ops_evaluated_at, career_ops_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                career_ops_status=excluded.career_ops_status,
                career_ops_score=excluded.career_ops_score,
                career_ops_category=excluded.career_ops_category,
                career_ops_recommendation=excluded.career_ops_recommendation,
                career_ops_tracker_id=excluded.career_ops_tracker_id,
                career_ops_result_path=excluded.career_ops_result_path,
                career_ops_report_path=excluded.career_ops_report_path,
                career_ops_cv_payload_path=excluded.career_ops_cv_payload_path,
                career_ops_cv_html_path=excluded.career_ops_cv_html_path,
                career_ops_cv_pdf_path=excluded.career_ops_cv_pdf_path,
                career_ops_evaluated_at=excluded.career_ops_evaluated_at,
                career_ops_error=excluded.career_ops_error
            """,
            (
                evaluation.job_id, evaluation.status, evaluation.score,
                evaluation.category, evaluation.recommendation, evaluation.tracker_id,
                evaluation.result_path, evaluation.report_path,
                evaluation.cv_payload_path, evaluation.cv_html_path,
                evaluation.cv_pdf_path, evaluation.evaluated_at, evaluation.error,
            ),
        )
        store.connection.execute(
            """
            INSERT INTO job_reviews(job_id, review_state, decision, reviewed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                review_state=excluded.review_state,
                decision=excluded.decision,
                reviewed_at=excluded.reviewed_at
            """,
            (evaluation.job_id, review_state, decision, reviewed_at),
        )
