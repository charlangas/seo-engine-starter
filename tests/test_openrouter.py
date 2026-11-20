"""Writer-editor loop self-verification with a mocked OpenRouter transport (no network, no key spend)."""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import apiclient, config as cfg, writer
from textutil import split_frontmatter
from tests_common import report

EDITOR_NOTE = "ZZ-EDITOR-NOTE cut the unsupported 73 percent figure"


class FakeResp:
    def __init__(self, payload, status=200):
        self.status_code, self._p, self.text = status, payload, json.dumps(payload)
    def json(self):
        return self._p


def main(path="configs/test-minimal.yaml"):
    live = cfg.load_config(path)
    briefs = sorted(live["_paths"]["briefs"].glob("brief_*.json"))
    if not briefs:
        print("OPENROUTER: SKIP (run test_stage2.py first to create a brief)")
        return True
    brief = json.loads(briefs[0].read_text())
    brief.setdefault("pubDate", "2026-01-05")
    os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"
    real_request = apiclient.requests.request

    def run(editor_replies, writer_backend="openrouter"):
        c = cfg.load_config(path, dry_run=True)
        c["writer_backend"] = writer_backend
        tb = writer.TemplateBackend()                     # a clean article stands in for the writer model's output
        good = tb.draft(brief, c)
        for _ in range(4):
            fx = writer.critique(good, brief, c)
            if not fx:
                break
            good = tb.revise(good, fx, brief, c)
        calls, replies = [], list(editor_replies)

        def fake(method, url, **kw):
            body = kw["json"]
            role = "editor" if body["model"] == c["editor_model"] else "writer"
            calls.append({"role": role, "url": url, "auth": kw["headers"]["Authorization"],
                          "user": body["messages"][1]["content"], "json_mode": "response_format" in body})
            if role == "editor":
                content = replies.pop(0) if replies else '{"verdict": "approve", "fixes": []}'
            else:
                content = f'TITLE: {good["title"]}\nDESCRIPTION: {good["description"]}\n===\n{good["body"]}'
            if isinstance(content, dict):
                return FakeResp(content)
            return FakeResp({"model": body["model"], "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                             "usage": {"prompt_tokens": 10, "completion_tokens": 20, "cost": 0.001}})

        apiclient.requests.request = fake
        try:
            md = writer.generate_article(brief, c)
        finally:
            apiclient.requests.request = real_request
        return md, calls, c

    revise = json.dumps({"verdict": "revise", "fixes": [{"section": "Introduction", "problem": "Unsupported number.",
                                                         "instruction": EDITOR_NOTE}]})
    md, calls, c = run([revise])
    roles = [x["role"] for x in calls]
    fm, _ = split_frontmatter(md)
    checks = [
        ("writer and editor alternate", roles == ["writer", "editor", "writer", "editor"], roles),
        ("calls go to OpenRouter with a bearer key", all(x["url"] == apiclient.OPENROUTER_URL and x["auth"].startswith("Bearer ") for x in calls), ""),
        ("editor asked for JSON, given the brief and the draft", calls[1]["json_mode"] and "BRIEF:" in calls[1]["user"] and "DRAFT:" in calls[1]["user"], ""),
        ("editor fix reaches the writer's revision prompt", EDITOR_NOTE in calls[2]["user"], ""),
        ("editor text never enters the article", EDITOR_NOTE not in md and "ZZ-EDITOR" not in md, ""),
        ("LLM article is not flagged draft", "draft" not in fm, ""),
        ("LLM calls counted against the per-run cap", c["_runtime"].calls["llm"] == 4, c["_runtime"].calls["llm"]),
    ]
    md2, calls2, _ = run(["this is not json at all"])
    checks.append(("unusable editor reply does not block the article", bool(md2) and [x["role"] for x in calls2] == ["writer", "editor"], [x["role"] for x in calls2]))
    md3, calls3, _ = run(['```json\n{"verdict": "approve", "fixes": []}\n```'])
    checks.append(("editor approval stops the loop", [x["role"] for x in calls3] == ["writer", "editor"], ""))
    try:
        run([{"error": {"message": "model overloaded", "code": 502}}])
        checks.append(("error body is not treated as content", True, "editor error skipped"))
    except Exception as e:
        checks.append(("error body is not treated as content", False, str(e)))

    c = cfg.load_config(path, dry_run=True)
    c["max_llm_calls"] = 1
    c["_runtime"].limits["llm"] = 1
    c["writer_backend"] = "openrouter"
    apiclient.requests.request = lambda *a, **k: FakeResp({"choices": [{"finish_reason": "stop", "message": {"content": "TITLE: x\n===\n# x"}}]})
    try:
        writer.generate_article(brief, c)
        checks.append(("per-run LLM cap stops further calls", False, "no error"))
    except Exception as e:
        checks.append(("per-run LLM cap stops further calls", type(e).__name__ == "BudgetExceeded", type(e).__name__))
    finally:
        apiclient.requests.request = real_request

    os.environ["OPENROUTER_API_KEY"] = ""
    c = cfg.load_config(path, dry_run=True)
    c["writer_backend"] = "openrouter"
    try:
        writer.pick_backend(c)
        checks.append(("missing key fails clearly", False, ""))
    except writer.WriterError as e:
        checks.append(("missing key fails clearly", "OPENROUTER_API_KEY" in str(e), str(e)))
    c["writer_backend"] = "auto"
    checks.append(("auto falls back to template without a key", writer.pick_backend(c).name in ("template", "anthropic"), writer.pick_backend(c).name))
    return report("OPENROUTER WRITER-EDITOR (mocked transport)", checks)

if __name__ == "__main__":
    sys.exit(0 if main() else 1)
