from app.retry import is_retryable_error, retry_with_backoff


class FakeAPIError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def test_retryable_error_classification():
    assert is_retryable_error(TimeoutError()) is True
    assert is_retryable_error(FakeAPIError(429)) is True
    assert is_retryable_error(FakeAPIError(503)) is True
    assert is_retryable_error(FakeAPIError(401)) is False


def test_retry_eventually_succeeds_and_uses_backoff():
    calls = {"count": 0}
    delays = []

    def flaky_fn():
        calls["count"] += 1
        if calls["count"] < 3:
            raise TimeoutError("暂时超时")
        return "成功"

    result = retry_with_backoff(
        flaky_fn,
        max_retries=3,
        base_delay=1.0,
        sleep_fn=delays.append,
        jitter_fn=lambda: 0.0,
    )
    assert result == "成功"
    assert calls["count"] == 3
    assert delays == [1.0, 2.0]


def test_non_retryable_error_is_raised_immediately():
    calls = {"count": 0}
    delays = []

    def invalid_request():
        calls["count"] += 1
        raise FakeAPIError(401)

    try:
        retry_with_backoff(
            invalid_request,
            max_retries=3,
            sleep_fn=delays.append,
            jitter_fn=lambda: 0.0,
        )
    except FakeAPIError as error:
        assert error.status_code == 401
    else:
        raise AssertionError("401 应该立即抛出，不能重试")

    assert calls["count"] == 1
    assert delays == []


def test_exhausted_retries_raise_last_error():
    calls = {"count": 0}

    def always_timeout():
        calls["count"] += 1
        raise TimeoutError("持续超时")

    try:
        retry_with_backoff(
            always_timeout,
            max_retries=2,
            sleep_fn=lambda _: None,
            jitter_fn=lambda: 0.0,
        )
    except TimeoutError:
        pass
    else:
        raise AssertionError("超过最大重试次数后应该抛出异常")

    assert calls["count"] == 3


def test_invalid_retry_parameters_are_rejected():
    try:
        retry_with_backoff(lambda: "ok", max_retries=-1)
    except ValueError:
        pass
    else:
        raise AssertionError("负数 max_retries 应该抛 ValueError")

    try:
        retry_with_backoff(lambda: "ok", base_delay=-1)
    except ValueError:
        pass
    else:
        raise AssertionError("负数 base_delay 应该抛 ValueError")
