"""Canonical message fixtures and explicit request-hosted advancement for tests."""
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
    """Existing scenario fixtures now exercise acceptance followed by advancement."""
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
    advanced = client.post("/api/v1/agent-runs/" + data["agent_run"]["agent_run_id"] + "/advance",
                           json={"submission_id": data["submission_id"]})
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
    advanced = client.post(f"/api/v1/agent-runs/{data['agent_run']['agent_run_id']}/advance",
                          json={"submission_id": data["submission_id"]})
    if advanced.status_code != 200:
        return advanced
    import httpx
    return httpx.Response(200, json={"data": {**data, "agent_run": advanced.json()["data"]}})
