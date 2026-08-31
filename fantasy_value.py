#!/usr/bin/env python3
"""
fantasy_value.py

Market-implied projected fantasy points for MY roster, using The Odds API
player props converted with my league's (half-PPR) scoring.

Flow
----
  1. Load my roster from Yahoo (Team.roster + player_details). If that fails
     for ANY reason (expired token, Zscaler block on the OAuth host, offseason),
     fall back to the hardcoded DEFAULT_ROSTER below.
  2. Plan the MINIMUM set of Odds API calls: one request per unique game my
     players are in, requesting only the markets those players' positions need.
  3. Convert props -> projected fantasy points, blending per scoring component:
     use the market line where posted, else that player's own season-projection
     share for the missing component (never a silent zero).
  4. Print a table sorted by lineup slot, tagging each row's data source.

Cost model (The Odds API)
-------------------------
  * cost = (#markets) x (#regions) PER request; independent of #players/#books.
  * player props are per-event only -> one request per game (unavoidable).
  * the /events list call is FREE (no quota cost).
  We therefore: dedupe by game, union each game's needed markets, use 1 region,
  and cache results so same-week re-runs cost 0 credits.

Setup
-----
  Odds API key:
    - hardcoded default below, OR env ODDS_API_KEY, OR odds_api_key.txt.
    Free key: https://the-odds-api.com
  NOTE: api.the-odds-api.com is blocked by the corporate Zscaler proxy (returns a
    403 block page, likely a Gambling category filter). Run this OFF the corp
    network (phone hotspot / home wifi) for live odds; otherwise it falls back to
    season-projection baselines.

  pip install yahoo_fantasy_api yahoo_oauth tabulate truststore requests

Usage
-----
  python fantasy_value.py                 # Yahoo roster if possible, else default
  python fantasy_value.py --force-default # skip Yahoo, use DEFAULT_ROSTER
  python fantasy_value.py --starters      # only starting slots (skip BN) -> fewer calls
  python fantasy_value.py --refresh        # force a re-pull even if cache is current
  python fantasy_value.py --stale-ok       # if a re-pull fails, reuse last week's lines
  python fantasy_value.py --dry-run        # print the call plan + est. cost, no calls
  python fantasy_value.py --csv out.csv    # also write a CSV

Caching: the odds pull is stamped with the NFL week (rolls over Tuesday). Re-runs
within the same week reuse it for 0 credits; when the slate changes the cache is
detected as stale and auto-refreshed on the next run (a live pull, so run on the
hotspot). Use --refresh to force fresh lines mid-week (e.g. after injury news).
"""

import os, sys, re, json, time, logging, argparse, datetime

import truststore
truststore.inject_into_ssl()  # Windows CA store (Zscaler-friendly)

logging.getLogger().setLevel(logging.WARNING)
for _n in ("yahoo_oauth", "urllib3", "rauth", "yahoo_fantasy_api"):
    logging.getLogger(_n).setLevel(logging.WARNING)

import requests
from tabulate import tabulate

# --- My league / team ---
LEAGUE_KEY = "461.l.241315"
TEAM_KEY   = "461.l.241315.t.12"

# --- League scoring (reverse-engineered from Yahoo projections; half-PPR) ---
SCORING = {
    "pass_yd": 0.04,   # 1 pt / 25 yds
    "pass_td": 4.0,
    "pass_int": -1.0,
    "rush_yd": 0.1,
    "rec": 0.5,        # half-PPR
    "rec_yd": 0.1,
    "td": 6.0,         # any rush/receiving TD (anytime-TD market)
    "two_pt": 2.0,
    "fum_lost": -2.0,
}

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# DEFAULT ROSTER  (fallback when Yahoo can't be reached)
# Faithfully transcribed from the live Yahoo roster page (teams verified via web,
# incl. 2026 offseason moves). `proj` is the season Fan Pts projection; `stats`
# is the full projected stat line, used to fill any prop that a book hasn't posted.
# ---------------------------------------------------------------------------
def _stats(pass_yd=0, pass_td=0, pass_int=0, rush_att=0, rush_yd=0, rush_td=0,
           tgt=0, rec=0, rec_yd=0, rec_td=0, ret_td=0, two_pt=0, fum_lost=0):
    return dict(pass_yd=pass_yd, pass_td=pass_td, pass_int=pass_int,
                rush_att=rush_att, rush_yd=rush_yd, rush_td=rush_td,
                tgt=tgt, rec=rec, rec_yd=rec_yd, rec_td=rec_td,
                ret_td=ret_td, two_pt=two_pt, fum_lost=fum_lost)

