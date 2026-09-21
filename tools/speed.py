"""Read `calls.jsonl` and answer "is it slow, and whose fault is it".

The plugin writes one line per attempt; this turns the pile into the four
numbers that settle an argument:

* how long until the answer starts, per model — the provider's speed;
* how much of a turn went on waiting for a key — the quota's cost;
* how often an attempt is thrown away, and for what;
* whether the first-token wait is cutting calls that would have answered.

Read-only. Takes no argument in the common case; pass a path to read a copy
taken off another machine, or `--since HH:MM` to look at one stretch.

    python tools/speed.py
    python tools/speed.py --since 12:40
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "hermes" / "plugin-data" / "hermes-kame-api-rotation" / "calls.jsonl"
)

out = sys.stdout.buffer


def say(text: str = "") -> None:
    out.write((text + "\n").encode("utf-8"))


def load(path: Path, since: Optional[float]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if since is not None and (row.get("at") or 0) < since:
            continue
        rows.append(row)
    return rows


def _stat(values: List[float]) -> str:
    """Median and worst. The mean hides exactly the case being looked for."""
    if not values:
        return "     —          —"
    ordered = sorted(values)
    return "%7.1fs  %8.1fs" % (
        statistics.median(ordered) / 1000.0,
        ordered[-1] / 1000.0,
    )


def report(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        say("nada gravado ainda — use o Hermes um pouco e rode de novo.")
        return

    first, last = rows[0].get("at") or 0, rows[-1].get("at") or 0
    say("%d tentativas   %s -> %s" % (
        len(rows),
        time.strftime("%d/%m %H:%M:%S", time.localtime(first)),
        time.strftime("%H:%M:%S", time.localtime(last)),
    ))
    say()

    models: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        models.setdefault(str(row.get("identity") or "?"), []).append(row)

    say("VELOCIDADE — quanto o provedor demora para comecar a responder")
    say("%-28s %6s %8s %10s %9s %10s" % (
        "modelo", "resp.", "1o sinal", "1a palavra", "pior 1a", "total"))
    for name, group in sorted(models.items()):
        answered = [r for r in group if r.get("outcome") == "answered"]
        sign = [r["ms_to_first_sign"] for r in answered if r.get("ms_to_first_sign") is not None]
        text = [r["ms_to_first_text"] for r in answered if r.get("ms_to_first_text") is not None]
        total = [r["ms_total"] for r in answered if r.get("ms_total") is not None]
        say("%-28s %6d %8s %10s %9s %10s" % (
            name[:28],
            len(answered),
            ("%.1fs" % (statistics.median(sign) / 1000.0)) if sign else "—",
            ("%.1fs" % (statistics.median(text) / 1000.0)) if text else "—",
            ("%.1fs" % (max(text) / 1000.0)) if text else "—",
            ("%.1fs" % (statistics.median(total) / 1000.0)) if total else "—",
        ))
    say()

    say("CULPA — do tempo de um turno, quanto foi esperar chave")
    say("%-28s %10s %12s %10s" % ("modelo", "turnos", "esperando", "provedor"))
    for name, group in sorted(models.items()):
        answered = [r for r in group if r.get("outcome") == "answered"]
        waited = [r.get("ms_waited_before") or 0 for r in answered]
        working = [r.get("ms_total") or 0 for r in answered]
        if not answered:
            continue
        total = sum(waited) + sum(working)
        share = (sum(waited) / total * 100.0) if total else 0.0
        say("%-28s %10d %11.0f%% %9.0f%%" % (
            name[:28], len(answered), share, 100.0 - share))
    say()

    say("DESPERDICIO — tentativas que nao viraram resposta")
    kinds: Dict[str, int] = {}
    cut = 0
    for row in rows:
        if row.get("outcome") == "answered":
            continue
        kinds[str(row.get("kind") or row.get("outcome") or "?")] = kinds.get(
            str(row.get("kind") or row.get("outcome") or "?"), 0) + 1
        if (row.get("chars_seen") or 0) > 0:
            cut += 1
    for name, count in sorted(kinds.items(), key=lambda kv: -kv[1]):
        say("  %-24s %5d" % (name, count))
    if cut:
        say("  %-24s %5d  (ja tinham escrito algo na tela)" % ("cortadas no meio", cut))
    say()

    say("O CORTE — chamadas largadas antes de responderem")
    timeouts = [r for r in rows if str(r.get("kind")) == "timeout"]
    if not timeouts:
        say("  nenhuma. o first token wait nao cortou nada nesta janela.")
    else:
        lived = [r["ms_total"] for r in timeouts if r.get("ms_total") is not None]
        answered_text = [
            r["ms_to_first_text"] for r in rows
            if r.get("outcome") == "answered" and r.get("ms_to_first_text") is not None
        ]
        say("  %d cortadas, viveram %s (mediana / pior)" % (len(timeouts), _stat(lived)))
        if answered_text:
            slower = [v for v in answered_text if lived and v > max(lived)]
            say("  %d respostas que deram certo demoraram MAIS que a mais longa cortada."
                % len(slower))
            say("  se esse numero for alto, o first token wait esta curto demais.")


def main() -> int:
    args = sys.argv[1:]
    since = None
    path = DEFAULT
    while args:
        item = args.pop(0)
        if item == "--since" and args:
            clock = args.pop(0)
            today = time.localtime()
            try:
                hour, minute = (int(part) for part in clock.split(":"))
            except ValueError:
                say("--since quer HH:MM")
                return 2
            since = time.mktime((
                today.tm_year, today.tm_mon, today.tm_mday, hour, minute, 0, 0, 0, -1
            ))
        else:
            path = Path(item)

    say("arquivo: %s" % path)
    say()
    report(load(path, since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
