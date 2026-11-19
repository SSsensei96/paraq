"""A stand-in for the Ollama server, implementing the four API calls Paraq uses.

/api/chat "translates" by prefixing every input line with "[EN] ", so sentence
alignment can be checked. Text containing "MERGE" comes back as one line, to
exercise the fallback that translates sentence by sentence.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOllama:
    def __init__(self):
        self.pulled: set[str] = set()
        self.prompts: list[str] = []
        self.flaked = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/version":
                    self._json({"version": "0.0-fake"})
                elif self.path == "/api/tags":
                    self._json({"models": [{"name": t, "model": t} for t in sorted(outer.pulled)]})
                else:
                    self.send_error(404)

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/pull":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    for done in (0, 1_000_000, 3_000_000):
                        line = {"status": "pulling abc", "digest": "sha256:abc", "total": 3_000_000, "completed": done}
                        self.wfile.write((json.dumps(line) + "\n").encode())
                    self.wfile.write(b'{"status":"success"}\n')
                    outer.pulled.add(req["model"])
                elif self.path == "/api/chat":
                    if req["model"] not in outer.pulled:
                        self._json({"error": f"model '{req['model']}' not found"})
                        return
                    prompt = req["messages"][-1]["content"]
                    looping = "LOOPHARD" in prompt or ("LOOP" in prompt and "repeat_penalty" not in req.get("options", {}))
                    if looping:
                        body = json.dumps({"error": "prediction aborted, token repeat limit reached"}).encode()
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if "FAIL500" in prompt or ("FLAKY" in prompt and not outer.flaked):
                        outer.flaked = True
                        body = json.dumps({"error": "model requires more system memory (5.1 GiB) than is available (3.2 GiB)"}).encode()
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    outer.prompts.append(prompt)
                    text = prompt.split("\n\n\n", 1)[1]
                    if "MERGE" in text:  # the model joins the first two sentences
                        out = "[EN] " + " ".join(text.splitlines()).replace(". ", ", ", 1)
                    elif "SPLIT" in text:  # the model breaks the first sentence in two
                        out = "[EN] " + " ".join(text.splitlines()).replace(", ", ". Х", 1)
                    else:
                        out = "\n".join("[EN] " + l for l in text.splitlines())
                    self._json({"message": {"role": "assistant", "content": out}, "done": True})
                else:
                    self.send_error(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
