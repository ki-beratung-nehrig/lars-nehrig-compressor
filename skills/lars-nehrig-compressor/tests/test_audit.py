#!/usr/bin/env python3
"""Deterministic test suite for scripts/audit.py. No network, no fixtures
outside this repository. Run:  python tests/test_audit.py
"""
import base64
import datetime
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT = os.path.join(HERE, "..", "scripts", "audit.py")
spec = importlib.util.spec_from_file_location("audit", AUDIT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

FAILED = []
TOTAL = 0
TMP_ROOT = tempfile.mkdtemp(prefix="audit-tests-")
import atexit
atexit.register(shutil.rmtree, TMP_ROOT, ignore_errors=True)   # crash-safe cleanup


def check(name, passed):
    global TOTAL
    TOTAL += 1
    print(("PASS  " if passed else "FAIL  ") + name)
    if not passed:
        FAILED.append(name)


def tmpdir():
    return tempfile.mkdtemp(dir=TMP_ROOT)


def fake_png(w, h):
    ihdr = struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"
    chunk = b"IHDR" + ihdr
    return base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + chunk
        + struct.pack(">I", zlib.crc32(chunk))).decode()


def usage(n=10, **extra):
    u = {"input_tokens": n, "output_tokens": n,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    u.update(extra)
    return u


def asst(mid, model, u, content=None, uuid=None):
    m = {"model": model, "usage": u, "content": content or []}
    if mid is not None:
        m["id"] = mid
    rec = {"type": "assistant", "message": m}
    if uuid is not None:
        rec["uuid"] = uuid
    return rec


def tool_use(tid, name, inp=None):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp or {}}


def result(tid, content):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": content}]}}


def img_block(data):
    return {"type": "image", "source": {"type": "base64", "data": data}}


def write_jsonl(path, objs, raw_extra=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o) + "\n")
        if raw_extra:
            f.write(raw_extra)


def run_cli(args, cwd=None, env=None):
    e = os.environ.copy()
    if env:
        e.update(env)
    return subprocess.run([sys.executable, os.path.abspath(AUDIT)] + args,
                          capture_output=True, text=True, cwd=cwd, env=e)


def run_json(objs, extra_files=None, raw_extra=""):
    d = tmpdir()
    write_jsonl(os.path.join(d, "s.jsonl"), objs, raw_extra)
    for rel, objs2 in (extra_files or {}).items():
        write_jsonl(os.path.join(d, rel), objs2)
    r = run_cli(["--path", d, "--json"])
    return (json.loads(r.stdout) if r.returncode == 0 else None), r


# --- 1) Official vision examples, EXACT ---
for w, h, std, high in [(200, 200, 64, 64), (1000, 1000, 1296, 1296),
                        (1092, 1092, 1521, 1521), (1920, 1080, 1560, 2691),
                        (3840, 2160, 1560, 4784)]:
    check(f"vision {w}x{h} std == {std}", audit.image_tokens((w, h), False) == std)
    check(f"vision {w}x{h} high == {high}", audit.image_tokens((w, h), True) == high)
check("resized_size A4 (1075,1520) == (924,1307)",
      audit.resized_size(1075, 1520) == (924, 1307))
check("resized_size 1920x1080 std == (1456,819)",
      audit.resized_size(1920, 1080) == (1456, 819))

# --- 2) Image format headers, incl. zero-dimension corruption ---
gif = base64.b64encode(bytes.fromhex(
    "47494638396101000100800000000000ffffff"
    "2c00000000010001000002024401003b")).decode()
check("GIF 1x1 dims == (1,1)", audit.image_dims(gif) == (1, 1))
webp = base64.b64encode(
    b"RIFF" + struct.pack("<I", 22) + b"WEBPVP8X" + struct.pack("<I", 10)
    + b"\x00\x00\x00\x00" + (99).to_bytes(3, "little")
    + (49).to_bytes(3, "little")).decode()
check("WebP VP8X dims == (100,50)", audit.image_dims(webp) == (100, 50))
check("unknown format -> None", audit.image_dims(
    base64.b64encode(b"not an image").decode()) is None)
check("PNG with height 0 -> None (no crash)",
      audit.image_dims(fake_png(2000, 0)) is None)
