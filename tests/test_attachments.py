"""End-to-end: files the user attaches to a message.

The test does not patch call_llm. It runs the real Harness backend, the real
streaming model call, and real session persistence against an in-process HTTP
endpoint, so it proves on the wire what unit tests cannot:

* an attached file is staged under the app data area, never in the workspace,
  and the user's own copy is left untouched;
* the manifest reaches the model in the same turn as the message that carries
  it, and the agent reads the file through the path the manifest names;
* a PDF and a Word document come back as text, so the agent can answer from
  them without any other input;
* an oversize file is refused by name, with the limit, and the rest of the
  message still goes;
* two files with the same name both survive;
* a file that cannot be staged is named in the manifest, and the message and
  the other attachments still arrive;
* the transcript keeps the attachment metadata and none of the bytes;
* deleting the session takes the staged files with it.

Writes tests/e2e-attachments-report.json so the run is checkable after the
fact, the same way the other end-to-end tests write theirs.
"""

import base64
import json
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import attachments as att
from harness import events, providers, server
from harness import paths as paths_mod
from tests.mock_provider import _payload, _stream

TITLE_MARKER = "Title this coding task"


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, user, secret):
        self.store[(service, user)] = secret

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


def make_pdf(text="Hello attachment world"):
    """A one-page PDF with a line of text, built by hand."""
    stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n"
        "%%EOF\n"
    ).encode()
    return bytes(out)


