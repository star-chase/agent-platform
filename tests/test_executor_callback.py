from app.agent_fc_persistent import AgentFCPersistentExecutor


def test_notify_step_calls_injected_callback():
    received = []
    executor = AgentFCPersistentExecutor(
        llm_client=None,
        on_step=received.append,
    )
    step = {"iteration": 1, "type": "tool_call", "tool_name": "search"}

    executor._notify_step(step)

    assert received == [step]


def test_notify_step_without_callback_is_noop():
    executor = AgentFCPersistentExecutor(llm_client=None)
    executor._notify_step({"iteration": 1})
