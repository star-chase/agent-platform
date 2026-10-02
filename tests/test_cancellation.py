from datetime import datetime, timedelta, timezone

from app.cancellation import is_task_cancelled, is_task_timed_out


def test_cancelled_status_is_detected():
    assert is_task_cancelled({"status": "CANCELLED"}) is True
    assert is_task_cancelled({"status": "RUNNING"}) is False


def test_running_task_times_out():
    now = datetime.now(timezone.utc)
    task = {
        "status": "RUNNING",
        "started_at": (now - timedelta(seconds=10)).isoformat(),
    }
    assert is_task_timed_out(task, now=now, timeout_seconds=5) is True


def test_completed_task_does_not_time_out():
    now = datetime.now(timezone.utc)
    task = {
        "status": "COMPLETED",
        "started_at": (now - timedelta(days=1)).isoformat(),
    }
    assert is_task_timed_out(task, now=now, timeout_seconds=5) is False


def test_task_without_started_at_does_not_time_out():
    assert is_task_timed_out({"status": "RUNNING"}, timeout_seconds=0) is False
