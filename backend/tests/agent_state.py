"""Canonical message fixtures and background run observation for tests."""
from materialsagent.domain.models.agent import AgentRun as Run, identifier


def agent_run(*, goal="test", context=None, user_inputs=None, user_messages=None, **values):
    source = values.get("source_message_id", "message")
    messages = [{"message_id": source, "role": "USER", "text": goal, "attachments": []}]
    user_ids, context_ids = [source], []
    for item in context or []:
        identity = identifier()
        context_ids.append(identity)
        messages.append({"message_id": identity, "role": item.get("role", "user").upper(),
                         "text": item.get("content", ""), "attachments": item.get("additional_inputs", [])})
    for text in user_inputs or []:
        identity = identifier()
        user_ids.append(identity)
        messages.append({"message_id": identity, "role": "USER", "text": text, "attachments": []})
    for item in user_messages or []:
        if item["message_id"] not in user_ids:
            user_ids.append(item["message_id"])
            messages.append({**item, "role": "USER"})
    return Run(**values, messages=messages, user_message_ids=user_ids, context_message_ids=context_ids)


def post_message(client, path, *, json, headers=None, **options):
    """Existing scenario fixtures now exercise acceptance followed by background completion."""
    body = dict(json)
    mode = body.pop("mode", None)
    if mode == "RESUME_RUN":
        target = client.get("/api/v1/agent-runs/" + body.pop("agent_run_id")).json()["data"]
        body["reply_to"] = {"question_message_id": target["question_message_id"],
                            "waiting_version": body.pop("waiting_version")}
    accepted = client.post(path, json=body, headers=headers, **options)
    if accepted.status_code != 202:
        return accepted
    data = accepted.json()["data"]
    if data["idempotency_replayed"]:
        import httpx
        return httpx.Response(200, json={"data": data})
    advanced = wait_run(client, data["agent_run"]["agent_run_id"])
    if advanced.status_code != 200:
        return advanced
    # Preserve the submission envelope; advancement remains separately asserted in new API tests.
    import httpx
    return httpx.Response(200, json={"data": {**data, "agent_run": advanced.json()["data"]}})


def post_operation(client, path, *, json=None, headers=None):
    accepted = client.post(path, json=json, headers=headers)
    if accepted.status_code != 202:
        return accepted
    data = accepted.json()["data"]
    advanced = wait_run(client, data["agent_run"]["agent_run_id"])
    import httpx
    return httpx.Response(200, json={"data": {**data, "agent_run": advanced.json()["data"]}})


def wait_run(client, run_id, timeout=20):
    """Wait for both durable state and background closeout, without triggering work."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.get(f"/api/v1/agent-runs/{run_id}")
        if result.status_code != 200:
            return result
        data = result.json()["data"]
        runtime = client.app.state.agent_runtime
        if data["status"] not in {"PENDING", "RUNNING"} and not any(
            key[0] == run_id and not task.done() for key, task in list(runtime.running_tasks.items())
        ):
            return result
        time.sleep(0.02)
    raise AssertionError(f"Run did not settle: {run_id}")
