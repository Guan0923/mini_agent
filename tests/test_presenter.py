from mini_agent.runtime import RuntimeEvent
from mini_agent.tui.presenter import TerminalPresenter


def test_presenter_renders_stream_and_response(capsys) -> None:
    presenter = TerminalPresenter()

    presenter.on_event(RuntimeEvent("strategy", "reactive", {"reason": "Simple task."}))
    presenter.on_event(RuntimeEvent("thinking_start"))
    presenter.on_event(RuntimeEvent("thinking_delta", "Thinking."))
    presenter.on_event(RuntimeEvent("thinking_end"))
    presenter.on_event(RuntimeEvent("response", "Hello."))

    assert capsys.readouterr().out == "STRATEGY reactive — Simple task.\nTHINKING\nThinking.\nRESPONSE\nHello.\n"