check("image_tokens survives zero dims (tier cap)",
      audit.image_tokens((2000, 0), False) == 1568)

# --- 3) Pricing: current AND historical model ids ---
for mid, want in [("claude-3-5-haiku-20241022", 0.8),
                  ("claude-haiku-4-5-20251001", 1.0),
                  ("claude-3-haiku-20240307", 0.25),
                  ("claude-opus-4-1", 15.0),
                  ("claude-opus-4-20250514", 15.0),
                  ("claude-3-opus-20240229", 15.0),
                  ("claude-opus-4-8", 5.0),
                  ("claude-opus-4-5-20251101", 5.0)]:
    check(f"price {mid} == {want}", audit.base_price(mid) == want)
check("sonnet-5 intro 2.0 (2026-08-31)",
      audit.base_price("claude-sonnet-5", datetime.date(2026, 8, 31)) == 2.0)
check("sonnet-5 standard 3.0 (2026-09-01)",
      audit.base_price("claude-sonnet-5", datetime.date(2026, 9, 1)) == 3.0)
check("unknown model -> None", audit.base_price("weird-model-9") is None)

# --- 4) Image tier follows the RECEIVING request's model ---
out, _ = run_json([
    asst("m1", "claude-sonnet-4-6", usage(), [tool_use("t1", "browser_shot")]),
    result("t1", [img_block(fake_png(1920, 1080))]),
    asst("m2", "claude-opus-4-8", usage()),
])
img = next(t for t in out["tool_payload_estimated"] if t["tool"] == "browser_shot")
check("sonnet issues, opus receives -> high tier (2691)",
      img["image_tokens"] == 2691)

out, _ = run_json([
    asst("m1", "claude-opus-4-8", usage(), [tool_use("t1", "browser_shot")]),
    result("t1", [img_block(fake_png(1920, 1080))]),
    asst("m2", "claude-sonnet-4-6", usage()),
])
img = next(t for t in out["tool_payload_estimated"] if t["tool"] == "browser_shot")
check("opus issues, sonnet receives -> standard tier (1560)",
      img["image_tokens"] == 1560)

out, _ = run_json([
    asst("m1", "claude-opus-4-8", usage(), [tool_use("t1", "browser_shot")]),
    result("t1", [img_block(fake_png(1920, 1080))]),
])
img = next(t for t in out["tool_payload_estimated"] if t["tool"] == "browser_shot")
check("no receiver -> issuer fallback (2691) and counted",
      img["image_tokens"] == 2691 and out["images_tier_fallback"] == 1)

# --- 5) Corrupt image inside a run: no crash, unknown counted ---
out, r = run_json([
    asst("m1", "claude-opus-4-8", usage(), [tool_use("t1", "Shot")]),
    result("t1", [img_block(fake_png(2000, 0))]),
    asst("m2", "claude-opus-4-8", usage()),
])
check("corrupt image: exit 0 and images_unknown_dims == 1",
      r.returncode == 0 and out["images_unknown_dims"] == 1)

# --- 6) Robustness: truncated JSONL, missing ids ---
out, r = run_json([asst("m1", "claude-opus-4-8", usage())],
                  raw_extra='{"type":"assistant","message":{"id":"broken"\n')
check("truncated line: exit 0, invalid_json_lines == 1",
      r.returncode == 0 and out["invalid_json_lines"] == 1
      and out["files_with_parse_errors"] == 1)

out, _ = run_json([asst(None, "claude-opus-4-8", usage(10)),
                   asst(None, "claude-opus-4-8", usage(20))])
check("two id-less records stay separate (2 requests, counter 2)",
      out["requests"]["total"] == 2 and out["missing_request_ids"] == 2
      and out["usage_tokens_authoritative"]["input"] == 30)

# --- 7) Short text blocks aggregate before division ---
blocks = [{"type": "text", "text": "abc"}] * 100
out, _ = run_json([
    asst("m1", "claude-opus-4-8", usage(), [tool_use("t1", "T")]),
    result("t1", blocks),
])
t = out["tool_payload_estimated"][0]["text_tokens"]
check(f"100 x 'abc' blocks ~= 75 tokens (got {t})", t == 75)

