#!/usr/bin/env python3
"""Show where tokens actually go in your Claude Code sessions.

Reads the local Claude Code transcripts (JSONL) and reports:
  - AUTHORITATIVE usage: billed token counts taken from the API usage fields.
  - ESTIMATED one-time tool payload: how much each tool wrote into context,
    once. Text is approximated as chars/4; images use Anthropic's official
    resize reference and the 28x28 patch formula, never base64 length.
    Note: this is one-time payload size, not lifetime context exposure.

    python audit.py                # sessions of the current project
    python audit.py --last         # only the most recently modified session
    python audit.py --all          # every project on this machine
    python audit.py --path DIR     # a specific directory of transcripts
    python audit.py --json         # machine-readable summary

Nothing leaves your machine; this only reads local transcript files. The
transcript root honors CLAUDE_CONFIG_DIR (default ~/.claude).

Correctness notes:
  - One API request can span several JSONL lines, and branching a session
    copies the whole conversation into a new file. Requests, tool results and
    tool calls are therefore deduplicated GLOBALLY across all read files by
    their real IDs; cross-file duplicates and conflicting copies are counted
    and reported. Unparseable lines and records without an ID are counted
    too, never silently dropped or merged.
  - The auditor parses Claude Code's internal JSONL format, which can change
    between client releases; watch the reported warnings after upgrades.
  - Subagent transcripts (nested under <session>/subagents/) are included
    and reported separately - subagents have their own context and cost.
  - Images are priced with the vision tier of the model that RECEIVES the
    tool result (the next request), falling back to the issuing model only
    when no later request exists in the file; fallbacks are counted.
  - Dollar figures are standard API list-price equivalents at today's list
    prices (see _price_rules; Sonnet 5 is date-aware) and exclude modifiers
    such as batch discounts, fast mode, data-residency multipliers and
    server-tool fees. Subscription plans are billed differently. A coverage
    percentage shows how much of the usage the estimate covers.
  - median is statistics.median; p90 is the nearest-rank quantile.
"""
import argparse
import base64
import collections
import datetime
import glob
import hashlib
import json
import math
import os
import re
import statistics
import sys

# Cache multipliers relative to base input price (all current Claude models):
# output = 5x, 1h cache write = 2x, 5m write = 1.25x, cache read = 0.1x.
W_OUTPUT, W_WRITE_5M, W_WRITE_1H, W_READ = 5.0, 1.25, 2.0, 0.1


def _price_rules(on_date):
    """Ordered (substring, $/MTok input) rules matched against the model id.

    List prices as of 2026-07, including documented historical generations
    (Opus 4/4.1 and Claude 3 Opus $15, Claude 3 Haiku $0.25, Haiku 3.5 $0.80).
    Sonnet 5 is date-dependent. Unknown models are excluded from the dollar
    estimate but counted in the coverage figure.
    """
    sonnet5 = 2.0 if on_date <= datetime.date(2026, 8, 31) else 3.0
    return (
        ("fable", 10.0),
        ("mythos", 10.0),
        ("opus-4-5", 5.0),
        ("opus-4-6", 5.0),
        ("opus-4-7", 5.0),
        ("opus-4-8", 5.0),
        ("opus", 15.0),        # Opus 4, 4.1, Claude 3 Opus and their aliases
        ("sonnet-5", sonnet5),
        ("3-5-haiku", 0.8),    # official id: claude-3-5-haiku-YYYYMMDD
        ("haiku-3-5", 0.8),
        ("3-haiku", 0.25),     # Claude 3 Haiku (claude-3-haiku-20240307)
        ("haiku", 1.0),
        ("sonnet", 3.0),
    )


def base_price(model_id, on_date=None):
    m = (model_id or "").lower()
    for key, price in _price_rules(on_date or datetime.date.today()):
        if key in m:
            return price
    return None


# Models on the high-resolution vision tier (long edge <= 2576 px, cap 4784
# visual tokens). All others use the standard tier (1568 px / 1568 tokens).
HIGH_RES_MODELS = ("fable", "mythos", "opus-4-8", "opus-4-7", "sonnet-5")

# Tools whose inputs count as browser/computer action signals.
BROWSER_TOOL_HINTS = ("browser", "computer", "chrome", "playwright", "puppeteer")


def is_high_res(model_id):
    m = (model_id or "").lower()
    return any(h in m for h in HIGH_RES_MODELS)


