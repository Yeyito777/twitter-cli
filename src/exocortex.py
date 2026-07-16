import json
import os
import socket
import subprocess
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_ROOT = Path(os.environ.get("EXOCORTEX_CONFIG_DIR", "").strip() or (REPO_ROOT / "config"))


def _detect_worktree_name():
    try:
        git_dir = subprocess.check_output(
            ["git", "rev-parse", "--git-dir"],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        git_common_dir = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        git_dir_path = (REPO_ROOT / git_dir).resolve()
        git_common_dir_path = (REPO_ROOT / git_common_dir).resolve()
        if git_dir_path != git_common_dir_path:
            return Path(git_dir).name
    except Exception:
        pass
    return None


def _socket_path():
    worktree = _detect_worktree_name()
    runtime_dir = CONFIG_ROOT / "runtime"
    if worktree:
        runtime_dir = runtime_dir / worktree
    return runtime_dir / "exocortexd.sock"


def exocortex_request(command_type, response_type, *, timeout_seconds=10, **fields):
    """Send one newline-delimited JSON command to exocortexd and await its reply."""
    socket_path = _socket_path()
    if not socket_path.exists():
        raise RuntimeError(
            "exocortexd is not running. Start exocortexd to manage supervised tool daemons."
        )

    req_id = f"external_tool_{os.getpid()}_{uuid.uuid4().hex}"
    payload = {"type": command_type, "reqId": req_id, **fields}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_seconds)
    try:
        sock.connect(str(socket_path))
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))

        buffer = ""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                raise RuntimeError("Connection closed before exocortexd replied")
            buffer += chunk.decode("utf-8")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if event.get("reqId") != req_id:
                    continue
                if event.get("type") == "error":
                    raise RuntimeError(event.get("message") or "exocortexd returned an error")
                if event.get("type") == response_type:
                    return event
    finally:
        try:
            sock.close()
        except Exception:
            pass


def manage_external_tool_daemon(tool_name, action, timeout_seconds=10):
    event = exocortex_request(
        "manage_external_tool_daemon",
        "external_tool_daemon_result",
        timeout_seconds=timeout_seconds,
        toolName=tool_name,
        action=action,
    )
    return event.get("status") or {}


def register_external_notification_source(
    tool_name,
    source_id,
    source_label,
    source_description=None,
    *,
    timeout_seconds=10,
):
    source = {"id": source_id, "label": source_label}
    if source_description:
        source["description"] = source_description
    event = exocortex_request(
        "register_external_notification_source",
        "external_notification_source",
        timeout_seconds=timeout_seconds,
        toolName=tool_name,
        source=source,
    )
    return event.get("source") or {}


def list_external_notification_subscriptions(
    *,
    tool_name=None,
    source_id=None,
    conv_id=None,
    timeout_seconds=10,
):
    fields = {}
    if tool_name is not None:
        fields["toolName"] = tool_name
    if source_id is not None:
        fields["sourceId"] = source_id
    if conv_id is not None:
        fields["convId"] = conv_id
    event = exocortex_request(
        "list_external_notification_subscriptions",
        "external_notification_subscriptions",
        timeout_seconds=timeout_seconds,
        **fields,
    )
    return event.get("subscriptions") or []


def subscribe_external_notification(
    tool_name,
    source_id,
    conv_id,
    *,
    delivery="wake",
    source_label=None,
    source_description=None,
    timeout_seconds=10,
):
    if delivery not in ("wake", "inbox"):
        raise ValueError("delivery must be 'wake' or 'inbox'")
    fields = {
        "toolName": tool_name,
        "sourceId": source_id,
        "convId": conv_id,
        "delivery": delivery,
    }
    if source_label is not None:
        fields["sourceLabel"] = source_label
    if source_description is not None:
        fields["sourceDescription"] = source_description
    event = exocortex_request(
        "subscribe_external_notification",
        "external_notification_subscription",
        timeout_seconds=timeout_seconds,
        **fields,
    )
    return event.get("subscription") or {}


def unsubscribe_external_notification(
    *,
    subscription_id=None,
    tool_name=None,
    source_id=None,
    conv_id=None,
    timeout_seconds=10,
):
    if subscription_id:
        fields = {"subscriptionId": subscription_id}
    else:
        if not (tool_name and source_id and conv_id):
            raise ValueError(
                "provide subscription_id or all of tool_name, source_id, and conv_id"
            )
        fields = {"toolName": tool_name, "sourceId": source_id, "convId": conv_id}
    event = exocortex_request(
        "unsubscribe_external_notification",
        "external_notification_subscriptions",
        timeout_seconds=timeout_seconds,
        **fields,
    )
    return event.get("subscriptions") or []


def publish_external_notification(
    tool_name,
    source_id,
    event_id,
    text,
    *,
    occurred_at=None,
    timeout_seconds=30,
):
    fields = {
        "toolName": tool_name,
        "sourceId": source_id,
        "eventId": event_id,
        "text": text,
    }
    if occurred_at is not None:
        fields["occurredAt"] = occurred_at
    return exocortex_request(
        "publish_external_notification",
        "external_notification_publish_result",
        timeout_seconds=timeout_seconds,
        **fields,
    )
