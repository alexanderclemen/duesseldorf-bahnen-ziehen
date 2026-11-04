#!/usr/bin/env python3
"""Fetch the Belegungsplan of all Düsseldorf public pools and write plan.json.

Source: the public lane plans of the Bädergesellschaft Düsseldorf
(https://www.baeder-duesseldorf.de/belegungsplan/). One request to the site's
SvelteKit data endpoint returns every location and pool for today and the next
two days, in 15-minute slots per lane.

Each day's plan is also kept in STATE_DIRECTORY/history (default /var/lib/baeder),
and the rest of the week (days 4 to 7) is filled with a forecast: the average of
the same weekday in the last weeks. Only the Python standard library is used.

Usage: fetch_baeder.py [OUTPUT_FILE]   (default /var/www/baeder/plan.json)
"""
import json
import os
import sys
import tempfile
import urllib.request
from datetime import date, datetime, timedelta, timezone

URL = "https://www.baeder-duesseldorf.de/belegungsplan/muenstertherme/__data.json"
PAGE = "https://www.baeder-duesseldorf.de/belegungsplan/{slug}/"
USER_AGENT = "duesseldorf-bahnen-ziehen/1.0 (+https://github.com/alexanderclemen/duesseldorf-bahnen-ziehen; every 30 min)"
STATE = os.environ.get("STATE_DIRECTORY", "/var/lib/baeder").split(":")[0]
WEEK = 7            # days shown
SAMPLES = 6         # past weeks used for a forecast
KEEP_DAYS = 400     # history older than this is deleted
WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
LANE_POOL_TYPE = 7  # beckentyp of the sports / swimmer pools (the others are course, leisure, spa pools)


def unflatten(arr):
    """Decode SvelteKit's 'devalue' format: a flat list where ints are references."""
    special = {-1: None, -2: None, -3: float("nan"), -4: float("inf"),
               -5: float("-inf"), -6: -0.0}
    cache = {}

    def get(i):
        if i in special:
            return special[i]
        if i in cache:
            return cache[i]
        v = arr[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str):  # typed values like ["Date", "..."]
                cache[i] = v[1] if len(v) > 1 else None
                return cache[i]
            out = []
            cache[i] = out
            out.extend(get(x) for x in v)
            return out
        if isinstance(v, dict):
            out = {}
            cache[i] = out
            for k, x in v.items():
                out[k] = get(x)
            return out
        cache[i] = v
        return v

    return get(0)


