"""A deterministic mock of an OpenAI-compatible chat endpoint.

Tests drive it with a script file: a JSON object mapping a model name to a
list of steps. Each step is one response, in order:

    {"kind": "text",   "content": "..."}                       plain text
    {"kind": "tool",   "name": "bash", "arguments": {...}}     native tool call
    {"kind": "raw",    "tool_calls": [...]}                    verbatim tool calls
    {"kind": "error",  "status": 500}                          an upstream failure
    {"kind": "fold"}                                           end the stream early
    {"kind": "delay",  "seconds": 0.2}                         stall, then text

Every request is appended to a JSONL file, so a test can assert on the exact
payload the app sent (which tools, how many requests) and on the order of
turns. No network, no keys, no state outside the two temp files.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"


class MockState:
    def __init__(self, script_path, record_path):
        self.script = {}
        if script_path and os.path.exists(script_path):
            with open(script_path, encoding="utf-8") as handle:
                self.script = json.load(handle)
        self.record_path = record_path
        self.requests = []
        self.count = 0
        self.lock = threading.Lock()

    def steps_for(self, model):
        return list(self.script.get(model, [{"kind": "text", "content": "ok"}]))

    def record(self, payload):
        self.requests.append(payload)
        if self.record_path:
            with open(self.record_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def count(self, model=None):
        return sum(1 for r in self.requests if model is None or r.get("model") == model)


def _tool_call(step):
    return {
        "id": step.get("id") or "call_1",
        "type": "function",
        "function": {
            "name": step["name"],
            "arguments": json.dumps(step.get("arguments") or {}),
        },
    }


def _payload(model, step, index):
    """Build the chat completion object for one step."""
    reasoning = step.get("reasoning")
    if step.get("kind") == "tool":
        message = {
            "role": "assistant",
            "content": step.get("content") or None,
            "tool_calls": [_tool_call(step)],
        }
    elif step.get("kind") == "raw":
        message = {
            "role": "assistant",
            "content": step.get("content") or None,
            "tool_calls": step.get("tool_calls") or [],
        }
    else:
        message = {"role": "assistant", "content": step.get("content", "ok")}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "id": f"chatcmpl-mock-{index}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 6,
            "completion_tokens_details": {"reasoning_tokens": 0},
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }


def _stream(model, step, index):
    """SSE chunks for one step, matching OpenAI's streaming shape."""
    done_id = f"chatcmpl-mock-{index}"
    reasoning = step.get("reasoning")
    if step.get("kind") == "tool":
        call = _tool_call(step)
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call["id"],
                                    "type": "function",
                                    "function": {
                                        "name": call["function"]["name"],
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
        ]
        # reasoning tokens arrive before the call when the model exposes them
        if reasoning:
            thought_chunks = [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "reasoning_content": reasoning[start : start + 11]
                            },
                        }
                    ]
                }
                for start in range(0, len(reasoning), 11)
            ]
            chunks[1:1] = thought_chunks
        arguments = call["function"]["arguments"]
        for start in range(0, len(arguments), 7):
            chunks.append(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {
                                            "arguments": arguments[start : start + 7]
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
    else:
        text = step.get("content", "ok")
        chunks = []
        # reasoning tokens arrive before the answer when the model exposes them
        if reasoning:
            for start in range(0, len(reasoning), 11):
                chunks.append(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "reasoning_content": reasoning[
                                        start : start + 11
                                    ]
                                },
                            }
                        ]
                    }
                )
        for start in range(0, len(text), 9):
            chunks.append(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant" if start == 0 else None,
                                "content": text[start : start + 9],
                            },
                        }
                    ]
                }
            )
        if step.get("kind") == "delay":
            time.sleep(float(step.get("seconds") or 0.2))
    chunks.append(
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 6}}
    )
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines)


class Handler(BaseHTTPRequestHandler):
    state: MockState = None
    counter = 0

    def log_message(self, *args):  # keep test output quiet
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        with self.state.lock:
            self.state.count += 1
            index = self.state.count
        self.state.record(body)

        model = body.get("model") or ""
        steps = self.state.steps_for(model)
        step = steps[(index - 1) % max(1, len(steps))]
        if step.get("kind") == "error":
            payload = {
                "error": {"message": "mock upstream failure", "type": "server_error"}
            }
            data = json.dumps(payload).encode()
            self.send_response(int(step.get("status") or 500))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if body.get("stream"):
            data = _stream(model, step, index).encode()
            if step.get("kind") == "fold":
                data = data[: data.index(b"data: [DONE]")]  # cut the stream short
        else:
            data = json.dumps(_payload(model, step, index)).encode()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/event-stream" if body.get("stream") else "application/json",
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class MockProvider:
    """In-process mock endpoint. Use as a context manager."""

    def __init__(self, script=None, record=None):
        self.script_path = script
        self.record_path = record
        self.server = None
        self.thread = None
        self.state = None

    def __enter__(self):
        self.state = MockState(self.script_path, self.record_path)
        handler = type("BoundHandler", (Handler,), {"state": self.state})
        self.server = ThreadingHTTPServer((HOST, 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        return False

    @property
    def base_url(self):
        return f"http://{HOST}:{self.port}/v1"


if __name__ == "__main__":
    import sys

    with MockProvider(
        sys.argv[1] if len(sys.argv) > 1 else None,
        sys.argv[2] if len(sys.argv) > 2 else None,
    ) as mock:
        print(mock.base_url, flush=True)
        threading.Event().wait()
