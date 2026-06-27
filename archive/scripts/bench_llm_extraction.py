"""
BENCH (throwaway) — measure local-LLM news-extraction throughput on THIS machine.

Replaces the back-of-envelope tok/s estimates with measured numbers from
Ollama's API (which returns exact prompt_eval / eval token counts + durations).

Two modes:

  1. Fetch real full article bodies from IBKR (needs Gateway up) into a JSON
     cache.  Our spike CSV only kept 200-char excerpts, too short to benchmark.

         .venv/Scripts/python scripts/bench_llm_extraction.py --fetch --n 10

  2. Benchmark an Ollama model against the cached bodies (no Gateway needed):

         .venv/Scripts/python scripts/bench_llm_extraction.py --run llama3.2:3b
         .venv/Scripts/python scripts/bench_llm_extraction.py --run llama3.1:8b --num-predict 300

Output: per-article latency, measured prefill & decode tok/s, and an
extrapolation to daily wall-clock at 100/day (typical) and 250/day (heavy),
plus the one-time 30-day backfill (~1,360 articles).

Ollama server must be running (it starts with the desktop app).  Talks to
http://localhost:11434 via stdlib urllib (no extra deps).
"""

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median, mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config  # noqa: E402

BODIES_PATH = Path(__file__).resolve().parent.parent / "logs" / "bench_bodies.json"
OLLAMA_URL = "http://localhost:11434/api/generate"

# Realistic extraction prompt — structured JSON, the shape a shadow-mode
# extraction layer would actually request.  Kept terse so output tokens
# (the decode bottleneck) reflect real usage, not an essay.
EXTRACT_PROMPT = """You are a financial news analyst. Read the article below and return ONLY a JSON object with these keys:
- event_type: one of [earnings, guidance, mgmt_change, mna, litigation, regulatory, product, analyst, macro, other]
- direction: one of [bullish, bearish, neutral]
- magnitude: integer 1-5 (materiality to the stock)
- time_horizon: one of [immediate, days, quarter, longterm]
- novelty: integer 1-5 (how new/surprising vs already-known)
- entities: list of company/person names mentioned
- summary: one sentence, max 25 words

Article:
\"\"\"
{body}
\"\"\"

JSON:"""

DEFAULT_FETCH_SYMBOLS = ["AAPL", "NVDA", "MU", "JPM", "XOM", "SNOW", "MRVL", "WMT", "ORCL", "META"]


# ── fetch mode ───────────────────────────────────────────────────────────────

def fetch_bodies(n: int, days: int, port: int, client_id: int) -> None:
    import asyncio

    async def _go():
        from ib_insync import IB, Stock, util
        util.logToConsole(level=40)
        ib = IB()
        print(f"Connecting to IBKR on {config.ibkr.host}:{port} ...")
        await ib.connectAsync(config.ibkr.host, port, clientId=client_id, timeout=10)
        print("Connected.")

        providers = await ib.reqNewsProvidersAsync()
        provider_codes = "+".join(p.code for p in providers)
        end_dt = datetime.now(timezone.utc)
        start_str = (end_dt - timedelta(days=days)).strftime("%Y%m%d %H:%M:%S")
        end_str = end_dt.strftime("%Y%m%d %H:%M:%S")

        bodies = []
        for sym in DEFAULT_FETCH_SYMBOLS:
            if len(bodies) >= n:
                break
            try:
                details = await ib.reqContractDetailsAsync(Stock(sym, "SMART", "USD"))
                if not details:
                    continue
                con_id = details[0].contract.conId
                heads = await ib.reqHistoricalNewsAsync(
                    conId=con_id, providerCodes=provider_codes,
                    startDateTime=start_str, endDateTime=end_str, totalResults=8)
            except Exception as exc:
                print(f"  [{sym}] headlines failed: {exc}")
                continue

            for h in heads:
                if len(bodies) >= n:
                    break
                try:
                    art = await ib.reqNewsArticleAsync(
                        providerCode=getattr(h, "providerCode", ""),
                        articleId=getattr(h, "articleId", ""))
                    text = getattr(art, "articleText", "") or ""
                    # only keep substantive bodies (mirror the spike's 'full' floor)
                    if len(text) >= 800:
                        bodies.append({
                            "symbol": sym,
                            "provider": getattr(h, "providerCode", ""),
                            "headline": getattr(h, "headline", "")[:160],
                            "chars": len(text),
                            "body": text,
                        })
                        print(f"  [{sym}] kept body ({len(text)} chars)")
                except Exception:
                    pass

        ib.disconnect()
        BODIES_PATH.parent.mkdir(exist_ok=True)
        BODIES_PATH.write_text(json.dumps(bodies, ensure_ascii=False, indent=2), encoding="utf-8")
        lens = [b["chars"] for b in bodies]
        print(f"\nSaved {len(bodies)} bodies to {BODIES_PATH}")
        if lens:
            print(f"  char length: min={min(lens)} median={int(median(lens))} max={max(lens)}")

    asyncio.run(_go())


