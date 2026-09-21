"""Bounded Gemini readiness observations; never imports or edits Hermes.

Credentials stay in memory, from the existing home .env. No model is used to
analyze responses. A successful request tests readiness, not answer quality.
This is NOT a benchmark of KAME's end-to-end dispatch or proof of quota cost.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def redact(value, secrets):
    text = json.dumps(value, ensure_ascii=False)
    for secret in secrets:
        text = text.replace(secret, "[KEY]")
    text = re.sub(r"AIza[\w-]{20,}", "[KEY]", text)
    return json.loads(text)


def retry_seconds(body, headers):
    """Only explicit short waits; daily evidence is handled separately."""
    candidates = []
    raw = headers.get("Retry-After")
    if raw:
        try:
            candidates.append(float(raw))
        except (TypeError, ValueError):
            pass
    error = body.get("error", {}) if isinstance(body, dict) else {}
    for detail in error.get("details", []) if isinstance(error, dict) else []:
        if not isinstance(detail, dict):
            continue
        match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s)", str(detail.get("retryDelay", "")))
        if match:
            candidates.append(float(match[1]) / (1000 if match[2] == "ms" else 1))
    return max([20.0] + [x for x in candidates if 0 <= x <= 86400])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-requests", type=int, default=28)
    parser.add_argument("--per-key", type=int, default=2)
    parser.add_argument("--max-seconds", type=int, default=240)
    args = parser.parse_args()
    if not re.fullmatch(r"gemini-[A-Za-z0-9_.-]+", args.model):
        parser.error("Expected an explicit Gemini model id")
    if not (1 <= args.max_requests <= 56 and 1 <= args.per_key <= 4 and 1 <= args.max_seconds <= 900):
        parser.error("Budget exceeds this collector's bounded pilot limits")
    from dotenv import dotenv_values
    env = dotenv_values(args.home / ".env", interpolate=False)
    keys = list(dict.fromkeys(k.strip() for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY")
                             for k in (env.get(name) or "").split(",") if k.strip()))
    if not keys or any(not k.startswith("AIza") for k in keys):
        raise SystemExit("Expected configured Gemini API keys; values not printed")
    args.out.mkdir(parents=True, exist_ok=False)
    summary = {"model": args.model, "keys": len(keys), "mode": "live" if args.live else "dry-run",
               "started_at": datetime.now(timezone.utc).isoformat(), "requests": 0,
               "max_requests": args.max_requests, "per_key": args.per_key,
               "max_seconds": args.max_seconds, "status": "running", "quota_cost": "not measured",
               "scope": "direct Gemini readiness only; not KAME integration"}
    def save():
        (args.out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    save()
    if not args.live:
        summary["status"] = "dry-run-complete"
        save()
        print(json.dumps(summary))
        return
    import requests
    started = time.monotonic()
    due = [started] * len(keys)
    counts = [0] * len(keys)
    outcomes = Counter()
    try:
        with requests.Session() as session:
            # Disable implicit network retries; every POST is counted here.
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            while summary["requests"] < args.max_requests and time.monotonic() - started < args.max_seconds:
                if (args.out / "STOP").exists():
                    summary["status"] = "stopped-by-file"
                    break
                available = [i for i in range(len(keys)) if counts[i] < args.per_key and due[i] != float("inf")]
                if not available:
                    break
                i = min(available, key=lambda k: (due[k], counts[k], k))
                now = time.monotonic()
                if due[i] > now:
                    time.sleep(min(1.0, due[i] - now))
                    continue
                counts[i] += 1
                summary["requests"] += 1
                record = {"at": datetime.now(timezone.utc).isoformat(), "key_alias": f"K{i+1:02d}",
                          "attempt_on_key": counts[i], "model": args.model}
                began = time.monotonic()
                try:
                    response = session.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{args.model}:generateContent",
                        headers={"x-goog-api-key": keys[i]},
                        json={"contents": [{"role": "user", "parts": [{"text": "Reply with OK."}]}],
                              "generationConfig": {"maxOutputTokens": 32}},
                        timeout=(10, 30), allow_redirects=False,
                    )
                    try:
                        body = response.json()
                    except ValueError:
                        body = {"unparsed_error": response.text[:2000]} if response.status_code >= 400 else {}
                    record.update(status=response.status_code, duration_ms=round((time.monotonic()-began)*1000),
                                  headers={k:v for k,v in response.headers.items()
                                           if k.lower() in {"retry-after", "date", "x-request-id"}},
                                  usage=body.get("usageMetadata", {}) if isinstance(body, dict) else {})
                    if response.status_code >= 400:
                        record["error"] = redact(body, keys)
                    outcomes[str(response.status_code)] += 1
                    daily = "PerDay" in json.dumps(body) or "per_day" in json.dumps(body)
                    if response.status_code in (400, 401, 403, 404) or daily:
                        due[i] = float("inf")
                        record["next_action"] = "no more attempts on this key in this pilot"
                    elif response.status_code == 429 or response.status_code >= 500:
                        wait = retry_seconds(body, response.headers)
                        due[i] = time.monotonic() + wait
                        record["next_not_before_seconds"] = wait
                    elif 200 <= response.status_code < 300:
                        due[i] = time.monotonic() + 20
                    else:
                        due[i] = float("inf")
                except requests.RequestException as exc:
                    record.update(status="transport_error", exception_type=type(exc).__name__,
                                  duration_ms=round((time.monotonic()-began)*1000))
                    # A timeout does not prove that the server did no work.
                    due[i] = float("inf")
                    outcomes["transport_error"] += 1
                with (args.out / "observations.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                summary["outcomes"] = dict(outcomes)
                save()
                time.sleep(1)
        if summary["status"] == "running":
            summary["status"] = "completed-budget-or-eligibility"
    except KeyboardInterrupt:
        summary["status"] = "interrupted"
    except Exception as exc:
        summary["status"] = "failed"
        summary["exception_type"] = type(exc).__name__
    finally:
        summary["ended_at"] = datetime.now(timezone.utc).isoformat()
        summary["elapsed_seconds"] = round(time.monotonic() - started, 2)
        save()
        print(json.dumps(summary))


if __name__ == "__main__":
    main()
