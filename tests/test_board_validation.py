from datetime import datetime, timezone
import io
import json
import unittest
from urllib.error import HTTPError, URLError

from job_harvester.board_validation import BoardValidator
from job_harvester.registry import Board


class Response(io.BytesIO):
    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def board(provider: str) -> Board:
    return Board(
        provider, "acme", None, True, datetime.now(timezone.utc),
        None, "unknown", None,
    )


def raising(error: Exception):
    def opener(*args: object, **kwargs: object) -> Response:
        raise error
    return opener


class BoardValidationTests(unittest.TestCase):
    def test_greenhouse_and_lever_valid_responses(self) -> None:
        cases = [
            ("greenhouse", {"jobs": [{"id": 1}], "meta": {"total": 1}}, 1),
            ("lever", [{"id": "one"}, {"id": "two"}], 2),
        ]
        for provider, payload, count in cases:
            with self.subTest(provider=provider):
                validator = BoardValidator(
                    opener=lambda *args, _payload=payload, **kwargs: Response(
                        json.dumps(_payload).encode()
                    )
                )
                result = validator.validate(board(provider))
                self.assertEqual((result.outcome, result.job_count), ("valid", count))

    def test_empty_boards_are_valid(self) -> None:
        for provider, payload in (("greenhouse", {"jobs": []}), ("lever", [])):
            with self.subTest(provider=provider):
                validator = BoardValidator(
                    opener=lambda *args, _payload=payload, **kwargs: Response(
                        json.dumps(_payload).encode()
                    )
                )
                result = validator.validate(board(provider))
                self.assertEqual((result.outcome, result.job_count), ("valid", 0))

    def test_404_is_invalid_but_other_failures_are_temporary(self) -> None:
        cases = [
            (HTTPError("https://api", 404, "Not Found", {}, None), "invalid"),
            (HTTPError("https://api", 503, "Unavailable", {}, None), "temporary"),
            (URLError("timeout"), "temporary"),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                result = BoardValidator(opener=raising(error)).validate(board("lever"))
                self.assertEqual(result.outcome, expected)
