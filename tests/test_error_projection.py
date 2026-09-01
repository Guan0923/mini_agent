from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.api.error_handlers import install_error_handlers
from backend.domain import redact_sensitive_text, root_error, safe_error_message


def test_root_error_follows_explicit_cause() -> None:
    root = TimeoutError("socket timed out")
    wrapper = RuntimeError("Model stream failed")
    wrapper.__cause__ = root

    assert root_error(wrapper) is root
    assert safe_error_message(wrapper) == "socket timed out"


def test_root_error_follows_implicit_context() -> None:
    root = ValueError("invalid JSON at column 3")
    wrapper = RuntimeError("Decision failed")
    wrapper.__context__ = root

    assert root_error(wrapper) is root
    assert safe_error_message(wrapper) == "invalid JSON at column 3"


def test_root_error_prefers_explicit_cause() -> None:
    cause = ValueError("explicit")
    context = OSError("implicit")
    wrapper = RuntimeError("wrapper")
    wrapper.__cause__ = cause
    wrapper.__context__ = context

    assert root_error(wrapper) is cause


def test_root_error_ignores_deliberately_suppressed_context() -> None:
    hidden = ValueError("parser implementation detail")
    visible = RuntimeError("response ended prematurely")
    visible.__context__ = hidden
    visible.__suppress_context__ = True

    assert root_error(visible) is visible
    assert safe_error_message(visible) == "response ended prematurely"


def test_root_error_stops_at_a_cycle() -> None:
    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__cause__ = second
    second.__context__ = first

    assert root_error(first) is second
    assert safe_error_message(first) == "second"


def test_empty_root_message_falls_back_to_class_name() -> None:
    assert safe_error_message(RuntimeError()) == "RuntimeError"


def test_visible_error_message_redacts_sensitive_values() -> None:
    message = (
        "api_key=alpha; Authorization: Bearer beta, Cookie=session=gamma; "
        "Password: delta; Secret=epsilon; Token: zeta; API Key: eta; "
        '"api_token": "theta"'
    )

    assert redact_sensitive_text(message) == (
        "api_key=[REDACTED]; Authorization:[REDACTED], Cookie=[REDACTED]; "
        "Password:[REDACTED]; Secret=[REDACTED]; Token:[REDACTED]; API Key:[REDACTED]; "
        '"api_token:[REDACTED]"'
    )
    assert safe_error_message(RuntimeError(message)) == redact_sensitive_text(message)


def test_http_exception_keeps_status_and_projects_its_safe_root_message() -> None:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/wrapped")
    def wrapped() -> None:
        root = RuntimeError("Authorization: Bearer private-value")
        raise HTTPException(status_code=502, detail="Model request failed") from root

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/wrapped")

    assert response.status_code == 502
    assert response.json() == {"detail": "Authorization:[REDACTED]"}


def test_unhandled_http_exception_returns_500_with_safe_root_message() -> None:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/unhandled")
    def unhandled() -> None:
        try:
            raise OSError("Cookie=session-secret")
        except OSError as exc:
            raise RuntimeError("storage read failed") from exc

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/unhandled")

    assert response.status_code == 500
    assert response.json() == {"detail": "Cookie=[REDACTED]"}
