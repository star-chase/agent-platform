from app.workflow_model import Task, Workflow, dumps_model


def valid_workflow():
    return Workflow(
        id="workflow-1",
        goal="调研新能源汽车行业并生成报告",
        dag={
            "nodes": [
                {
                    "id": "collect_market",
                    "description": "收集市场规模",
                    "depends_on": [],
                }
            ]
        },
    )


def valid_task():
    return Task(
        id="task-1",
        workflow_id="workflow-1",
        node_id="collect_market",
        question="收集新能源汽车市场规模",
    )


def test_workflow_validation():
    workflow = valid_workflow()
    workflow.validate()
    assert workflow.status == "PLANNING"


def test_empty_workflow_goal_is_rejected():
    workflow = valid_workflow()
    workflow.goal = " "
    try:
        workflow.validate()
    except ValueError:
        pass
    else:
        raise AssertionError("空 goal 应该被拒绝")


def test_task_validation():
    task = valid_task()
    task.validate()
    assert task.depends_on == []


def test_task_to_dict_and_from_dict():
    task = valid_task()
    task.upstream_outputs = {"collect_policy": "政策数据"}
    restored = Task.from_dict(task.to_dict())
    assert restored == task


def test_workflow_to_dict_and_from_dict():
    workflow = valid_workflow()
    restored = Workflow.from_dict(workflow.to_dict())
    assert restored == workflow


def test_json_serialization_preserves_chinese_text():
    payload = dumps_model(valid_workflow())
    assert "新能源汽车" in payload