def make_docx(text="Word document body"):
    """A Word file with one paragraph - enough for the extractor."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml'
        '/2006/main"><w:body><w:p><w:r><w:t>'
        f"{text}"
        "</w:t></w:r></w:p></w:body></w:document>"
    )
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


class AttachHandler(BaseHTTPRequestHandler):
    """A model that reads the attachments the manifest points at.

    It takes the paths out of the manifest in the request it was given, so the
    test proves the manifest really is how the agent finds the files. One file
    per turn, then an answer that quotes everything it read.
    """

    state = None

    def log_message(self, *args):
        pass

    def manifest_paths(self, messages):
        """Every file path the manifest names."""
        text = ""
        for message in reversed(messages):
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                text = message["content"]
                break
        paths = []
        for line in text.splitlines():
            if line.startswith("- ") and " - " in line:
                candidate = line.rsplit(" - ", 1)[-1].strip()
                if candidate and candidate != "(no path)":
                    paths.append(candidate)
        return paths

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        model = body.get("model") or ""
        messages = body.get("messages") or []
        if any(
            TITLE_MARKER in str(m.get("content") or "") for m in messages
        ):
            self.answer(model, body, "ok")
            return

        paths = self.manifest_paths(messages)
        with self.state.lock:
            self.state.requests.append(body)
            self.state.count += 1
            index = self.state.count

        # A probe step, when a test asks for one: the model looks at the file
        # with a shell command before it reads it.
        probe = self.state.probe
        file_index = index - 1 - (1 if probe else 0)
        if probe and index == 1:
            step = {
                "kind": "tool",
                "name": "bash",
                "arguments": {"command": probe(paths[0])},
            }
        elif file_index < len(paths):
            step = {
                "kind": "tool",
                "name": "read_file",
                "arguments": {"path": paths[file_index]},
            }
        else:
            seen = []
            for message in messages:
                if message.get("role") == "tool":
                    seen.append(str(message.get("content") or ""))
            step = {"kind": "text", "content": "FILE SAID: " + " | ".join(seen)}

        if body.get("stream"):
            data = _stream(model, step, index).encode()
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

    def answer(self, model, body, text):
        data = json.dumps(_payload(model, {"kind": "text", "content": text}, 1)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class AttachState:
    def __init__(self):
        self.requests = []
        self.count = 0
        self.lock = threading.Lock()
        self.probe = None  # optional: a shell probe of the file, before the read


class AttachE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The report is this run's, not the last one's.
        (Path(__file__).resolve().parent / "e2e-attachments-report.json").unlink(
            missing_ok=True
        )

    def setUp(self):
        events.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir(parents=True)
        self.appdata = Path(self.tmp.name) / "appdata"
        self.appdata.mkdir(parents=True)
        paths_mod.init(project_root=self.root, app_data_dir=self.appdata)
        self.addCleanup(paths_mod.reset)
        os.environ["HARNESS_SESSIONS_ROOT"] = str(Path(self.tmp.name) / "sessions")
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))
        mock.patch.dict("sys.modules", {"keyring": FakeKeyring()}).start()
        self.addCleanup(mock.patch.stopall)

        self.backend = server.Harness()
        self.sent = []
        self.backend.attach(self.sent.append)
        self.addCleanup(self.quiesce)
        self.state = AttachState()
        self.boot()

    def quiesce(self):
        for s in list(self.backend.sessions.values()):
            s.join_background(timeout=10)

    def boot(self):
        handler = type("Bound", (AttachHandler,), {"state": self.state})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        providers.save_group({"id": "grp", "name": "One", "base_url": base})
        providers.save_model(
            {"id": "mdl-at", "name": "AT", "model": "at-model"}, "grp"
        )
        providers.set_active("mdl-at")
        providers.set_key("grp", "sk-attach-test")

    # ---- helpers ----

    def wait_until(self, cond, timeout=20):
        import time

        limit = time.time() + timeout
        while time.time() < limit:
            if cond():
                return True
            time.sleep(0.01)
        return False

    def count(self, kind, session="s"):
        return sum(
            1
            for m in self.sent
            if m.get("event") == kind and m.get("session") == session
        )

    def done(self, session="s", timeout=30):
        return self.wait_until(lambda: self.count("done", session) >= 1, timeout)

    def dispatch(self, obj):
        obj.setdefault("session", "s")
        obj.setdefault("root", str(self.root))
        obj["req_id"] = 1
        self.backend.dispatch(obj)

    def replies(self, op):
        return [m for m in self.sent if m.get("reply") == op]

    def attach(self, name, data, kind="application/octet-stream"):
        """Stage one file through the upload ops, and return its metadata."""
        self.dispatch(
            {
                "op": "attach_begin",
                "name": name,
                "type": kind,
                "size": len(data),
            }
        )
        begin = self.replies("attach_begin")[-1]
        self.assertTrue(begin.get("ok"), begin.get("error"))
        ticket = begin["data"]["id"]
        for start in range(0, max(len(data), 1), 64 * 1024):
            piece = data[start : start + 64 * 1024]
            self.dispatch(
                {
                    "op": "attach_part",
                    "id": ticket,
                    "data": base64.b64encode(piece).decode("ascii"),
                }
            )
            self.assertTrue(self.replies("attach_part")[-1].get("ok"))
        self.dispatch({"op": "attach_finish", "id": ticket})
        done = self.replies("attach_finish")[-1]
        self.assertTrue(done.get("ok"), done.get("error"))
        return done["data"]["attachment"]

    def open_and_submit(self, text, attachments=None, mode="build"):
        self.dispatch({"op": "open", "mode": mode})
        self.assertTrue(
            self.wait_until(
                lambda: any(
                    m.get("event") == events.SESSION and m.get("session") == "s"
                    for m in self.sent
                )
            )
        )
        self.sent.clear()
        self.dispatch(
            {
                "op": "submit",
                "text": text,
                "mode": mode,
                "attachments": attachments or [],
            }
        )

    def flow_requests(self):
        return [
            r
            for r in self.state.requests
            if not any(
                TITLE_MARKER in str(m.get("content") or "") for m in r["messages"]
            )
        ]

    def session_id(self):
        for m in self.sent:
            if m.get("event") == events.SESSION and m["payload"].get("id"):
                return m["payload"]["id"]
        return None

    def transcript(self):
        sid = self.session_id()
        messages = []
        path = paths_mod.session_dir() / f"{sid}.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "rewind_to" in entry:
                del messages[entry["rewind_to"] :]
            elif "compacted" in entry:
                messages = list(entry["compacted"])
            else:
                messages.append(entry)
        return messages

    def report(self, payload):
        """Collect the run's findings into one report, like the other suites."""
        path = Path(__file__).resolve().parent / "e2e-attachments-report.json"
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        data.update(payload)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def user_message(self):
        """The last user message on the transcript, with its attachments."""
        users = [m for m in self.transcript() if m.get("role") == "user"]
        return users[-1] if users else {}

    def assistant_text(self):
        """The agent's answer, as it landed on the transcript."""
        answers = [
            str(m.get("content") or "")
            for m in self.transcript()
            if m.get("role") == "assistant" and m.get("content")
        ]
        return answers[-1] if answers else ""

    # ---- scenarios ----

    def test_attached_file_is_staged_read_and_delivered_in_the_same_turn(self):
        source = Path(self.tmp.name) / "outside.md"
        source.write_text("the notes the user attached\n", encoding="utf-8")
        before = source.read_bytes()

        meta = self.attach("outside.md", before, "text/markdown")
        self.assertTrue(Path(meta["path"]).exists())
        self.assertNotIn(
            str(self.root),
            str(Path(meta["path"])),
            "the staged copy must not sit in the workspace",
        )
        self.assertEqual(
            source.read_bytes(),
            before,
            "the user's own file must not be touched",
        )

        self.open_and_submit("summarize this", attachments=[meta])
        self.assertTrue(self.done())

        request = self.flow_requests()[0]
        sent = [m for m in request["messages"] if m.get("role") == "user"]
        manifest = "\n".join(str(m.get("content") or "") for m in sent)
        self.assertIn("summarize this", manifest)
        self.assertIn("outside.md", manifest)
        self.assertIn("untrusted data", manifest)
        # The file moved into the session's own directory when it was sent, so
        # the manifest names where it lives now.
        staged = self.user_message()["attachments"][0]["path"]
        self.assertIn(staged, manifest)
        self.assertNotIn(meta["path"], manifest)
        self.assertTrue(Path(staged).exists())
        self.assertIn("2026", staged)

        self.assertIn("the notes the user attached", self.assistant_text())

        self.report(
            {
                "staged_outside_the_workspace": True,
                "source_untouched": True,
                "manifest_in_first_request": True,
                "agent_read_the_file": "the notes the user attached"
                in self.assistant_text(),
            }
        )

    def test_ask_mode_probes_an_attachment_outside_the_workspace(self):
        """Ask mode can look at a file the user attached.

        The attachment is staged outside the workspace, and the probe is the
        shape an agent reaches for: a shell variable and a merged stderr. Ask
        mode refuses every bash command the permission layer does not rate as
        read-only, and it refuses it before execution - so a probe read as a
        mutation never reaches the file.
        """
        from harness import sandbox

        meta = self.attach(
            "attached.html", b"<html><body>attached body</body></html>", "text/html"
        )

        def probe(path):
            return (
                f"P1='{path}'; P2='{path}'; "
                'ls -la "$P1" 2>&1; echo \'---\'; '
                'head -c 6 "$P2" 2>/dev/null || head -c 6 "$P1" 2>&1'
            )

        self.state.probe = probe
        self.open_and_submit("what is in this", attachments=[meta], mode="ask")
        self.assertTrue(self.done())

        staged = None
        for message in self.transcript():
            if message.get("role") != "user":
                continue
            for attachment in message.get("attachments") or []:
                staged = Path(attachment["path"])
        self.assertIsNotNone(staged, "the transcript kept the attachment metadata")
        self.assertFalse(
            str(staged).startswith(str(self.root)),
            "the attachment is staged outside the workspace",
        )

        results = [
            (str(m["payload"].get("name") or ""), str(m["payload"].get("result") or ""))
            for m in self.sent
            if m.get("event") == events.TOOL_RESULT
        ]
        probes = [result for name, result in results if name == "bash"]
        self.assertTrue(probes, f"the probe never ran: {results}")
        for result in probes:
            self.assertNotIn("Ask mode", result, result)
            self.assertNotIn("read-only", result, result)
        if sandbox._find_bash() is not None:
            self.assertIn(
                "attached.html",
                probes[0],
                f"the probe did not read the attachment: {probes[0]}",
            )

        # The file itself was read too, and its bytes reached the answer.
        answer = self.assistant_text()
        self.assertIn("attached body", answer)

        self.report(
            {
                "ask_mode_probe_outside_workspace_ran": True,
                "ask_mode_probe_saw_the_attachment": (
                    sandbox._find_bash() is None or "attached.html" in probes[0]
                ),
            }
        )

    def test_pdf_and_word_documents_come_back_as_text(self):
        pdf = self.attach("report.pdf", make_pdf("Revenue grew by nine percent"))
        docx = self.attach("notes.docx", make_docx("The quarterly plan is fixed"))

        self.open_and_submit("compare these two", attachments=[pdf, docx])
        self.assertTrue(self.done())

        answer = self.assistant_text()
        self.assertIn("Revenue grew by nine percent", answer)
        self.assertIn("The quarterly plan is fixed", answer)

        self.report(
            {
                "pdf_text_read": "Revenue grew by nine percent" in answer,
                "docx_text_read": "The quarterly plan is fixed" in answer,
            }
        )

    def test_an_unusual_file_type_is_accepted_and_delivered(self):
        blob = bytes(range(256)) * 4
        meta = self.attach("weird.qqq", blob, "application/x-unknown")

        self.open_and_submit("what is in this", attachments=[meta])
        self.assertTrue(self.done())

        request = self.flow_requests()[0]
        sent = "\n".join(
            str(m.get("content") or "")
            for m in request["messages"]
            if m.get("role") == "user"
        )
        self.assertIn("weird.qqq", sent)
        staged = self.user_message()["attachments"][0]["path"]
        self.assertIn(staged, sent)
        # A binary file has no text, and the agent is told so rather than
        # being handed mojibake.
        self.assertIn("binary file", self.assistant_text())

    def test_an_oversize_file_is_refused_by_name_and_the_message_still_goes(self):
        good = self.attach("small.md", b"tiny\n", "text/markdown")
        with mock.patch.object(att, "MAX_FILE_BYTES", 16):
            self.dispatch(
                {
                    "op": "attach_begin",
                    "name": "huge.bin",
                    "type": "application/octet-stream",
                    "size": 1024 * 1024,
                }
            )
        reply = self.replies("attach_begin")[-1]
        self.assertFalse(reply.get("ok"))
        self.assertIn("huge.bin", str(reply.get("error")))
        self.assertIn("16", str(reply.get("error")))

        self.open_and_submit("carry on", attachments=[good])
        self.assertTrue(self.done())
        request = self.flow_requests()[0]
        sent = "\n".join(
            str(m.get("content") or "")
            for m in request["messages"]
            if m.get("role") == "user"
        )
        self.assertIn("carry on", sent)
        self.assertIn("small.md", sent)
        self.report(
            {
                "oversize_file_refused_by_name": True,
                "message_still_sent_after_a_refused_file": True,
            }
        )

    def test_too_many_files_in_one_message_are_named_and_dropped(self):
        metas = [
            self.attach(f"file{index}.md", f"number {index}\n".encode(), "text/markdown")
            for index in range(4)
        ]
        with mock.patch.object(att, "MAX_FILES_PER_MESSAGE", 2):
            self.open_and_submit("too many", attachments=metas)
            self.assertTrue(self.done())
            notes = [
                str(m["payload"].get("text") or "")
                for m in self.sent
                if m.get("event") == events.NOTE
            ]
            self.assertTrue(any("file2.md" in note for note in notes), notes)
            self.assertTrue(any("file3.md" in note for note in notes), notes)

        request = self.flow_requests()[0]
        sent = "\n".join(
            str(m.get("content") or "")
            for m in request["messages"]
            if m.get("role") == "user"
        )
        self.assertIn("file0.md", sent)
        self.assertIn("file1.md", sent)
        self.assertNotIn("file3.md", sent)
        self.report(
            {
                "too_many_files_named_and_dropped": True,
                "the_rest_of_the_message_still_goes": True,
            }
        )

    def test_two_files_with_the_same_name_both_survive(self):
        first = self.attach("same.md", b"first copy\n", "text/markdown")
        second = self.attach("same.md", b"second copy\n", "text/markdown")
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(Path(first["path"]).exists())
        self.assertTrue(Path(second["path"]).exists())
        self.assertNotEqual(
            Path(first["path"]).read_bytes(), Path(second["path"]).read_bytes()
        )

        self.open_and_submit("read both", attachments=[first, second])
        self.assertTrue(self.done())
        kept = self.user_message()["attachments"]
        self.assertEqual(len(kept), 2)
        self.assertNotEqual(kept[0]["path"], kept[1]["path"])
        self.assertNotEqual(
            kept[0]["name"],
            kept[1]["name"],
            "two files with one name are told apart by name, not by accident",
        )
        self.assertEqual(Path(kept[0]["path"]).read_text(encoding="utf-8"), "first copy\n")
        self.assertEqual(Path(kept[1]["path"]).read_text(encoding="utf-8"), "second copy\n")

        request = self.flow_requests()[0]
        sent = "\n".join(
            str(m.get("content") or "")
            for m in request["messages"]
            if m.get("role") == "user"
        )
        self.assertIn(kept[0]["path"], sent)
        self.assertIn(kept[1]["path"], sent)
        self.report({"two_files_with_one_name_both_survive": True})

    def test_a_file_that_cannot_be_staged_is_named_and_the_rest_still_arrive(self):
        good = self.attach("here.md", b"still here\n", "text/markdown")
        broken = {
            "id": "att-gone",
            "name": "vanished.bin",
            "type": "application/octet-stream",
            "size": 10,
            "path": str(Path(self.tmp.name) / "not-there.bin"),
        }
        self.open_and_submit("read what you can", attachments=[broken, good])
        self.assertTrue(self.done())

        request = self.flow_requests()[0]
        sent = "\n".join(
            str(m.get("content") or "")
            for m in request["messages"]
            if m.get("role") == "user"
        )
        self.assertIn("vanished.bin", sent)
        self.assertIn("NOT AVAILABLE", sent)
        self.assertIn("here.md", sent)
        self.report(
            {
                "a_failed_file_is_named_in_the_manifest": True,
                "the_other_attachments_still_arrive": True,
            }
        )

    def test_the_transcript_keeps_metadata_and_not_the_bytes(self):
        secret = b"THE SECRET BYTES\n"
        meta = self.attach("secret.md", secret, "text/markdown")
        self.open_and_submit("keep this", attachments=[meta])
        self.assertTrue(self.done())

        user = self.user_message()
        self.assertEqual(user["attachments"][0]["name"], "secret.md")
        self.assertEqual(user["attachments"][0]["size"], len(secret))
        self.assertIn("path", user["attachments"][0])
        # The attachment metadata is metadata: it names the file, it does not
        # carry the bytes. (The tool result further down does carry them -
        # the agent read the file, and that is its own doing.)
        self.assertNotIn(
            "THE SECRET BYTES",
            json.dumps(user["attachments"]),
            "the attachment record must not carry the file's contents",
        )
        self.report({"transcript_keeps_metadata_not_bytes": True})

    def test_deleting_the_session_takes_the_staged_files_with_it(self):
        meta = self.attach("gone.md", b"bye\n", "text/markdown")
        self.open_and_submit("keep it for now", attachments=[meta])
        self.assertTrue(self.done())
        staged = Path(self.user_message()["attachments"][0]["path"])
        self.assertTrue(staged.exists())

        self.dispatch({"op": "delete_session", "id": self.session_id()})
        self.assertFalse(staged.exists(), "the session's files go with it")
        self.report({"deleting_a_session_deletes_its_files": True})

    def test_a_steer_carries_its_attachment_to_the_next_model_call(self):
        meta = self.attach("steered.md", b"steer notes\n", "text/markdown")
        self.open_and_submit("start the work")
        self.assertTrue(self.wait_until(lambda: self.count(events.STATUS) >= 1))
        self.dispatch(
            {
                "op": "enqueue",
                "text": "also read this",
                "kind": "steer",
                "attachments": [meta],
            }
        )
        self.assertTrue(self.done())

        requests = self.flow_requests()
        self.assertGreaterEqual(len(requests), 2, "the steer made a second request")
        second = "\n".join(
            str(m.get("content") or "")
            for m in requests[1]["messages"]
            if m.get("role") == "user"
        )
        self.assertIn("also read this", second)
        self.assertIn("steered.md", second)
        self.report({"a_steer_carries_its_attachment_to_the_next_call": True})

    def test_a_pending_message_keeps_its_file_and_can_give_it_back(self):
        meta = self.attach("pending.md", b"pending notes\n", "text/markdown")
        self.open_and_submit("start the work")
        self.assertTrue(self.wait_until(lambda: self.count(events.STATUS) >= 1))
        self.dispatch(
            {
                "op": "enqueue",
                "text": "also read this",
                "kind": "steer",
                "attachments": [meta],
            }
        )
        pending = await_pending = None
        for _ in range(100):
            events_seen = [
                m
                for m in self.sent
                if m.get("event") == events.PENDING
                and m.get("payload", {}).get("items")
            ]
            if events_seen:
                await_pending = events_seen[-1]["payload"]["items"]
                pending = await_pending
                break
            import time

            time.sleep(0.02)
        self.assertIsNotNone(pending, "the pending item never showed up")
        self.assertEqual(len(pending[0].get("attachments") or []), 1)
        staged = Path(pending[0]["attachments"][0]["path"])
        self.assertTrue(staged.exists())

        # The text can still be edited, and the file stays with it.
        self.dispatch(
            {"op": "pending_edit", "id": pending[0]["id"], "text": "also read this later"}
        )
        self.dispatch(
            {
                "op": "pending_remove_attachment",
                "id": pending[0]["id"],
                "attachment_id": pending[0]["attachments"][0]["id"],
            }
        )
        after = None
        for _ in range(100):
            seen = [
                m
                for m in self.sent
                if m.get("event") == events.PENDING
                and m.get("payload", {}).get("items")
            ]
            if seen:
                after = seen[-1]["payload"]["items"]
                if not after[0].get("attachments"):
                    break
            import time

            time.sleep(0.02)
        self.assertEqual(after[0].get("attachments"), [])
        self.assertEqual(after[0]["text"], "also read this later")
        self.assertFalse(staged.exists(), "the taken-back file goes away")

    def attached_user_message(self, request):
        """The user message that carries the manifest.

        The request ends with an injected reminder, which is a user message
        too, so the one to look at is the one that names the files.
        """
        found = [
            m
            for m in request["messages"]
            if m.get("role") == "user" and "<attachments>" in str(m.get("content"))
        ]
        self.assertTrue(found, "no message carried the manifest")
        return found[0]

    def test_an_image_reaches_the_model_as_an_image(self):
        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
            + b"\x1f\x15\xc4\x89"
        )
        meta = self.attach("pic.png", png, "image/png")
        self.open_and_submit("what is in this picture", attachments=[meta])
        self.assertTrue(self.done())

        request = self.flow_requests()[0]
        user = self.attached_user_message(request)
        content = user["content"]
        self.assertIsInstance(content, list, "the image rides as a message part")
        images = [p for p in content if p.get("type") == "image_url"]
        self.assertEqual(len(images), 1)
        self.assertTrue(
            images[0]["image_url"]["url"].startswith("data:image/png;base64,")
        )

    def test_a_model_that_cannot_see_images_is_told_so(self):
        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
            + b"\x1f\x15\xc4\x89"
        )
        meta = self.attach("pic.png", png, "image/png")
        caps = providers.active_model_capabilities()
        caps["supports_images"] = False
        with mock.patch.object(
            providers, "active_model_capabilities", return_value=caps
        ):
            self.open_and_submit("what is in this picture", attachments=[meta])
            self.assertTrue(self.done())

        request = self.flow_requests()[0]
        user = self.attached_user_message(request)
        self.assertIsInstance(user["content"], str)
        self.assertIn("cannot see images", user["content"])
        self.assertIn("pic.png", user["content"])

    def test_the_system_prompt_says_attached_files_are_data(self):
        from harness import llm

        for mode in ("build", "plan", "ask"):
            prompt = llm.system_prompt(mode)
            self.assertIn("## Attachments", prompt)
            self.assertIn("untrusted data", prompt)
            self.assertIn("never instructions", prompt)