# --- 8) context share includes uncached input ---
out, _ = run_json([asst("m1", "claude-opus-4-8",
                        usage(0, input_tokens=1000))])
check("input-only session: context_share_pct == 100",
      out["context_share_pct"] == 100.0)

# --- 9) Granular-only cache fields stay consistent ---
out, _ = run_json([asst("m1", "claude-opus-4-8", {
    "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
    "cache_creation": {"ephemeral_5m_input_tokens": 400,
                       "ephemeral_1h_input_tokens": 600}})])
check("granular cache: raw cache_write == 1000 and context == 100%",
      out["usage_tokens_authoritative"]["cache_write"] == 1000
      and out["context_share_pct"] == 100.0)

# --- 10) Browser signals deduplicated + screenshot tool names ---
shot = tool_use("t1", "browser_click",
                {"action": "screenshot", "coordinate": [1, 2]})
out, _ = run_json([
    asst("m1", "claude-opus-4-8", usage(), [shot]),
    asst("m1", "claude-opus-4-8", usage(), [shot]),      # duplicate line
    asst("m2", "claude-opus-4-8", usage(),
         [tool_use("t2", "mcp__browser__screenshot")]),
])
sig = out["browser_action_signals"]
check("duplicate browser tool_use counted once",
      sig["coordinate_actions"] == 1)
check("screenshot-named tool counted without action field",
      sig["screenshots"] == 2)
out, _ = run_json([
    asst("m1", "claude-opus-4-8", usage(),
         [tool_use("t1", "SomeDataTool", {"coordinate": [1, 2]})]),
])
check("non-browser tool input not counted", not out["browser_action_signals"])

# --- 11) Dedup, subagents, coverage, unicode (regression) ---
uml = {"note": "ü" * 100}
out, _ = run_json([
    asst("m1", "claude-opus-4-8", usage(100), [tool_use("t1", "T")]),
    asst("m1", "claude-opus-4-8", usage(100)),           # dup request
    result("t1", uml),
    result("t1", uml),                                   # dup result
    asst("m2", "weird-model-9", usage(100)),
    asst("m3", "<synthetic>", usage(0)),
], extra_files={"sess/subagents/agent-1.jsonl":
                [asst("s1", "claude-opus-4-8", usage(100))]})
check("requests deduplicated (3)", out["requests"]["total"] == 3)
check("main/subagents split (2/1)",
      out["requests"]["main"] == 2 and out["requests"]["subagents"] == 1)
check("tool result deduplicated (1 call)",
      out["tool_payload_estimated"][0]["calls"] == 1)
tt = out["tool_payload_estimated"][0]["text_tokens"]
check(f"unicode counted without escape inflation ({tt} tok, ~28)",
      20 <= tt <= 40)
check("usd present despite unknown model", out["usd_estimate_known_models"] > 0)
check("coverage between 0 and 100", 0 < out["usd_coverage_pct"] < 100)

# --- 12) Low-coverage warning in text output ---
d = tmpdir()
write_jsonl(os.path.join(d, "s.jsonl"),
            [asst("m1", "claude-opus-4-8", usage(1)),
             asst("m2", "weird-model-9", usage(0, output_tokens=1000000))])
r = run_cli(["--path", d])
check("low coverage prints WARNING", "WARNING" in r.stdout)

# --- 13) CLI scope flags mutually exclusive ---
check("--last --all rejected", run_cli(["--last", "--all"]).returncode != 0)
check("--all --path rejected",
      run_cli(["--all", "--path", d]).returncode != 0)

# --- 14) CLAUDE_CONFIG_DIR honored ---
cfg = tmpdir()
workdir = tmpdir()
slug = __import__("re").sub(r"[^A-Za-z0-9]", "-", workdir)
write_jsonl(os.path.join(cfg, "projects", slug, "s.jsonl"),
            [asst("m1", "claude-opus-4-8", usage(50))])
r = run_cli(["--json"], cwd=workdir, env={"CLAUDE_CONFIG_DIR": cfg})
ok_cfg = r.returncode == 0 and json.loads(r.stdout)["requests"]["total"] == 1
check("CLAUDE_CONFIG_DIR transcripts found", ok_cfg)

