import http.client
import threading
import unittest
from http.server import ThreadingHTTPServer
from tempfile import TemporaryDirectory
from pathlib import Path

from weather99.web import handler_for


class DashboardHTTPTests(unittest.TestCase):
    def test_page_and_settlement_module_are_served_with_dashboard(self):
        with TemporaryDirectory() as folder:
            with ThreadingHTTPServer(("127.0.0.1", 0), handler_for(Path(folder))) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    for route, expected in (("/", b"settlement-summary"),
                                            ("/settlement.js", b"renderSettlement"),
                                            ("/api/dashboard", b"weather99.dashboard.v1")):
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                        connection.request("GET", route)
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200, route)
                        self.assertIn(expected, response.read())
                        connection.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=3)
