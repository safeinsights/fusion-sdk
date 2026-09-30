"""Content-free log audit: marker strings in params, bodies and remote error messages never reach a log line."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from safeinsights_fusion import Fusion, RemoteError, Settings, _log

from .conftest import FakeFactory
from .helpers import ScriptedSource, error_envelope, ok_table

PARAM_MARKER = "MARKER_PARAM_7f3a"
BODY_MARKER = "MARKER_BODY_9c1d"
ERROR_MARKER = "MARKER_ERR_5e22"


def test_destination_logs_are_content_free(
    fake: FakeFactory, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    pair = fake("happy")
    replies: list[Any] = [ok_table([[BODY_MARKER, 1]]), error_envelope("HANDLER_ERROR", f"Traceback: {ERROR_MARKER}")]
    ep = pair.endpoints[0].source
    with (
        ScriptedSource(ep.endpoint, ep.token, handler=lambda _q: replies.pop(0)),
        caplog.at_level(logging.DEBUG, logger=_log.LOGGER_NAME),
    ):
        fusion = Fusion.connect(pair.env("destination"), settings=settings)
        r = fusion.peer().request("counts_by_group", {"person_ids": [PARAM_MARKER], "group_by": PARAM_MARKER})
        assert r.to_records()[0]["grade"] == BODY_MARKER
        with pytest.raises(RemoteError, match=ERROR_MARKER):
            fusion.peer().request("counts_by_group", {"person_ids": [PARAM_MARKER]})
        fusion.complete()
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert caplog.records, "expected event lines"
    for marker in (PARAM_MARKER, BODY_MARKER, ERROR_MARKER, ep.token, pair.endpoints[0].destination.token):
        assert marker not in text
    events = {rec.getMessage().split()[1] for rec in caplog.records}
    assert {
        "connect.start",
        "ready.ok",
        "round.start",
        "round.complete",
        "round.remote_error",
        "complete.start",
        "complete.leg",
    } <= events
    assert all(rec.getMessage().startswith("fusion ") for rec in caplog.records)
