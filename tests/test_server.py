"""End-to-end checks against an actual local MCP server process."""

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


ROOT = Path(__file__).resolve().parents[1]
TEST_TOKEN = "test-token-for-local-integration-check-only"
ADDITIONAL_TEST_TOKEN = "second-token-for-local-integration-check-only"


class ServerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        token_file = Path(cls.directory.name) / "token"
        token_file.write_text(TEST_TOKEN, encoding="utf-8")
        token_file.chmod(0o600)
        extra_token_file = Path(cls.directory.name) / "mcp-token-account2"
        extra_token_file.write_text(ADDITIONAL_TEST_TOKEN, encoding="utf-8")
        extra_token_file.chmod(0o600)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        env = os.environ.copy()
        env.update(
            VOOY_MCP_HOST="127.0.0.1",
            VOOY_MCP_PORT=str(cls.port),
            VOOY_MCP_TOKEN_FILE=str(token_file),
            VOOY_MCP_EXTRA_TOKEN_DIR=cls.directory.name,
        )
        cls.process = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.addClassCleanup(cls.stop_server)
        for _ in range(100):
            if cls.process.poll() is not None:
                raise AssertionError("MCP server exited before it could start")
            try:
                with urlopen(f"{cls.base_url}/healthz", timeout=0.2) as response:
                    if response.status == 200:
                        return
            except (URLError, TimeoutError):
                time.sleep(0.1)
        raise AssertionError("MCP server did not start")

    @classmethod
    def stop_server(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)

    def test_missing_token_file_prevents_server_startup(self):
        env = os.environ.copy()
        env["VOOY_MCP_TOKEN_FILE"] = str(Path(self.directory.name) / "missing")
        result = subprocess.run(
            [sys.executable, str(ROOT / "server.py")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("token", result.stderr.lower())

    def test_mcp_requires_a_valid_bearer_token(self):
        for authorization in (None, "Bearer invalid-token"):
            headers = {"Authorization": authorization} if authorization else {}
            with self.subTest(authorization=authorization):
                with self.assertRaises(HTTPError) as result:
                    urlopen(Request(f"{self.base_url}/mcp", headers=headers))
                self.assertEqual(result.exception.code, 401)

    def test_valid_token_cannot_bypass_origin_validation(self):
        request = Request(
            f"{self.base_url}/mcp",
            headers={
                "Authorization": f"Bearer {TEST_TOKEN}",
                "Origin": "https://untrusted.example",
            },
        )
        with self.assertRaises(HTTPError) as result:
            urlopen(request)
        self.assertEqual(result.exception.code, 403)

    def test_authenticated_client_can_discover_and_call_test_tool(self):
        async def run():
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {TEST_TOKEN}"},
                trust_env=False,
            ) as http_client:
                async with streamable_http_client(
                    f"{self.base_url}/mcp", http_client=http_client
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        info = await session.initialize()
                        self.assertEqual(info.server_info.name, 'vooy-mcp')
                        self.assertEqual(info.server_info.title, 'Vooy 미디어 저장소')
                        self.assertIn('연결', info.server_info.description)
                        self.assertIn('ping', info.instructions)
                        tools = await session.list_tools()
                        self.assertIn("ping", [tool.name for tool in tools.tools])
                        self.assertEqual(next(tool.title for tool in tools.tools if tool.name == 'ping'),
                                         'MCP 연결 확인')
                        result = await session.call_tool(
                            "ping", {"message": "안녕하세요"}
                        )
                        self.assertFalse(result.is_error)
                        self.assertIn("안녕하세요", result.content[0].text)

        asyncio.run(run())

    def test_additional_token_can_initialize_mcp_session(self):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "integration-test", "version": "1"},
                },
            }
        ).encode()
        request = Request(
            f"{self.base_url}/mcp",
            data=body,
            headers={
                "Authorization": f"Bearer {ADDITIONAL_TEST_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urlopen(request) as response:
                result = json.load(response)
        except HTTPError as error:
            self.fail(f"Additional token rejected with HTTP {error.code}")
        self.assertIn("serverInfo", result["result"])


if __name__ == "__main__":
    unittest.main()