# --- 15) Branch copies: global dedup across files ---
out, _ = run_json(
    [asst("mA", "claude-opus-4-8", usage(100), uuid="u-1")],
    extra_files={"branch.jsonl":
                 [asst("mA", "claude-opus-4-8", usage(100), uuid="u-1")]})
check("branch copy: 1 request, 1 duplicate id, 1 extra record",
      out["requests"]["total"] == 1
      and out["duplicate_request_ids_across_files"] == 1
      and out["duplicate_request_records_across_files"] == 1
      and out["duplicate_usage_conflict_records"] == 0)

out, _ = run_json(
    [asst("mA", "claude-opus-4-8", usage(10), uuid="u-1")],       # partial
    extra_files={"branch.jsonl": [asst(
        "mA", "claude-opus-4-8",
        usage(10, cache_read_input_tokens=5000), uuid="u-1")]})   # fullest
check("partial vs full copy: fullest usage kept once",
      out["requests"]["total"] == 1
      and out["usage_tokens_authoritative"]["cache_read"] == 5000)

out, _ = run_json(
    [asst("mA", "claude-opus-4-8", usage(10), uuid="u-1")],
    extra_files={"branch.jsonl":
                 [asst("mA", "claude-opus-4-8", usage(10), uuid="u-2")]})
check("same id, different uuid: conflict counted",
      out["duplicate_usage_conflict_records"] == 1)

out, _ = run_json(
    [asst("m1", "claude-opus-4-8", usage(), [tool_use("t1", "T")]),
     result("t1", [{"type": "text", "text": "x" * 400}])],
    extra_files={"branch.jsonl":
                 [result("t1", [{"type": "text", "text": "x" * 400}])]})
check("copied tool result: payload once, cross-file dup counted",
      out["tool_payload_estimated"][0]["text_tokens"] == 100
      and out["duplicate_tool_result_ids_across_files"] == 1
      and out["tool_result_conflict_ids"] == 0)

out, _ = run_json(
    [asst("m1", "claude-opus-4-8", usage(), [tool_use("t1", "T")]),
     result("t1", [{"type": "text", "text": "xxxx"}])],          # tiny first
    extra_files={"branch.jsonl":
                 [result("t1", [{"type": "text", "text": "y" * 400}])]})
check("fullest tool-result copy wins (100 tok, conflict counted)",
      out["tool_payload_estimated"][0]["text_tokens"] == 100
      and out["tool_result_conflict_ids"] == 1)

out, _ = run_json(
    [asst("mA", "claude-opus-4-8", usage(100), uuid="u-1")],
    extra_files={"b1.jsonl": [asst("mA", "claude-opus-4-8", usage(100), uuid="u-1")],
                 "b2.jsonl": [asst("mA", "claude-opus-4-8", usage(100), uuid="u-1")]})
check("3 copies of one request id: 1 unique id, 2 extra records",
      out["requests"]["total"] == 1
      and out["duplicate_request_ids_across_files"] == 1
      and out["duplicate_request_records_across_files"] == 2)

out, _ = run_json(
    [asst("m1", "claude-opus-4-8", usage(), [tool_use("t1", "T")]),
     result("t1", [{"type": "text", "text": "AAAA"}])],
    extra_files={"branch.jsonl":
                 [result("t1", [{"type": "text", "text": "BBBB"}])]})
check("equal-length different text: conflict via fingerprint",
      out["tool_result_conflict_ids"] == 1
      and out["tool_payload_estimated"][0]["text_tokens"] == 1)

out, _ = run_json(
    [asst("m1", "claude-sonnet-4-6", usage(), [tool_use("t1", "Shot")]),
     result("t1", [img_block(fake_png(1, 1))]),
     asst("m2", "claude-sonnet-4-6", usage())],
    extra_files={"branch.jsonl":
                 [result("t1", [img_block(fake_png(2000, 2000))]),
                  asst("m3", "claude-sonnet-4-6", usage())]})
# Official formula: a square on the standard tier maxes at 39x39 patches
# = 1521 tokens (the 1568 cap is a bound, not a reachable value).
img = next(t for t in out["tool_payload_estimated"] if t["tool"] == "Shot")
check("same image count, larger dimensions wins (1521 tok, 1 image, conflict)",
      img["image_tokens"] == 1521 and img["images"] == 1
      and out["tool_result_conflict_ids"] == 1)