DEFAULT_ROSTER = [
    {"name": "Dak Prescott",      "team": "DAL", "pos": "QB", "slot": "QB",    "bye": 14, "proj": 323.78,
     "stats": _stats(pass_yd=4552, pass_td=30, pass_int=10, rush_att=53, rush_yd=177, rush_td=2, two_pt=3, fum_lost=2)},
    {"name": "Saquon Barkley",    "team": "PHI", "pos": "RB", "slot": "RB",    "bye": 10, "proj": 213.80,
     "stats": _stats(rush_att=280, rush_yd=1140, rush_td=7, tgt=50, rec=37, rec_yd=273, rec_td=2, two_pt=1, fum_lost=1)},
    {"name": "Kenneth Walker III", "team": "KC", "pos": "RB", "slot": "RB",    "bye": 5,  "proj": 176.40,
     "stats": _stats(rush_att=221, rush_yd=1027, rush_td=5, tgt=36, rec=31, rec_yd=282)},
    {"name": "DeVonta Smith",     "team": "PHI", "pos": "WR", "slot": "WR",    "bye": 10, "proj": 163.30,
     "stats": _stats(tgt=113, rec=77, rec_yd=1008, rec_td=4)},
    {"name": "Garrett Wilson",    "team": "NYJ", "pos": "WR", "slot": "WR",    "bye": 13, "proj": 81.50,
     "stats": _stats(tgt=59, rec=36, rec_yd=395, rec_td=4)},
    {"name": "Mike Evans",        "team": "SF",  "pos": "WR", "slot": "WR",    "bye": 8,  "proj": 69.80,
     "stats": _stats(tgt=62, rec=30, rec_yd=368, rec_td=3)},
    {"name": "Colston Loveland",  "team": "CHI", "pos": "TE", "slot": "TE",    "bye": 10, "proj": 136.10,
     "stats": _stats(rush_att=1, rush_yd=-2, tgt=82, rec=58, rec_yd=713, rec_td=6)},
    {"name": "Rico Dowdle",       "team": "PIT", "pos": "RB", "slot": "W/R/T", "bye": 9,  "proj": 196.80,
     "stats": _stats(rush_att=236, rush_yd=1076, rush_td=6, tgt=50, rec=39, rec_yd=297, rec_td=1, fum_lost=1)},
    {"name": "RJ Harvey",         "team": "DEN", "pos": "RB", "slot": "BN",    "bye": 10, "proj": 183.10,
     "stats": _stats(rush_att=146, rush_yd=540, rush_td=7, tgt=58, rec=47, rec_yd=356, rec_td=5, fum_lost=1)},
    {"name": "Jakobi Meyers",     "team": "JAX", "pos": "WR", "slot": "BN",    "bye": 7,  "proj": 138.30,
     "stats": _stats(rush_att=5, rush_yd=13, tgt=110, rec=75, rec_yd=835, rec_td=3, fum_lost=1)},
    {"name": "Tyler Allgeier",    "team": "ARI", "pos": "RB", "slot": "BN",    "bye": 14, "proj": 116.00,
     "stats": _stats(rush_att=143, rush_yd=514, rush_td=8, tgt=16, rec=14, rec_yd=96)},
    {"name": "Caleb Douglas",     "team": "MIA", "pos": "WR", "slot": "BN",    "bye": 6,  "proj": 0.00,
     "stats": _stats()},
    {"name": "Tank Bigsby",       "team": "PHI", "pos": "RB", "slot": "BN",    "bye": 10, "proj": 52.30,
     "stats": _stats(rush_att=63, rush_yd=356, rush_td=2, tgt=4, rec=3, rec_yd=32)},
    {"name": "Tank Dell",         "team": "HOU", "pos": "WR", "slot": "IR",    "bye": 8,  "proj": 0.00,
     "stats": _stats()},
]

# Yahoo abbreviation -> Odds API full team name (used to find each player's game)
ABBR_TO_TEAM = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LAC": "Los Angeles Chargers", "LAR": "Los Angeles Rams",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}

# ---------------------------------------------------------------------------
# The Odds API
# ---------------------------------------------------------------------------
ODDS_BASE = "https://api.the-odds-api.com/v4"
NFL_KEY   = "americanfootball_nfl"
CACHE_FILE = os.path.join(HERE, "odds_cache.json")

