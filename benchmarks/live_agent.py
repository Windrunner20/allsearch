"""Pi Agent round. The prompt contains only the user query or the fetch URL."""

from __future__ import annotations

import json
import re
import subprocess
import time
import traceback
from pathlib import Path

from benchmarks.harness import load_cases, provider_query, score_observation

OUT = Path(__file__).resolve().parents[1] / "benchmarks" / "runs" / "agent-20261015T0839Z"
URL_RE = re.compile(r"https?://[^\s)>\"']+")


def _answer_from_events(stdout: str) -> tuple[list[str], str]:
    tools: list[str] = []
    answer = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "tool_execution_start":
            name = event.get("toolName") or event.get("name")
            if name:
                tools.append(str(name))
        if event.get("type") == "message_end" and (event.get("message") or {}).get("role") == "assistant":
            content = (event.get("message") or {}).get("content")
            if isinstance(content, str):
                answer = content
            elif isinstance(content, list):
                answer = "".join(
                    str(part.get("text") or "") if isinstance(part, dict) else str(part) for part in content
                )
    return tools, answer


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cases = load_cases()
    for index, case in enumerate(cases, start=1):
        dest = OUT / f"{case['id']}.json"
        if dest.exists():
            print(f"{index}/39 skip {case['id']}", flush=True)
            continue
        query = provider_query(case)
        if case.get("kind") == "fetch":
            query = "请读取这个 URL 并说明页面是否在讲 venv 模块：" + str(case.get("url") or "")
        prompt = (
            "你只能使用 AllSearch MCP 的 search、fetch、read_result、health。"
            "不要使用其他 MCP，不要用 bash，不要读本地 benchmark 文件。"
            "最终答案必须给出可核对的原始链接。如果读到评论，请引用评论原句。\n问题："
            + query
        )
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                ["pi", "--mode", "json", "--no-session", "--no-builtin-tools", "-p", prompt],
                cwd="/home/bowie/allsearch",
                capture_output=True,
                text=True,
                timeout=240,
            )
            tools, answer = _answer_from_events(proc.stdout)
            code = proc.returncode
            err = proc.stderr[-400:]
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", "replace")
            tools, answer = _answer_from_events(stdout)
            code = 124
            err = "TIMEOUT"
        except Exception:
            tools, answer, code, err = [], "", 1, traceback.format_exc()[-800:]
        urls = URL_RE.findall(answer)
        observation = {
            "id": case["id"],
            "answer": answer,
            "results": [{"url": url, "title": "", "snippet": answer[:800]} for url in urls[:8]],
            "executed_at": "2026-10-15T08:40:00Z",
        }
        try:
            scored = score_observation(case, observation)
        except Exception:
            scored = {"task": {"status": "score_error", "reasons": [traceback.format_exc()[-400:]]}}
        row = {
            "id": case["id"],
            "query": query,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "returncode": code,
            "tools": tools,
            "answer": answer[:4000],
            "stderr": err,
            "score": scored,
        }
        dest.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"{index}/39 {case['id']} tools={tools} task={scored.get('task', {}).get('status')} {row['elapsed_ms']}ms",
            flush=True,
        )


if __name__ == "__main__":
    main()
