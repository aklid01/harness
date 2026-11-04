"""The sidecar's one-time token: no ready frame, no dispatch, no reply without it.

A browser page can open a websocket to 127.0.0.1; the shell's token is the
only thing that separates the app from everything else on the machine.
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import paths
from harness import server as server_mod

PORT = 8977


def start_server(token):
    from harness.server import get_backend

    get_backend()
    return asyncio.ensure_future(server_mod.serve("127.0.0.1", PORT, auth_token=token))


class AuthCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        paths.init(
            project_root=Path(self.tmp.name) / "proj",
            app_data_dir=Path(self.tmp.name) / "app",
        )
        self.addCleanup(paths.reset)
        self.token = "t" * 32

    def test(self):
        async def scenario():
            import websockets

            task = start_server(self.token)
            try:
                await asyncio.sleep(0.5)

                async with websockets.connect(f"ws://127.0.0.1:{PORT}") as ws:
                    with self.assertRaises(
                        (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed)
                    ):
                        # No auth frame: the sidecar stays silent and closes.
                        await asyncio.wait_for(ws.recv(), timeout=1.5)

                async with websockets.connect(f"ws://127.0.0.1:{PORT}") as ws:
                    await ws.send(json.dumps({"auth": "x" * 32}))
                    with self.assertRaises(
                        (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed)
                    ):
                        await asyncio.wait_for(ws.recv(), timeout=1.5)

                async with websockets.connect(f"ws://127.0.0.1:{PORT}") as ws:
                    await ws.send(json.dumps({"auth": self.token}))
                    frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                self.assertIn("ready", frame)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