# Only request the markets a position can actually score from (keeps cost minimal).
MARKETS_BY_POS = {
    "QB": ["player_pass_yds", "player_pass_tds", "player_rush_yds", "player_anytime_td"],
    "RB": ["player_rush_yds", "player_reception_yds", "player_receptions", "player_anytime_td"],
    "WR": ["player_reception_yds", "player_receptions", "player_anytime_td"],
    "TE": ["player_reception_yds", "player_receptions", "player_anytime_td"],
}
# Short labels for the coverage column
SHORT = {"player_pass_yds": "PaYd", "player_pass_tds": "PaTD", "player_rush_yds": "RuYd",
         "player_reception_yds": "ReYd", "player_receptions": "Rec", "player_anytime_td": "TD"}

SKIP_SLOTS = {"IR", "IR-R", "PUP", "RES", "NA", "O"}  # not startable -> no odds pull

def conf_label(c):
    """Turn a 0..1 confidence into '92% High' style text; '-' for no data."""
    if c is None:
        return "-"
    pct = round(c * 100)
    tag = "High" if c >= 0.85 else "Med" if c >= 0.5 else "Low" if c > 0 else "None"
    return f"{pct}% {tag}"

def slot_rank(slot):
    order = ["QB","RB","WR","TE","W/R/T","W/R","R/W","K","DEF","BN","IR","IR-R","PUP","RES"]
    try:
        return order.index(slot or "")
    except ValueError:
        return len(order) + 1

def norm_pos(pos):
    p = (pos or "").upper().split(",")[0].split("/")[0].strip()
    if p in ("QB", "RB", "WR", "TE"):
        return p
    if p in ("FB",):
        return "RB"
    return p or "WR"

def norm_name(name):
    n = (name or "").lower()
    n = re.sub(r"[.’']", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    n = re.sub(r"[^a-z ]", "", n)
    return re.sub(r"\s+", " ", n).strip()

def median(vals):
    vals = sorted(v for v in (vals or []) if v is not None)
    n = len(vals)
    if n == 0:
        return None
    m = n // 2
    return vals[m] if n % 2 else (vals[m - 1] + vals[m]) / 2

def american_to_prob(price):
    try:
        o = float(price)
    except (TypeError, ValueError):
        return None
    return (-o) / ((-o) + 100) if o < 0 else 100 / (o + 100)

# Key is read from (in order): env ODDS_API_KEY, then odds_api_key.txt (gitignored),
# then this default. Kept EMPTY in source so no secret is committed to the repo.
# Locally the key lives in odds_api_key.txt; on Streamlit Cloud it's in st.secrets.
ODDS_API_KEY_DEFAULT = ""

def load_odds_key():
    key = os.environ.get("ODDS_API_KEY")
    if key:
        return key.strip()
    p = os.path.join(HERE, "odds_api_key.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    return ODDS_API_KEY_DEFAULT or None

# ---------------------------------------------------------------------------
# Week-aware cache: auto-refreshes when the NFL slate rolls over (Tuesday, post-MNF)
# ---------------------------------------------------------------------------
def nfl_week_key(today=None):
    """Identifier for the current NFL scoring week. Rolls over on Tuesday."""
    today = today or datetime.date.today()
    offset = (today.weekday() - 1) % 7   # Mon=0, Tue=1 -> most recent Tuesday
    return (today - datetime.timedelta(days=offset)).isoformat()

def _human_age(ts):
    if not ts:
        return "age unknown"
    mins = (time.time() - ts) / 60
    if mins < 90:
        return f"{mins:.0f} min old"
    hrs = mins / 60
    return f"{hrs:.0f} hr old" if hrs < 48 else f"{hrs/24:.0f} days old"

def load_cache():
    """Return cache dict {week_key, fetched_at, events} or None. Legacy files
    (bare event dicts) are wrapped with week_key=None so they read as stale."""
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and "events" in data:
        return data
    return {"week_key": None, "fetched_at": None, "events": data or {}}

def save_cache(week_key, events):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"week_key": week_key, "fetched_at": time.time(), "events": events}, f)

# ---------------------------------------------------------------------------
# Call planning + fetching
# ---------------------------------------------------------------------------
def get_events(key, session):
    """List upcoming NFL games. FREE call (no quota cost)."""
    r = session.get(f"{ODDS_BASE}/sports/{NFL_KEY}/events",
                    params={"apiKey": key}, timeout=25)
    r.raise_for_status()
    return r.json()

