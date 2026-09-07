from app.state_machine import InvalidTransitionError, transition


def test_valid_transition():
    task = {"status": "PENDING"}
    result = transition(task, "RUNNING")
    assert result["status"] == "RUNNING"
    assert "updated_at" in result


def test_invalid_transition():
    task = {"status": "COMPLETED"}
    try:
        transition(task, "RUNNING")
    except InvalidTransitionError:
        pass
    else:
        raise AssertionError("COMPLETED -> RUNNING 应该被拒绝")


def test_failed_can_retry():
    task = {"status": "FAILED"}
    transition(task, "RUNNING")
    assert task["status"] == "RUNNING"
