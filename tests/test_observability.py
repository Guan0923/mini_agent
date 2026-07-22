import json
from pathlib import Path

from mini_agent.observability import EventFanout, JsonlRunLogger
from mini_agent.planning import RuleBasedPlanner
from mini_agent.runtime import LegacyAgentRunner as AgentRunner
from mini_agent.runtime import RuntimeEvent
from mini_agent.tools import ToolRegistry


def test_jsonl_logger_persists_the_complete_event_stream(tmp_path: Path) -> None:
    events = []
    logger = JsonlRunLogger(tmp_path / "logs")
    state = AgentRunner(RuleBasedPlanner(), ToolRegistry(tmp_path)).run(
        "calculate 2 + 2",
        lambda _: False,
        on_event=EventFanout([events.append, logger]),
    )

    log_path = logger.path_for(state.run_id)
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

    assert log_path.exists()
    persistent_events = [event for event in events if event.kind != "assistant_message"]
    assert len(records) == len(persistent_events)
    assert "assistant_message" not in [record["kind"] for record in records]
    assert records[0]["kind"] == "run_started"
    assert records[-1]["kind"] == "run_finished"
    assert all(record["run_id"] == state.run_id for record in records)
    assert all(record["data"]["run_id"] == state.run_id for record in records)
    assert all(record["timestamp"] for record in records)



def test_jsonl_logger_redacts_and_summarizes_wire_payloads(tmp_path: Path) -> None:
    logger = JsonlRunLogger(tmp_path / "logs", include_full_messages=False)
    logger(
        RuntimeEvent(
            "model_response",
            "response",
            {
                "run_id": "run_wire",
                "wire_response": {
                    "content": "complete response",
                    "token": "visible-secret",
                },
            },
        )
    )

    record = json.loads(logger.path_for("run_wire").read_text(encoding="utf-8"))
    assert record["schema_version"] == 2
    assert record["data"]["wire_response"]["content"]["preview"] == "complete response"
    assert record["data"]["wire_response"]["token"] == "[REDACTED]"
    assert "visible-secret" not in json.dumps(record, ensure_ascii=False)