def build_plan(roster, events, starters_only=False):
    """One entry per unique game -> the union of markets my players there need.

    Returns (plan, skipped) where plan[event_id] = {event, markets(set), players[list]}.
    """
    ev_by_team = {}
    for e in events:
        ev_by_team[e.get("home_team")] = e
        ev_by_team[e.get("away_team")] = e

    plan, skipped = {}, []
    for p in roster:
        slot = (p.get("slot") or "").upper()
        if slot in SKIP_SLOTS:
            skipped.append((p.get("name"), f"inactive ({slot})")); continue
        if starters_only and slot == "BN":
            skipped.append((p.get("name"), "bench (--starters)")); continue
        if p.get("proj") == 0:  # 0-projection players won't have props
            skipped.append((p.get("name"), "0 proj")); continue
        full = ABBR_TO_TEAM.get((p.get("team") or "").upper())
        e = ev_by_team.get(full) if full else None
        if not e:
            skipped.append((p.get("name"), f"no game this week ({p.get('team')})")); continue
        entry = plan.setdefault(e["id"], {"event": e, "markets": set(), "players": []})
        entry["markets"].update(MARKETS_BY_POS.get(norm_pos(p.get("pos")), []))
        entry["players"].append(p.get("name"))
    return plan, skipped

def plan_cost(plan):
    return sum(len(info["markets"]) for info in plan.values())  # x1 region

def print_plan(plan, skipped):
    print(f"\n[plan] {len(plan)} game call(s), estimated {plan_cost(plan)} credits "
          f"(markets x 1 region):")
    for info in plan.values():
        e = info["event"]
        mk = ",".join(sorted(SHORT[m] for m in info["markets"]))
        print(f"  - {e['away_team']} @ {e['home_team']}: {len(info['markets'])} mkts [{mk}] "
              f"<- {', '.join(info['players'])}")
    if skipped:
        print("[plan] not pulled: " + "; ".join(f"{n} ({why})" for n, why in skipped))

def fetch_props(key, plan, session):
    """Execute the plan. Returns {event_id: props_json}. Prints per-call cost."""
    out, total = {}, 0
    for eid, info in plan.items():
        markets = sorted(info["markets"])
        e = info["event"]
        try:
            rp = session.get(
                f"{ODDS_BASE}/sports/{NFL_KEY}/events/{eid}/odds",
                params={"apiKey": key, "regions": "us",
                        "markets": ",".join(markets), "oddsFormat": "american"},
                timeout=25)
            if rp.status_code == 404:
                print(f"[odds] no props posted yet: {e['away_team']} @ {e['home_team']}")
                continue
            rp.raise_for_status()
            out[eid] = rp.json()
            last = rp.headers.get("x-requests-last")
            rem = rp.headers.get("x-requests-remaining")
            total += int(last) if (last or "").isdigit() else len(markets)
            print(f"[odds] {e['away_team']} @ {e['home_team']}: cost {last}, remaining {rem}")
        except requests.RequestException as ex:
            print(f"[warn] props fetch failed for {eid}: {type(ex).__name__}", file=sys.stderr)
    print(f"[odds] credits used this pull: ~{total}")
    return out

def aggregate_props(odds_by_event):
    """Collapse all books -> {norm_name: {market: median_line, '_name': display}}."""
    raw = {}
    for props in odds_by_event.values():
        for bk in props.get("bookmakers", []):
            for m in bk.get("markets", []):
                mkey = m.get("key")
                for o in m.get("outcomes", []):
                    player = o.get("description")
                    if not player:
                        continue
                    d = raw.setdefault(norm_name(player), {"_name": player})
                    if mkey == "player_anytime_td":
                        if (o.get("name") or "").lower() != "yes":
                            continue
                        v = american_to_prob(o.get("price"))
                    else:
                        pt = o.get("point")
                        v = float(pt) if pt is not None else None
                    if v is not None:
                        d.setdefault(mkey, []).append(v)
    out = {}
    for nn, d in raw.items():
        agg = {"_name": d["_name"]}
        for mkey, vals in d.items():
            if mkey != "_name":
                agg[mkey] = median(vals)
        out[nn] = agg
    return out