class AttachmentUnit(unittest.TestCase):
    """The pieces the end-to-end test leans on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        paths_mod.init(project_root=self.base, app_data_dir=self.base / "app")
        self.addCleanup(paths_mod.reset)
        os.environ["HARNESS_SESSIONS_ROOT"] = str(self.base / "sessions")
        self.addCleanup(lambda: os.environ.pop("HARNESS_SESSIONS_ROOT", None))

    def test_names_with_spaces_and_unicode_survive(self):
        for name in ("my notes.md", "café résumé.txt", "文件 名.md", "a-b_c.1.json"):
            self.assertEqual(att.safe_name(name), name)

    def test_a_windows_illegal_name_becomes_usable(self):
        cleaned = att.safe_name('bad:name?.txt')
        self.assertNotIn(":", cleaned)
        self.assertNotIn("?", cleaned)
        self.assertTrue(cleaned.endswith(".txt"))

    def test_a_reserved_device_name_is_escaped(self):
        self.assertNotEqual(att.safe_name("CON.txt").upper(), "CON.TXT")
        self.assertNotEqual(att.safe_name("nul").upper(), "NUL")

    def test_two_files_with_one_name_do_not_overwrite(self):
        directory = self.base / "dupes"
        directory.mkdir()
        first = att.unique_path(directory, "same.md")
        first.write_text("one", encoding="utf-8")
        second = att.unique_path(directory, "same.md")
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_text(encoding="utf-8"), "one")

    def test_two_uploads_of_one_name_get_different_paths(self):
        # Two files picked at the same time must not land on one path: the
        # second upload learns the name is taken when it looks.
        first = att.begin("client", "same.md", 4, "text/markdown")
        second = att.begin("client", "same.md", 4, "text/markdown")
        self.assertNotEqual(first["path"], second["path"])
        att.part(first["id"], base64.b64encode(b"one\n").decode())
        att.part(second["id"], base64.b64encode(b"two\n").decode())
        one = att.finish(first["id"])
        two = att.finish(second["id"])
        self.assertEqual(Path(one["path"]).read_text(encoding="utf-8"), "one\n")
        self.assertEqual(Path(two["path"]).read_text(encoding="utf-8"), "two\n")

    def test_adopt_moves_the_file_into_the_session_directory(self):
        record = att.begin("client", "notes.md", 5, "text/markdown")
        att.part(record["id"], base64.b64encode(b"hello").decode())
        meta = att.finish(record["id"])
        self.assertTrue(Path(meta["path"]).exists())

        adopted = att.adopt([meta], "20260101-000000", self.base)
        self.assertEqual(len(adopted), 1)
        moved = Path(adopted[0]["path"])
        self.assertTrue(moved.exists())
        self.assertFalse(Path(meta["path"]).exists())
        self.assertEqual(moved.read_text(encoding="utf-8"), "hello")
        self.assertIn("20260101-000000", str(moved))

    def test_adopting_the_same_file_twice_leaves_it_where_it_is(self):
        record = att.begin("client", "notes.md", 5, "text/markdown")
        att.part(record["id"], base64.b64encode(b"hello").decode())
        meta = att.finish(record["id"])
        first = att.adopt([meta], "20260101-000000", self.base)[0]
        second = att.adopt([first], "20260101-000000", self.base)[0]
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(Path(second["path"]).read_text(encoding="utf-8"), "hello")

    def test_a_missing_file_is_reported_not_dropped(self):
        missing = {
            "id": "att-x",
            "name": "gone.bin",
            "type": "application/octet-stream",
            "size": 3,
            "path": str(self.base / "nope.bin"),
        }
        adopted = att.adopt([missing], "20260101-000000", self.base)
        self.assertEqual(len(adopted), 1)
        self.assertTrue(adopted[0]["error"])
        self.assertIn("gone.bin", adopted[0]["error"])

    def test_the_manifest_names_the_file_and_its_path(self):
        path = self.base / "report.pdf"
        path.write_bytes(make_pdf("a report"))
        block = att.manifest(
            [
                {
                    "id": "a1",
                    "name": "report.pdf",
                    "type": "application/pdf",
                    "size": 2048,
                    "path": str(path),
                }
            ]
        )
        self.assertIn("report.pdf", block)
        self.assertIn("application/pdf", block)
        self.assertIn(str(path), block)
        self.assertIn("untrusted data", block)

    def test_the_manifest_says_when_the_file_is_gone(self):
        block = att.manifest(
            [
                {
                    "id": "a1",
                    "name": "gone.pdf",
                    "type": "application/pdf",
                    "size": 10,
                    "path": str(self.base / "gone.pdf"),
                }
            ]
        )
        self.assertIn("NOT AVAILABLE", block)
        self.assertIn("no longer on disk", block)

    def test_reading_a_pdf_gives_its_text(self):
        path = self.base / "doc.pdf"
        path.write_bytes(make_pdf("Extracted from the PDF"))
        text, reason = att.readable_text(str(path))
        self.assertIsNone(reason)
        self.assertIn("Extracted from the PDF", text)

    def test_reading_a_word_document_gives_its_text(self):
        path = self.base / "doc.docx"
        path.write_bytes(make_docx("Body of the word file"))
        text, reason = att.readable_text(str(path))
        self.assertIsNone(reason)
        self.assertIn("Body of the word file", text)

    def test_a_binary_file_says_it_has_no_text(self):
        path = self.base / "blob.bin"
        path.write_bytes(bytes(range(256)))
        text, reason = att.readable_text(str(path))
        self.assertIsNone(text)
        self.assertIn("binary", reason)

    def test_an_empty_file_says_it_is_empty(self):
        path = self.base / "empty.txt"
        path.write_bytes(b"")
        text, reason = att.readable_text(str(path))
        self.assertIsNone(text)
        self.assertIn("empty", reason)

    def test_a_pdf_with_no_text_says_so(self):
        path = self.base / "scan.pdf"
        path.write_bytes(make_pdf(""))
        text, reason = att.readable_text(str(path))
        self.assertIsNone(text)
        self.assertIn("no extractable text", reason)

    def test_a_broken_pdf_reports_why(self):
        path = self.base / "broken.pdf"
        path.write_bytes(b"%PDF-1.4 this is not a pdf")
        text, reason = att.readable_text(str(path))
        self.assertIsNone(text)
        self.assertTrue(reason)

    def test_an_image_becomes_a_data_url(self):
        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
            + b"\x1f\x15\xc4\x89"
        )
        path = self.base / "pic.png"
        path.write_bytes(png)
        url = att.image_data_url(str(path))
        self.assertIsNotNone(url)
        self.assertTrue(url.startswith("data:image/png;base64,"))

    def test_a_text_file_is_not_mistaken_for_binary(self):
        path = self.base / "plain.md"
        path.write_text("# heading\n\nsome text\n", encoding="utf-8")
        text, reason = att.readable_text(str(path))
        self.assertIsNone(reason)
        self.assertIn("# heading", text)


if __name__ == "__main__":
    unittest.main()
