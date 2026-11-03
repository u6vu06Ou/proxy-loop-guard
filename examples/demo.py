"""Offline demo: python -m examples.demo. No model calls or credentials."""
import json

from proxy_loop_guard import LoopDetected, LoopGuard


def main():
    guard = LoopGuard()
    request = {"model": "demo-model", "messages": [
        {"role": "user", "content": "Find the configuration file"},
    ]}
    for count in range(1, 13):
        request["messages"].append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"read-{count}", "type": "function", "function": {
                "name": "Read", "arguments": json.dumps({"file_path": "/project/config.py"}),
            }}],
        })
        before = len(request["messages"])
        try:
            guard.check(request, session_id="demo")
        except LoopDetected as error:
            print(f"Call {count}: BLOCKED -- {error.detail}")
            break
        if len(request["messages"]) > before:
            print(f"Call {count}: WARNING -- change method or wrap up")
        else:
            print(f"Call {count}: allowed")


if __name__ == "__main__":
    main()
