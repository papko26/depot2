"""The effort unit, everywhere: MTok, printed as a share of a usage window.

- **MTok** is millions of price-weighted input-equivalent tokens: input ×1, cache write ×1.25, cache read ×0.1,
  output ×5. It is what the depot stores, and every limit of the config is in it.
- **%UW** is MTok / `usage.mtok_per_pct`: the share of the usage window the project budgets with. A car's size
  (`usage.sizes`: doc, S, M, L) is in %UW.
- A call that reports a cost but no tokens is converted at `usage.usd_per_mtok`.
"""

import json
import math
import re

from .i18n import t

WEIGHTS = {"input": 1.0, "cache_write": 1.25, "cache_read": 0.1, "output": 5.0}
API_KEYS = {"input_tokens": "input", "cache_creation_input_tokens": "cache_write",
            "cache_read_input_tokens": "cache_read", "output_tokens": "output"}


class EffortError(ValueError):
    pass


def per_pct(cfg):
    return float(cfg["usage"]["mtok_per_pct"])


def usd_per_mtok(cfg):
    return float(cfg["usage"]["usd_per_mtok"])


def to_usd(mtok, cfg):
    """MTok -> the CLI's ≈$ (`--max-budget-usd` takes only $)."""
    return round(float(mtok) * usd_per_mtok(cfg), 6)


def from_usd(usd, cfg):
    """The CLI's ≈$ -> MTok, at usage.usd_per_mtok."""
    return float(usd) / usd_per_mtok(cfg)


def sizes(cfg):
    return cfg["usage"]["sizes"]


def weighted_mtok(tokens):
    """{"input", "cache_write", "cache_read", "output"} token counts -> MTok (input-equivalent)."""
    return sum((tokens.get(k) or 0) * w for k, w in WEIGHTS.items()) / 1e6


def call_mtok(res, cfg):
    """One call's spend in MTok: its own token usage, weighted (a killed call's is summed from the
    stream). A call that reports no tokens but a cost: that ≈$ / usage.usd_per_mtok. Else 0."""
    m = weighted_mtok((res or {}).get("usage") or {})
    if m > 0:
        return m
    usd = (res or {}).get("cost_usd")
    return from_usd(usd, cfg) if usd else 0.0


def by_cost_only(res):
    """True when a call's MTok came from its ≈$, not from its tokens."""
    return weighted_mtok((res or {}).get("usage") or {}) <= 0 and bool((res or {}).get("cost_usd"))


def costs_mtok(costs, cfg):
    """A run's spend in MTok from its costs.json (a round's or a reflection's): its own "mtok"; for a
    file from before it, the sum over its calls, else its token counts, else total_usd / usd_per_mtok."""
    c = costs or {}
    if c.get("mtok") is not None:
        return float(c["mtok"])
    if c.get("items"):
        return sum(call_mtok(x, cfg) for x in c["items"])
    if isinstance(c.get("tokens"), dict) and weighted_mtok(c["tokens"]) > 0:
        return weighted_mtok(c["tokens"])
    return from_usd(c["total_usd"], cfg) if c.get("total_usd") else 0.0


def from_transcript(path):
    """It sums the per-message `usage` with the weights, deduped by message id: a streamed message is
    logged once per content block with the same usage."""
    seen = set()
    tok = {k: 0 for k in WEIGHTS}
    n = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"usage"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            msg = e.get("message") if isinstance(e, dict) else None
            u = msg.get("usage") if isinstance(msg, dict) else None
            if not isinstance(u, dict):
                continue
            mid = msg.get("id") or e.get("requestId") or e.get("uuid")
            if mid is not None and mid in seen:
                continue
            seen.add(mid)
            n += 1
            for api, k in API_KEYS.items():
                tok[k] += u.get(api) or 0
    if not n:
        raise EffortError("no usage in %s" % path)
    return round(weighted_mtok(tok), 4)


def size_mtok(size, cfg):
    if size not in sizes(cfg):
        raise EffortError("a size is one of %s (usage.sizes, in %%UW)" % ", ".join(sorted(sizes(cfg))))
    return float(sizes(cfg)[size]) * per_pct(cfg)


def check_size(size, cfg):
    if size is not None:
        size_mtok(size, cfg)
    return size


def parse_mtok(text):
    """'12', '12.5', '10-13' / '10–13' (a range) -> 12.0 / [10.0, 13.0]; None/'none' -> None."""
    if text is None or isinstance(text, (int, float)):
        return None if text is None else float(text)
    if isinstance(text, (list, tuple)):
        return [float(text[0]), float(text[1])]
    t = str(text).strip().replace(",", ".")
    if t.lower() in ("", "none", "-", "—"):
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)$", t)
    if m:
        return [float(m.group(1)), float(m.group(2))]
    try:
        return float(t)
    except ValueError:
        raise EffortError("an effort is a number of MTok or a range like 10-13, got %r" % text)


def est_from(cfg, pct=None, mtok=None, allow_range=False):
    if pct is not None and mtok is not None:
        raise EffortError("give --est-pct or --est-mtok, not both")
    v = parse_mtok(pct) if pct is not None else parse_mtok(mtok)
    if v is not None and pct is not None:
        v = [round(x * per_pct(cfg), 4) for x in v] if isinstance(v, list) else round(v * per_pct(cfg), 4)
    if isinstance(v, list) and not allow_range:
        raise EffortError("a range is for a train's own estimate; a car or an item gets one number")
    return v


def _num(x, nd):
    return ("%.*f" % (nd, x)).rstrip("0").rstrip(".") or "0"


def _small(x, nd):
    """nd decimals; below 0.1, two significant digits, so a call's cap never prints as 0."""
    a = abs(x)
    if 0 < a < 0.1:
        nd = max(nd, min(8, 1 - int(math.floor(math.log10(a)))))
    return _num(x, nd)


def fmt(mtok, cfg):
    if mtok is None:
        return "—"
    k = per_pct(cfg)
    if isinstance(mtok, (list, tuple)):
        lo, hi = mtok
        return "%s–%s%%UW (%s–%s MTok)" % (_small(lo / k, 2), _small(hi / k, 2), _mt(lo), _mt(hi))
    return "%s%%UW (%s MTok)" % (_small(mtok / k, 2), _mt(mtok))


def share(u, cfg):
    return fmt(float(u) * 100 * per_pct(cfg), cfg)


def _mt(x):
    return _small(x, 1 if abs(x) >= 10 else 2)


def cell(expected, spent, cfg):
    """The expected / spent cell of every table."""
    if expected is None and spent is None:
        return "—"
    return "%s / %s" % (("≈ " + fmt(expected, cfg)) if expected is not None else "—", fmt(spent, cfg))


def item_expected(obj, cfg):
    """A stack object's or a car's expected MTok: its own estimate, else its size's."""
    if obj.get("est_mtok") is not None:
        return obj["est_mtok"]
    if obj.get("size"):
        try:
            return size_mtok(obj["size"], cfg)
        except EffortError:
            return None
    return None


def legend(cfg):
    s = ", ".join("%s %s" % (k, _num(v, 2)) for k, v in sorted(sizes(cfg).items(), key=lambda x: x[1]))
    return t("effort.legend", per=_num(per_pct(cfg), 2), sizes=s)