out, _ = run_json(
    [asst("m1", "claude-sonnet-4-6", usage(), [tool_use("t1", "T")]),
     result("t1", [{"type": "text", "text": "x" * 400}]),
     asst("m2", "claude-sonnet-4-6", usage())],
    extra_files={"branch.jsonl":
                 [result("t1", [img_block(fake_png(1, 1))]),
                  asst("m3", "claude-sonnet-4-6", usage())]})
t = out["tool_payload_estimated"][0]
check("no union across variants: one canonical copy only",
      t["text_tokens"] == 100 and t["image_tokens"] == 0 and t["images"] == 0
      and out["tool_result_conflict_ids"] == 1)

out, _ = run_json(
    [asst(None, "claude-opus-4-8", usage(10))],
    extra_files={"b.jsonl": [asst(None, "claude-opus-4-8", usage(20))]})
check("id-less records in two files stay separate",
      out["requests"]["total"] == 2)

# --- 16) Slug: non-alphanumeric characters become dashes ---
cfg2 = tmpdir()
work2 = os.path.join(tmpdir(), "My Project_v1.2")
os.makedirs(work2)
slug2 = __import__("re").sub(r"[^A-Za-z0-9]", "-", work2)
write_jsonl(os.path.join(cfg2, "projects", slug2, "s.jsonl"),
            [asst("m1", "claude-opus-4-8", usage(50))])
r = run_cli(["--json"], cwd=work2, env={"CLAUDE_CONFIG_DIR": cfg2})
check("slug with space/underscore/dot resolves without --path",
      r.returncode == 0 and json.loads(r.stdout)["requests"]["total"] == 1)

# --- 17) Median stays fractional; screenshot not double-counted ---
out, _ = run_json([asst("m1", "claude-opus-4-8", usage(0, input_tokens=1)),
                   asst("m2", "claude-opus-4-8", usage(0, input_tokens=2))])
check("median of 1 and 2 == 1.5",
      out["context_per_request"]["median"] == 1.5)

out, _ = run_json([asst("m1", "claude-opus-4-8", usage(), [tool_use(
    "t1", "mcp__browser__screenshot", {"action": "screenshot"})])])
check("screenshot name + action counted once",
      out["browser_action_signals"]["screenshots"] == 1)

# --- 18) Zero dimensions for every parser family ---
gif0 = base64.b64encode(b"GIF89a" + (5).to_bytes(2, "little")
                        + (0).to_bytes(2, "little") + b"\x00" * 20).decode()
jpeg0 = base64.b64encode(
    b"\xff\xd8\xff\xc0\x00\x11\x08" + (0).to_bytes(2, "big")
    + (2000).to_bytes(2, "big") + b"\x00" * 20).decode()
webp0 = base64.b64encode(
    b"RIFF" + struct.pack("<I", 30) + b"WEBPVP8 " + struct.pack("<I", 20)
    + b"\x00" * 10 + (100).to_bytes(2, "little")
    + (0).to_bytes(2, "little") + b"\x00" * 6).decode()
check("GIF height 0 -> None", audit.image_dims(gif0) is None)
check("JPEG height 0 -> None", audit.image_dims(jpeg0) is None)
check("WebP height 0 -> None", audit.image_dims(webp0) is None)

# --- 19) CLAUDE_SKILL_DIR resolves to a runnable script (approximation) ---
skill_dir = os.path.abspath(os.path.join(HERE, ".."))
resolved = os.path.join(skill_dir, "scripts", "audit.py")
r = subprocess.run([sys.executable, resolved, "--path", cfg2, "--json"],
                   capture_output=True, text=True, cwd=tmpdir(),
                   env={**os.environ, "CLAUDE_SKILL_DIR": skill_dir})
check("script runs via CLAUDE_SKILL_DIR path from foreign cwd",
      r.returncode == 0)

print()
if FAILED:
    print(f"RESULT: {len(FAILED)}/{TOTAL} FAILED")
    sys.exit(1)
print(f"RESULT: {TOTAL}/{TOTAL} PASSED")