# ── run mode ─────────────────────────────────────────────────────────────────

def _ollama_generate(model: str, prompt: str, num_predict: int) -> dict:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": num_predict, "temperature": 0},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1200) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fmt_hrs(seconds: float) -> str:
    h = seconds / 3600.0
    if h >= 1:
        return f"{h:.1f} hr"
    return f"{seconds/60.0:.1f} min"


def run_bench(model: str, num_predict: int, warmup: bool) -> None:
    if not BODIES_PATH.exists():
        print(f"No cached bodies at {BODIES_PATH}. Run with --fetch first.")
        sys.exit(1)
    bodies = json.loads(BODIES_PATH.read_text(encoding="utf-8"))
    if not bodies:
        print("Body cache is empty.")
        sys.exit(1)

    print(f"Model: {model}  |  bodies: {len(bodies)}  |  num_predict cap: {num_predict}")
    print(f"Ollama: {OLLAMA_URL}\n")

    if warmup:
        # First call also loads the model into RAM — time it separately so it
        # doesn't pollute the per-article steady-state numbers.
        print("Warmup (includes model load into RAM) ...")
        t0 = time.time()
        try:
            _ollama_generate(model, "Reply with the single word: ok", 5)
        except Exception as exc:
            print(f"ERROR talking to Ollama: {exc}")
            print("Is the model pulled and the server running?")
            sys.exit(1)
        print(f"  warmup/load took {time.time()-t0:.1f}s\n")

    prefill_rates, decode_rates, latencies = [], [], []
    in_toks, out_toks = [], []

    for i, b in enumerate(bodies):
        prompt = EXTRACT_PROMPT.format(body=b["body"])
        t0 = time.time()
        try:
            r = _ollama_generate(model, prompt, num_predict)
        except Exception as exc:
            print(f"  [{i+1}] ERROR: {exc}")
            continue
        wall = time.time() - t0

        pe_n = r.get("prompt_eval_count", 0)
        pe_d = r.get("prompt_eval_duration", 0) / 1e9
        ev_n = r.get("eval_count", 0)
        ev_d = r.get("eval_duration", 0) / 1e9
        prefill = pe_n / pe_d if pe_d else 0
        decode = ev_n / ev_d if ev_d else 0

        prefill_rates.append(prefill)
        decode_rates.append(decode)
        latencies.append(wall)
        in_toks.append(pe_n)
        out_toks.append(ev_n)

        print(f"  [{i+1:2d}] {b['symbol']:5s} {b['chars']:5d}ch | "
              f"in={pe_n:4d}tok out={ev_n:3d}tok | "
              f"prefill={prefill:6.1f} t/s decode={decode:5.1f} t/s | {wall:5.1f}s")

    if not latencies:
        print("\nNo successful runs.")
        return

    avg_lat = mean(latencies)
    print("\n" + "=" * 70)
    print(f"MEASURED — {model}")
    print("=" * 70)
    print(f"  input tokens   : median {int(median(in_toks))}  (mean {mean(in_toks):.0f})")
    print(f"  output tokens  : median {int(median(out_toks))}  (mean {mean(out_toks):.0f})")
    print(f"  prefill tok/s  : median {median(prefill_rates):.1f}")
    print(f"  decode  tok/s  : median {median(decode_rates):.1f}")
    print(f"  per-article    : mean {avg_lat:.1f}s  (median {median(latencies):.1f}s)")
    print("\n  EXTRAPOLATION (steady-state, model already loaded):")
    for label, count in [("typical day (100)", 100), ("heavy day  (250)", 250),
                         ("30-day backfill (1360)", 1360)]:
        print(f"    {label:24s} : {_fmt_hrs(avg_lat * count)}")
    print("\n  NOTE: real daily runs add ~one model-load per batch (see warmup),")
    print("        and a cheap triage pre-filter would cut the article count 2-5x.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fetch", action="store_true", help="Fetch full bodies from IBKR into the cache")
    p.add_argument("--run", metavar="MODEL", default=None, help="Benchmark this Ollama model")
    p.add_argument("--n", type=int, default=10, help="How many bodies to fetch (--fetch)")
    p.add_argument("--days", type=int, default=30, help="Lookback days (--fetch)")
    p.add_argument("--num-predict", type=int, default=300, help="Max output tokens (--run)")
    p.add_argument("--no-warmup", action="store_true", help="Skip the warmup/load call")
    p.add_argument("--port", type=int, default=config.ibkr.paper_port)
    p.add_argument("--client-id", type=int, default=96)
    args = p.parse_args()

    if args.fetch:
        fetch_bodies(args.n, args.days, args.port, args.client_id)
    if args.run:
        run_bench(args.run, args.num_predict, warmup=not args.no_warmup)
    if not args.fetch and not args.run:
        p.print_help()


if __name__ == "__main__":
    main()
