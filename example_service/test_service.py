import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from service import Handler


class ServiceTests(unittest.TestCase):
    def test_serves_the_packaged_release(self):
        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        with urlopen(f"http://127.0.0.1:{server.server_port}/version", timeout=3) as response:
            self.assertEqual(json.load(response), {"version": Path("version.txt").read_text().strip()})