def normalize_usage(u):
    """One canonical view of a usage record, preferring granular cache fields.
    Used for raw counts, cost weighting and context alike, so they can't
    drift apart when only one representation is present."""
    cc = u.get("cache_creation") or {}
    w5 = cc.get("ephemeral_5m_input_tokens")
    w1 = cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = u.get("cache_creation_input_tokens", 0) or 0, 0
    return {
        "input": u.get("input_tokens", 0) or 0,
        "output": u.get("output_tokens", 0) or 0,
        "w5": w5 or 0,
        "w1": w1 or 0,
        "read": u.get("cache_read_input_tokens", 0) or 0,
    }


def weighted(n):
    """Price-ratio weight per category (unit: input-token equivalents)."""
    return {
        "input": float(n["input"]),
        "output": n["output"] * W_OUTPUT,
        "cache_write": n["w5"] * W_WRITE_5M + n["w1"] * W_WRITE_1H,
        "cache_read": n["read"] * W_READ,
    }


# ---------- image dimension parsing (JPEG, PNG, GIF, WebP) ----------

def _valid(dims):
    return dims if dims and dims[0] > 0 and dims[1] > 0 else None


def jpeg_dims(b):
    i = 2
    while i < len(b) - 9:
        if b[i] != 0xFF:
            i += 1
            continue
        marker = b[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return _valid((int.from_bytes(b[i + 7:i + 9], "big"),
                           int.from_bytes(b[i + 5:i + 7], "big")))
        i += 2 + int.from_bytes(b[i + 2:i + 4], "big")
    return None


def png_dims(b):
    if len(b) >= 24 and b[:8] == bytes((0x89,)) + b"PNG" + bytes((0x0D, 0x0A, 0x1A, 0x0A)):
        return _valid((int.from_bytes(b[16:20], "big"),
                       int.from_bytes(b[20:24], "big")))
    return None


def gif_dims(b):
    if len(b) >= 10 and b[:6] in (b"GIF87a", b"GIF89a"):
        return _valid((int.from_bytes(b[6:8], "little"),
                       int.from_bytes(b[8:10], "little")))
    return None


def webp_dims(b):
    if b[:4] != b"RIFF" or b[8:12] != b"WEBP" or len(b) < 30:
        return None
    chunk = b[12:16]
    if chunk == b"VP8X":
        return _valid((1 + int.from_bytes(b[24:27], "little"),
                       1 + int.from_bytes(b[27:30], "little")))
    if chunk == b"VP8L" and b[20] == 0x2F:
        bits = int.from_bytes(b[21:25], "little")
        return _valid(((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1))
    if chunk == b"VP8 ":
        return _valid((int.from_bytes(b[26:28], "little") & 0x3FFF,
                       int.from_bytes(b[28:30], "little") & 0x3FFF))
    return None


def image_dims(data_b64):
    try:
        raw = base64.b64decode(data_b64[:200000], validate=False)
        return jpeg_dims(raw) or png_dims(raw) or gif_dims(raw) or webp_dims(raw)
    except Exception:
        return None


# ---------- official vision cost (Anthropic reference implementation) ----------

def count_image_tokens(width, height):
    """Visual tokens: one token per 28x28 pixel patch."""
    return math.ceil(width / 28) * math.ceil(height / 28)


def resized_size(width, height, max_edge=1568, max_tokens=1568):
    """Anthropic's reference: largest aspect-preserving size satisfying both
    the edge limit and the visual-token limit (binary search, ties-to-even).
    Source: platform.claude.com/docs/en/build-with-claude/vision-coordinates
    """
    def fits(w, h):
        return (math.ceil(w / 28) * 28 <= max_edge
                and math.ceil(h / 28) * 28 <= max_edge
                and count_image_tokens(w, h) <= max_tokens)

    if fits(width, height):
        return (width, height)
    if height > width:
        rh, rw = resized_size(height, width, max_edge, max_tokens)
        return (rw, rh)
    aspect = width / height
    lo, hi = 1, width
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if fits(mid, max(round(mid / aspect), 1)):
            lo = mid
        else:
            hi = mid
    return (lo, max(round(lo / aspect), 1))


def image_tokens(dims, high_res):
    edge, cap = (2576, 4784) if high_res else (1568, 1568)
    if not dims or dims[0] < 1 or dims[1] < 1:
        return cap          # unknown/corrupt: worst case, reported separately
    try:
        w, h = resized_size(dims[0], dims[1], edge, cap)
        return count_image_tokens(w, h)
    except Exception:
        return cap


# ---------- transcript discovery ----------

def is_subagent(path):
    return "subagents" in path.replace("\\", "/").split("/")


def find_transcripts(args):
    if args.path:
        files = glob.glob(os.path.join(args.path, "**", "*.jsonl"), recursive=True)
        if not files:
            sys.exit(f"No .jsonl transcripts found under {args.path}")
    else:
        config = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
        root = os.path.join(os.path.expanduser(config), "projects")
        if args.all:
            files = glob.glob(os.path.join(root, "*", "**", "*.jsonl"), recursive=True)
        else:
            # Claude Code slugifies the working directory by replacing every
            # non-alphanumeric character with a dash (C:\My Project_v1.2 ->
            # C--My-Project-v1-2). Source: code.claude.com/docs/en/sessions.
            slug = re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
            files = glob.glob(os.path.join(root, slug, "**", "*.jsonl"), recursive=True)
            if not files:
                dirs = sorted(glob.glob(os.path.join(root, "*")),
                              key=os.path.getmtime, reverse=True)
                names = "\n  ".join(os.path.basename(d) for d in dirs[:8]) or "(none)"
                sys.exit(
                    f"No transcripts for this directory (looked for '{slug}').\n"
                    f"Recent projects under {root}:\n  {names}\n"
                    f"Run from the project directory, or pass --all / --path."
                )
    files.sort(key=os.path.getmtime)
    if args.last:
        main_files = [f for f in files if not is_subagent(f)]
        if main_files:
            last = main_files[-1]
            session = os.path.splitext(os.path.basename(last))[0]
            files = [last] + [f for f in files
                              if is_subagent(f) and (os.sep + session + os.sep) in f]
    return files


def count_actions(obj, ctr):
    """Count action signals inside browser/computer tool inputs (recursive)."""
    if isinstance(obj, dict):
        if obj.get("action") == "screenshot":
            ctr["screenshots"] += 1
        if "coordinate" in obj:
            ctr["coordinate_actions"] += 1
        if "ref" in obj:
            ctr["ref_actions"] += 1
        for v in obj.values():
            count_actions(v, ctr)
    elif isinstance(obj, list):
        for v in obj:
            count_actions(v, ctr)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--last", action="store_true", help="only the most recent session")
    scope.add_argument("--all", action="store_true", help="every project on this machine")
    scope.add_argument("--path", help="read transcripts from this directory instead")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    files = find_transcripts(args)
    requests = {}             # request id -> record (GLOBAL across files)
    tool_meta = {}            # tool_use id -> (tool name, issuer high_res tier)
    res_info = {}             # result key -> {"file": first file, "fps": set}
    res_best = {}             # result key -> canonical variant (highest score)
    tool_chars = collections.Counter()   # summed chars; divided by 4 once at end
    tool_img = collections.Counter()
    tool_imgcnt = collections.Counter()
    tool_cnt = collections.Counter()
    behavior = collections.Counter()
    unknown_dims = 0
    tier_fallbacks = 0
    invalid_lines = 0
    error_files = set()
    missing_ids = 0
    dup_request_ids = set()   # request ids appearing in more than one file
    dup_request_records = 0   # additional cross-file request records removed
    dup_conflicts = 0         # duplicate records with a different record uuid
    dup_result_ids = set()    # tool_use_ids whose results appear in >1 file
    dup_result_records = 0    # additional cross-file result records removed

    def finalize(cand, high, fallback):
        """Price a result variant's images with the receiving tier, then keep
        it as the canonical copy for its tool_use_id iff its completeness
        score (text chars + estimated image tokens) beats the current best.
        Strict > keeps the first-read copy on ties. Exactly ONE variant wins;
        variants are never merged."""
        img_tok = unknown = 0
        for dims in cand["dims"]:
            if dims is None:
                unknown += 1
            img_tok += image_tokens(dims, high)
        score = cand["chars"] + img_tok
        best = res_best.get(cand["key"])
        if best is None or score > best["score"]:
            res_best[cand["key"]] = {
                "name": cand["name"], "chars": cand["chars"],
                "img_tokens": img_tok, "imgs": len(cand["dims"]),
                "unknown": unknown, "fallback": fallback, "score": score,
            }

    for f in files:
        sub = is_subagent(f)
        pending = []          # images awaiting the RECEIVING request's model
        with open(f, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    invalid_lines += 1
                    error_files.add(f)
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                if d.get("type") == "assistant":
                    rid = msg.get("id") or d.get("requestId")
                    if rid is None:
                        missing_ids += 1
                        # No real id: scope to file+line so nothing collides.
                        rid = f"__noid::{f}::{d.get('uuid') or lineno}"
                    model = msg.get("model")
                    if model:
                        # This request receives everything buffered before it.
                        high = is_high_res(model)
                        for cand in pending:
                            finalize(cand, high, False)
                        pending = []
                    u = msg.get("usage")
                    if u:
                        rec = {"usage": u, "model": model, "sub": sub,
                               "uuid": d.get("uuid"), "file": f}
                        old = requests.get(rid)
                        if old is None:
                            requests[rid] = rec
                        else:
                            if old["file"] != f:
                                # Branched sessions copy history verbatim.
                                dup_request_ids.add(rid)
                                dup_request_records += 1
                                if old["uuid"] != rec["uuid"]:
                                    dup_conflicts += 1
                            # Keep the most complete usage variant.
                            if (sum(weighted(normalize_usage(u)).values())
                                    > sum(weighted(normalize_usage(
                                        old["usage"])).values())):
                                requests[rid] = rec
                    if isinstance(content, list):
                        for b in content:
                            if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                                continue
                            tid = b.get("id")
                            if tid in tool_meta:
                                continue          # duplicate line of same call
                            name = b.get("name")
                            tool_meta[tid] = (name, is_high_res(model))
                            lname = (name or "").lower()
                            if any(h in lname for h in BROWSER_TOOL_HINTS):
                                before = behavior["screenshots"]
                                count_actions(b.get("input"), behavior)
                                # Name-based fallback only when the input
                                # itself carried no screenshot action.
                                if ("screenshot" in lname
                                        and behavior["screenshots"] == before):
                                    behavior["screenshots"] += 1
                elif d.get("type") == "user" and isinstance(content, list):
                    for b in content:
                        if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                            continue
                        tid = b.get("tool_use_id")
                        key = tid if tid is not None else f"__noid::{f}::{lineno}"
                        name, issuer_high = tool_meta.get(tid, ("?", False))
                        # Measure this copy first, then canonicalize: across
                        # branch copies the FULLEST variant wins, not the
                        # first one read, and differing copies are counted.
                        chars = 0
                        dims_list = []
                        cont = b.get("content")
                        for blk in (cont if isinstance(cont, list) else [cont]):
                            if isinstance(blk, dict) and blk.get("type") == "image":
                                data = ((blk.get("source") or {}).get("data")) or ""
                                dims_list.append(image_dims(data))
                            elif isinstance(blk, dict) and blk.get("type") == "text":
                                chars += len(blk.get("text") or "")
                            elif isinstance(blk, str):
                                chars += len(blk)
                            elif blk is not None:
                                # Structured results count too; keep unicode
                                # intact so chars/4 isn't inflated.
                                chars += len(json.dumps(
                                    blk, ensure_ascii=False,
                                    separators=(",", ":")))
                        # Content fingerprint over the FULL normalized block
                        # (image bytes included), so copies that differ in
                        # anything - equal-length text, image dimensions -
                        # register as conflicts.
                        fp = hashlib.sha256(json.dumps(
                            cont, ensure_ascii=False, sort_keys=True,
                            default=str).encode("utf-8", "replace")).hexdigest()
                        info = res_info.get(key)
                        if info is None:
                            res_info[key] = {"file": f, "fps": {fp},
                                             "name": name}
                        else:
                            if info["file"] != f:
                                dup_result_ids.add(key)
                                dup_result_records += 1
                            info["fps"].add(fp)
                            # A copy parsed before its tool_use (file-order
                            # ties) lacks the name; keep the known one.
                            if info["name"] == "?" and name != "?":
                                info["name"] = name
                        # Every copy becomes a candidate; exactly one wins in
                        # finalize() once its receiving tier is known.
                        pending.append({"key": key, "name": name,
                                        "chars": chars, "dims": dims_list,
                                        "issuer_high": issuer_high})
        # No later request received these: fall back to the issuing model.
        for cand in pending:
            finalize(cand, cand["issuer_high"], True)

    # Attribute payload from exactly ONE canonical variant per tool result.
    for key, best in res_best.items():
        name = res_info.get(key, {}).get("name") or best["name"]
        tool_cnt[name] += 1
        tool_chars[name] += best["chars"]
        tool_img[name] += best["img_tokens"]
        tool_imgcnt[name] += best["imgs"]
        unknown_dims += best["unknown"]
        if best["fallback"]:
            tier_fallbacks += best["imgs"]
    result_conflicts = {k for k, i in res_info.items() if len(i["fps"]) > 1}

    tool_text = {n: c // 4 for n, c in tool_chars.items()}

    # Zero-usage records (e.g. synthetic entries) are excluded from stats but
    # must not poison the dollar estimate or coverage.
    live = {k: r for k, r in requests.items()
            if sum(weighted(normalize_usage(r["usage"])).values()) > 0}
    if not live:
        sys.exit("No usage data found in the transcripts.")

    tok = collections.Counter()
    wsum = collections.Counter()      # ratio-weighted (price-independent)
    dollar = collections.Counter()    # per-model dollar-weighted categories
    per_model = collections.Counter()
    scope_tok = collections.Counter()
    scope_req = collections.Counter()
    usd_known = w_known = w_total = 0.0
    ctx = []
    today = datetime.date.today()
    for r in live.values():
        n = normalize_usage(r["usage"])
        tok["input"] += n["input"]
        tok["output"] += n["output"]
        tok["cache_write"] += n["w5"] + n["w1"]
        tok["cache_read"] += n["read"]
        w = weighted(n)
        wsum.update(w)
        wr = sum(w.values())
        w_total += wr
        per_model[r["model"] or "unknown"] += 1
        p = base_price(r["model"], today)
        if p is not None:
            usd_known += wr / 1e6 * p
            w_known += wr
            for k, v in w.items():
                dollar[k] += v / 1e6 * p
        scope = "subagents" if r["sub"] else "main"
        scope_req[scope] += 1
        scope_tok[scope] += n["input"] + n["output"] + n["w5"] + n["w1"] + n["read"]
        ctx.append(n["input"] + n["w5"] + n["w1"] + n["read"])
    ctx.sort()
    # Shares: exact dollar shares across known-price models; ratio-weighted
    # fallback only if every model is unknown.
    basis = dollar if usd_known > 0 else wsum
    total_b = sum(basis.values()) or 1.0
    shares = {k: round(100 * basis[k] / total_b, 1) for k in
              ("input", "output", "cache_write", "cache_read")}
    # Context = everything the model processes as input: uncached input plus
    # cache writes and cache reads.
    ctx_share = round(shares["input"] + shares["cache_write"]
                      + shares["cache_read"], 1)
    coverage = round(100 * w_known / w_total, 1) if w_total else 0.0
    tool_total = {n: tool_text.get(n, 0) + tool_img[n] for n in tool_cnt}
    top = sorted(tool_total.items(), key=lambda x: -x[1])[:10]
    p90 = ctx[max(0, math.ceil(0.9 * len(ctx)) - 1)]   # nearest-rank

    summary = {
        "transcripts": {"total": len(files),
                        "subagent_files": sum(1 for f in files if is_subagent(f))},
        "requests": {"total": len(live), **dict(scope_req)},
        "usage_tokens_authoritative": dict(tok),
        "usage_tokens_by_scope": dict(scope_tok),
        "cost_shares_pct": shares,
        "context_share_pct": ctx_share,
        "cost_shares_basis": "dollar_known_models" if usd_known > 0 else "price_ratio",
        "context_per_request": {"median": round(statistics.median(ctx), 1),
                                "p90": p90, "max": ctx[-1]},
        "models": dict(per_model),
        "usd_estimate_known_models": round(usd_known, 2),
        "usd_coverage_pct": coverage,
        "pricing_date": today.isoformat(),
        "tool_payload_estimated": [
            {"tool": n, "text_tokens": tool_text.get(n, 0),
             "image_tokens": tool_img[n], "images": tool_imgcnt[n],
             "calls": tool_cnt[n]} for n, _ in top],
        "images_unknown_dims": unknown_dims,
        "images_tier_fallback": tier_fallbacks,
        "browser_action_signals": dict(behavior),
        "invalid_json_lines": invalid_lines,
        "files_with_parse_errors": len(error_files),
        "missing_request_ids": missing_ids,
        "duplicate_request_ids_across_files": len(dup_request_ids),
        "duplicate_request_records_across_files": dup_request_records,
        "duplicate_usage_conflict_records": dup_conflicts,
        "duplicate_tool_result_ids_across_files": len(dup_result_ids),
        "duplicate_tool_result_records_across_files": dup_result_records,
        "tool_result_conflict_ids": len(result_conflicts),
    }
    if args.json:
        print(json.dumps(summary, indent=2))
        return

    print(f"\nTranscripts: {len(files)} ({summary['transcripts']['subagent_files']} subagent)"
          f"   Unique requests: {len(live):,} "
          f"(main {scope_req['main']:,} / subagents {scope_req['subagents']:,})")
    print("Models: " + ", ".join(f"{m} ({n:,})" for m, n in per_model.most_common()))
    if invalid_lines or missing_ids:
        print(f"  WARNING: {invalid_lines} unparseable JSONL lines in "
              f"{len(error_files)} file(s); {missing_ids} records without an id.")
    if dup_request_records or dup_result_records:
        print(f"  Branch copies deduplicated: {len(dup_request_ids):,} request"
              f" IDs ({dup_request_records:,} additional records removed) and"
              f" {len(dup_result_ids):,} tool-result IDs"
              f" ({dup_result_records:,} records).")
        if dup_conflicts or result_conflicts:
            print(f"  Conflicting copies kept at their fullest variant:"
                  f" {dup_conflicts} usage records,"
                  f" {len(result_conflicts)} tool-result IDs.")

    print("\n=== AUTHORITATIVE USAGE (billed tokens from transcripts) ===")
    if coverage < 95:
        print(f"  WARNING: shares below cover only {coverage:.1f}% of usage"
              " (unknown model prices).")
    for k in ("cache_read", "cache_write", "output", "input"):
        print(f"  {k:12s} {shares.get(k, 0):5.1f}%   ({tok[k]:,} tok)")
    basis_note = ("dollar shares across known-price models"
                  if usd_known > 0 else "price-ratio weighted (no known prices)")
    print(f"  (shares: {basis_note})")
    print(f"\n  -> context (input + cache write + cache read): {ctx_share:.1f}%"
          f"   output: {shares.get('output', 0):.1f}%")
    print("     If context dwarfs output, start there - but measure, don't assume.")
    print(f"  $ estimate (repriced at {today} list prices): ${usd_known:,.2f}"
          f" covering {coverage:.1f}% of usage.")
    print("     Excludes batch/fast/geo modifiers and server-tool fees;"
          " not a subscription bill.")
    if scope_req["subagents"]:
        print(f"  Subagents: {scope_req['subagents']:,} requests, "
              f"{scope_tok['subagents']:,} raw tokens (own context windows).")

    print("\n=== CONTEXT PER REQUEST (re-processed on later turns) ===")
    c = summary["context_per_request"]
    print(f"  median {c['median']:>10,}   p90 {c['p90']:>10,}   max {c['max']:>10,}")

    print("\n=== ESTIMATED ONE-TIME TOOL PAYLOAD (text chars/4; images official"
          " resize formula) ===")
    grand = sum(tool_total.values()) or 1
    for n, v in top:
        print(f"  {str(n)[:36]:37s} {v:10,d} ({100*v/grand:4.1f}%)"
              f"  text {tool_text.get(n, 0):>9,}  img {tool_img[n]:>8,}"
              f" ({tool_imgcnt[n]:4d})  {tool_cnt[n]:5d}x")
    if unknown_dims:
        print(f"  ({unknown_dims} images had unreadable dimensions and were"
              " counted at the tier maximum)")
    if tier_fallbacks:
        print(f"  ({tier_fallbacks} images had no receiving request; issuer"
              " tier assumed)")
    print("  One-time payload size for targeting - not lifetime context"
          " exposure, and not billed truth (that is the usage block above).")

    if behavior:
        print("\n=== BROWSER ACTION SIGNALS (from deduplicated tool inputs;"
              " not a measured loop) ===")
        print(f"  screenshots requested : {behavior['screenshots']:,}")
        print(f"  coordinate actions    : {behavior['coordinate_actions']:,}")
        print(f"  ref-based actions     : {behavior['ref_actions']:,}")
        print("  Many coordinate actions alongside many screenshots usually"
              " indicate a confirm-by-screenshot pattern; prefer refs from a"
              " page read.")
    print()


if __name__ == "__main__":
    main()
