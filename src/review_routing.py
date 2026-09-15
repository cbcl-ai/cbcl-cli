"""Choose a default reviewer that is independent of the task executor."""


def default_reviewer(task: dict) -> str:
    return "auditor" if task.get("assigned_agent") == "manager-assistant" else "manager-assistant"