def find_standorte(obj):
    """Search the decoded page data for the list of locations."""
    if isinstance(obj, dict):
        if isinstance(obj.get("standorte"), list):
            return obj["standorte"]
        for v in obj.values():
            r = find_standorte(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_standorte(v)
            if r is not None:
                return r
    return None


def mode_of(note):
    note = (note or "").lower()
    if "50" in note:
        return "50"
    if "25" in note:
        return "25"
    return None


def is_open_status(status):
    return str(status or "").lower().startswith("geöffnet")


def to_min(t):
    return int(t[:2]) * 60 + int(t[3:5])


def convert_pool(standort, becken, multi):
    slug = standort["standort"]["slug"]
    days = []
    for d in becken["days"]:
        periods = [{"start": p["start"], "end": p["end"],
                    "mode": mode_of(p.get("anmerkung")), "note": p.get("anmerkung"),
                    "open": is_open_status(p.get("status"))}
                   for p in (d.get("open_periods") or [])]
        # some pools publish no periods, only the day's opening hours
        if not periods and is_open_status(d.get("status")) and d.get("open_start") and d.get("open_end"):
            periods = [{"start": d["open_start"], "end": d["open_end"], "mode": None,
                        "note": None, "open": True}]
        slots = []
        for s in d["slots"]:
            lanes = []
            for lane in sorted(s["bahnen"], key=lambda x: x["pos"]):
                lanes.append({"name": lane.get("booking") or "belegt", "cat": lane.get("kategorie")}
                             if lane["belegt"] else None)
            slots.append({"t": s["start"], "lanes": lanes})
        days.append({"date": d["date"], "label": d["label"], "status": d.get("status"),
                     "periods": periods, "slots": slots})
    site = standort["standort"]["name"].strip()
    pool = " ".join(becken["name"].split())
    return {
        "id": f"{slug}:{becken['id']}", "slug": slug, "becken_id": becken["id"],
        "site": site, "pool": pool,
        "name": f"{site} · {pool}" if multi else site,
        "short": pool if multi else site,
        "type": becken.get("beckentyp_id"),
        "lanes": becken.get("bahnen_count") or 0,
        "hinweis": becken.get("hinweis"), "closed_dates": standort.get("closed_dates") or [],
        "url": PAGE.format(slug=slug), "days": days,
    }


def lane_pool_defaults(pools):
    """Default choice: the sports pools, plus the main pool of sites that have none."""
    chosen = {p["id"] for p in pools if p["type"] == LANE_POOL_TYPE}
    by_site = {}
    for p in pools:
        by_site.setdefault(p["slug"], []).append(p)
    for site_pools in by_site.values():
        if not any(p["id"] in chosen for p in site_pools):
            chosen.add(max(site_pools, key=lambda p: p["lanes"])["id"])
    return chosen


def write_json(path, data):
    """Write atomically, so no reader ever sees a half-written file."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def save_history(pools):
    """One file per date; the last fetch of a day wins (it knows the latest changes)."""
    hist = os.path.join(STATE, "history")
    by_date = {}
    for p in pools:
        for d in p["days"]:
            by_date.setdefault(d["date"], {})[p["id"]] = {
                "status": d["status"], "periods": d["periods"], "lanes": p["lanes"],
                "slots": [{"t": s["t"], "n": len(s["lanes"]) or p["lanes"],
                           "free": sum(1 for x in s["lanes"] if x is None),
                           "busy": sorted({x["name"] for x in s["lanes"] if x})}
                          for s in d["slots"]]}
    for day, entry in by_date.items():
        path = os.path.join(hist, day + ".json")
        old = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
        old.update(entry)
        write_json(path, old)
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    for name in os.listdir(hist):
        if name.endswith(".json") and name[:-5] < cutoff:
            os.remove(os.path.join(hist, name))


def slot_open(entry, t):
    if not is_open_status(entry.get("status")):
        return False
    m = to_min(t)
    return any(p["open"] and to_min(p["start"]) <= m < to_min(p["end"]) for p in entry["periods"])


def forecast_day(pool, target):
    """Average of the same weekday in the last SAMPLES weeks."""
    hist = os.path.join(STATE, "history")
    past = []
    for k in range(1, 60):
        path = os.path.join(hist, (target - timedelta(weeks=k)).isoformat() + ".json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                day = json.load(f)
            e = day.get(pool["id"]) or day.get(pool["slug"])  # older history was keyed by site
            if e:
                past.append(e)
        if len(past) >= SAMPLES:
            break
    out = {"date": target.isoformat(), "label": f"{WD[target.weekday()]}, {target.strftime('%d.%m.%Y')}",
           "forecast": True, "samples": len(past), "status": "geschlossen", "periods": [], "slots": []}
    if not past:
        return out
    latest = past[0]                     # opening periods: as on the most recent same weekday
    out["periods"], out["status"] = latest["periods"], latest["status"]
    n = latest["lanes"] or pool["lanes"]
    for t in sorted({s["t"] for e in past for s in e["slots"]}):
        vals, names = [], {}
        for e in past:
            s = next((x for x in e["slots"] if x["t"] == t), None)
            if s and slot_open(e, t):
                vals.append(s["free"] / s["n"])
                for name in s["busy"]:
                    names[name] = names.get(name, 0) + 1
        if not vals:
            out["slots"].append({"t": t, "n": n, "free": None, "often": []})
            continue
        often = [k for k, c in sorted(names.items(), key=lambda x: -x[1]) if c * 2 >= len(vals)]
        out["slots"].append({"t": t, "n": n, "free": round(sum(vals) / len(vals) * n, 2),
                             "often": often[:4], "seen": len(vals)})
    return out


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "/var/www/baeder/plan.json"
    req = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = json.load(r)
    nodes = [unflatten(n["data"]) for n in raw.get("nodes", [])
             if isinstance(n, dict) and n.get("type") == "data"]
    standorte = find_standorte(nodes)
    if not standorte:
        sys.exit("no 'standorte' in the response - the website changed")

    pools = []
    for st in standorte:
        becken = st.get("becken") or []
        for b in becken:
            if b.get("days"):
                pools.append(convert_pool(st, b, len(becken) > 1))
    if not pools:
        sys.exit("no pools in the response")
    defaults = lane_pool_defaults(pools)
    for p in pools:
        p["default"] = p["id"] in defaults

    save_history(pools)

    # the rest of the week from history
    first = min(date.fromisoformat(d["date"]) for p in pools for d in p["days"])
    n_fc = 0
    for p in pools:
        have = {d["date"] for d in p["days"]}
        for k in range(WEEK):
            day = first + timedelta(days=k)
            if day.isoformat() not in have:
                fc = forecast_day(p, day)
                n_fc += 1 if fc["samples"] else 0
                p["days"].append(fc)
        p["days"].sort(key=lambda d: d["date"])

    data = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "https://www.baeder-duesseldorf.de/belegungsplan/", "pools": pools}
    write_json(out_path, data)
    print(f"ok: {len(pools)} pools at {len({p['slug'] for p in pools})} sites, "
          f"{n_fc} pool-days forecast -> {out_path}")


if __name__ == "__main__":
    main()