# ---------------------------------------------------------------------------
# Projection: blend market lines with season-projection baseline per component
# ---------------------------------------------------------------------------
def project_player(p, odds):
    """Return (weekly_pts, source, coverage_markets, confidence).

    For each scoring component: use the posted market line if present, else that
    player's own season/17 share for the component (from stats). Never a silent 0.

    confidence (0..1) = share of the player's expected fantasy value that is
    backed by live market lines (point-weighted); falls back to the fraction of
    required markets returned when the player's stat mix is unknown.
    """
    stats = p.get("stats")
    proj = p.get("proj")

    # market_key -> (season_weekly_points_if_stats_known, line->points fn)
    def wk(x):
        return x / 17.0
    comp = {
        "player_pass_yds":      (wk(stats["pass_yd"] * SCORING["pass_yd"]) if stats else None,
                                 lambda v: v * SCORING["pass_yd"]),
        "player_pass_tds":      (wk(stats["pass_td"] * SCORING["pass_td"]) if stats else None,
                                 lambda v: v * SCORING["pass_td"]),
        "player_rush_yds":      (wk(stats["rush_yd"] * SCORING["rush_yd"]) if stats else None,
                                 lambda v: v * SCORING["rush_yd"]),
        "player_reception_yds": (wk(stats["rec_yd"] * SCORING["rec_yd"]) if stats else None,
                                 lambda v: v * SCORING["rec_yd"]),
        "player_receptions":    (wk(stats["rec"] * SCORING["rec"]) if stats else None,
                                 lambda v: v * SCORING["rec"]),
        "player_anytime_td":    (wk((stats["rush_td"] + stats["rec_td"] + stats["ret_td"]) * SCORING["td"]) if stats else None,
                                 lambda v: v * SCORING["td"]),
    }
    # Components with no prop market at all -> always from baseline
    always = wk(stats["pass_int"] * SCORING["pass_int"]
                + stats["two_pt"] * SCORING["two_pt"]
                + stats["fum_lost"] * SCORING["fum_lost"]) if stats else 0.0

    total, present, used_odds, used_base = always, [], 0, 0
    for mkey, (base_pts, fn) in comp.items():
        v = odds.get(mkey) if odds else None
        if v is not None:
            total += fn(v); present.append(mkey); used_odds += 1
        elif base_pts is not None:
            total += base_pts; used_base += 1

    expected = set(MARKETS_BY_POS.get(norm_pos(p.get("pos")), []))

    if used_odds == 0:
        if proj:
            return round(proj / 17, 2), "baseline", [], 0.0
        return (None, "n/a", [], None)

    # Confidence: point-weighted coverage of the expected markets.
    if stats:
        w = {m: abs(comp[m][0]) for m in expected if comp[m][0] is not None}
        tot = sum(w.values())
        conf = (sum(v for m, v in w.items() if m in present) / tot) if tot > 0 \
            else (len(set(present) & expected) / len(expected) if expected else 0.0)
    else:
        conf = len(set(present) & expected) / len(expected) if expected else 0.0

    got = set(present) & expected
    if got >= expected:
        src = "odds"
    elif stats:
        src = "blend"           # missing components filled from this player's baseline
    else:
        src = "odds~"           # partial and no baseline to fill the gaps
    return round(total, 2), src, present, conf

# ---------------------------------------------------------------------------
# Yahoo roster (best effort)
# ---------------------------------------------------------------------------
def load_yahoo_roster():
    try:
        from yahoo_oauth import OAuth2
        import yahoo_fantasy_api as yfa
        sc = OAuth2(None, None, from_file=os.path.join(HERE, "oauth2.json"))
        if not sc.token_is_valid():
            sc.refresh_access_token()
        tm = yfa.Team(sc, TEAM_KEY)
        roster = tm.roster()
        if not roster:
            return None
        gm = yfa.Game(sc, "nfl")
        lg = gm.to_league(LEAGUE_KEY)
        ids = [p.get("player_id") for p in roster if p.get("player_id") is not None]
        details = {str(d.get("player_id")): d for d in lg.player_details(ids)} if ids else {}
        out = []
        for p in roster:
            d = details.get(str(p.get("player_id")), {})
            out.append({
                "name": p.get("name"),
                "team": (d.get("editorial_team_abbr") or "").upper(),
                "pos": d.get("display_position") or d.get("primary_position") or "",
                "slot": p.get("selected_position") or "",
                "bye": None, "proj": None, "stats": None,
            })
        return out
    except Exception as e:
        print(f"[warn] Yahoo roster fetch failed ({type(e).__name__}: {e}). "
              f"Falling back to DEFAULT_ROSTER.", file=sys.stderr)
        return None

# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-default", action="store_true", help="skip Yahoo, use DEFAULT_ROSTER")
    ap.add_argument("--starters", action="store_true", help="only starting slots (skip BN)")
    ap.add_argument("--refresh", action="store_true", help="force a re-pull even if cache is current")
    ap.add_argument("--stale-ok", action="store_true", help="if a re-pull fails, use last week's cached lines anyway")
    ap.add_argument("--dry-run", action="store_true", help="print the call plan + est. cost, no calls")
    ap.add_argument("--csv", help="also write results to this CSV path")
    args = ap.parse_args()

    # 1) Roster
    roster = None if args.force_default else load_yahoo_roster()
    source = "Yahoo (live)"
    if not roster:
        roster = DEFAULT_ROSTER
        source = "DEFAULT_ROSTER (hardcoded)"

    # 2) Odds -> per-player market lines
    props_by_player = {}
    key = load_odds_key()
    if not key:
        print("[note] No Odds API key found. Showing season-projection baselines only.\n",
              file=sys.stderr)
    else:
        odds_by_event = {}
        cur_week = nfl_week_key()
        cache = load_cache()
        cache_fresh = bool(cache) and cache.get("week_key") == cur_week

        if cache_fresh and not args.refresh and not args.dry_run:
            # Cache is for the current NFL week -> reuse it, no credits spent.
            odds_by_event = cache["events"]
            print(f"[odds] cache current for NFL week {cur_week} "
                  f"({_human_age(cache.get('fetched_at'))}, 0 credits).")
        else:
            reason = ("--refresh" if args.refresh else
                      "no cache yet" if cache is None else
                      "cache is for an earlier week -> auto-refreshing" if not cache_fresh else
                      "dry-run")
            try:
                with requests.Session() as s:
                    events = get_events(key, s)                       # free call
                    plan, skipped = build_plan(roster, events, args.starters)
                    print(f"[odds] {reason} (NFL week {cur_week}).")
                    print_plan(plan, skipped)
                    if args.dry_run:
                        print("\n[dry-run] no odds calls made.")
                    else:
                        odds_by_event = fetch_props(key, plan, s)
                        save_cache(cur_week, odds_by_event)
            except requests.RequestException as ex:
                # Live pull failed (e.g. corp-network Zscaler block).
                if cache and cache_fresh:
                    odds_by_event = cache["events"]
                    print(f"[warn] re-pull failed ({type(ex).__name__}); using current-week "
                          f"cache ({_human_age(cache.get('fetched_at'))}).", file=sys.stderr)
                elif cache and args.stale_ok:
                    odds_by_event = cache["events"]
                    print(f"[warn] re-pull failed ({type(ex).__name__}); using STALE cache "
                          f"for week {cache.get('week_key')} (--stale-ok).", file=sys.stderr)
                else:
                    print(f"[warn] odds fetch failed ({type(ex).__name__}); no current cache. "
                          f"Baselines only. (Run on hotspot to refresh; --stale-ok to reuse "
                          f"old lines.)", file=sys.stderr)
        props_by_player = aggregate_props(odds_by_event)

    # 3) Merge + build rows
    rows = []
    for p in roster:
        odds = props_by_player.get(norm_name(p.get("name")), {})
        wk_pts, src, cov, conf = project_player(p, odds)
        rows.append([
            p.get("slot"), p.get("name"), p.get("team"), p.get("pos"),
            wk_pts if wk_pts is not None else "-",
            src,
            conf_label(conf),
            ",".join(SHORT[m] for m in cov) if cov else "-",
        ])

    rows.sort(key=lambda r: (slot_rank(r[0]),
                             -(r[4] if isinstance(r[4], (int, float)) else -1)))

    headers = ["Slot", "Player", "Team", "Pos", "Proj (wk)", "Source", "Conf", "Cov"]
    print(f"\nRoster source: {source} | Scoring: half-PPR "
          f"(0.5/rec, passTD=4, otherTD=6, INT=-1, fum=-2)")
    print(tabulate(rows, headers=headers, tablefmt="github"))
    print("\nSource: odds=all components market-driven | blend=some filled from this "
          "player's season baseline | baseline=no props (season/17) | n/a=no data.")
    print("Conf: share of the player's expected value backed by live market lines "
          "(High >=85%, Med >=50%, Low >0). Cov: which markets were live "
          "(PaYd/PaTD/RuYd/ReYd/Rec/TD).")

    if args.csv:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(headers)
            w.writerows(rows)
        print(f"\nSaved CSV to: {os.path.abspath(args.csv)}")

if __name__ == "__main__":
    main()
