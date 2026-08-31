#!/usr/bin/env python3
"""
app.py — Streamlit web UI for the roster value projections.

Roster source (sidebar):
  * Yahoo (live)   -> pulls your current roster from Yahoo so trades/adds/drops show
                      up automatically. Needs Yahoo creds in Streamlit secrets (see
                      DEPLOY.md). Falls back to the hardcoded roster on any error.
  * Hardcoded      -> the built-in DEFAULT_ROSTER.

Odds source (sidebar):
  * Upload saved file  -> reuse a snapshot (.json) or odds-lines (.csv); 0 API calls
  * Live pull          -> click a button to fetch fresh lines (cached per NFL week)
  * Baselines only     -> season-projection/17, no network

Because requests are made from Streamlit's servers (not your laptop), the corporate
Zscaler block does not apply.

Local test:  streamlit run app.py
Deploy:      see DEPLOY.md
"""
import os
import json
import tempfile
import requests
import pandas as pd
import streamlit as st

import fantasy_value as fv

st.set_page_config(page_title="Roster Value", page_icon="🏈", layout="wide")

# Odds key: Streamlit secrets -> env var, so fv.load_odds_key() finds it.
try:
    if "ODDS_API_KEY" in st.secrets:
        os.environ["ODDS_API_KEY"] = st.secrets["ODDS_API_KEY"]
except Exception:
    pass  # no secrets.toml locally -> falls back to odds_api_key.txt

BENCH_SLOTS = {"BN"}
INACTIVE_SLOTS = fv.SKIP_SLOTS
SHORT_INV = {v: k for k, v in fv.SHORT.items()}


# ------------------------------------------------------------------ roster
def _write_yahoo_oauth_file() -> str:
    """Reconstruct an oauth2.json in a temp dir from Streamlit secrets [yahoo]."""
    y = st.secrets["yahoo"]  # raises if the section is missing
    data = {
        "consumer_key": y["consumer_key"],
        "consumer_secret": y["consumer_secret"],
        "access_token": y.get("access_token", ""),
        "refresh_token": y["refresh_token"],
        "token_type": y.get("token_type", "bearer"),
        "token_time": float(y.get("token_time", 0) or 0),
    }
    if "redirect_uri" in y:
        data["redirect_uri"] = y["redirect_uri"]
    if "guid" in y:
        data["guid"] = y["guid"]
    path = os.path.join(tempfile.gettempdir(), "oauth2_yahoo.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


@st.cache_data(ttl=3600, show_spinner="Loading Yahoo roster…")
def load_yahoo_cached(week_key: str):
    """Cached Yahoo roster pull (refreshes hourly). Returns (roster|None, err|None)."""
    try:
        path = _write_yahoo_oauth_file()
    except Exception as e:  # noqa: BLE001
        return None, f"Yahoo secrets missing/invalid: {type(e).__name__}: {e}"
    try:
        roster = fv.load_yahoo_roster(oauth_path=path, raise_errors=True)
        if not roster:
            return None, "Yahoo returned an empty roster."
        return fv.enrich_roster(roster), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def get_roster(source: str, week: str):
    """Return (roster, label, warning|None)."""
    if source == "Hardcoded":
        return list(fv.DEFAULT_ROSTER), "Hardcoded", None
    roster, err = load_yahoo_cached(week)
    if roster:
        return roster, "Yahoo (live)", None
    return list(fv.DEFAULT_ROSTER), "Hardcoded (fallback)", err


# ------------------------------------------------------------------ odds
@st.cache_data(ttl=6 * 3600, show_spinner="Pulling odds…")
def pull_odds(week_key: str, roster_json: str):
    """Full-roster live pull, cached by (week, roster). Returns (odds_by_event, err)."""
    roster = json.loads(roster_json)
    key = fv.load_odds_key()
    if not key:
        return {}, "No ODDS_API_KEY configured — add it to Streamlit secrets."
    try:
        with requests.Session() as s:
            events = fv.get_events(key, s)                          # free call
            plan, skipped = fv.build_plan(roster, events, starters_only=False)
            if not plan:
                why = "; ".join(f"{n} ({r})" for n, r in skipped[:3])
                return {}, ("No games within ~9 days to pull — player props are only "
                            f"posted about a week before kickoff. e.g. {why}")
            return fv.fetch_props(key, plan, s), None
    except Exception as e:  # noqa: BLE001
        return {}, f"{type(e).__name__}: {e}"


def props_from_lines_csv(file) -> dict:
    df = pd.read_csv(file)
    props = {}
    for _, r in df.iterrows():
        name = str(r["Player"])
        d = props.setdefault(fv.norm_name(name), {"_name": name})
        mkey = SHORT_INV.get(str(r["Market"]).strip())
        if mkey is not None and pd.notna(r["Line"]):
            d[mkey] = float(r["Line"])
    return props


def build_projection_df(roster: list, props: dict) -> pd.DataFrame:
    rows = []
    for p in roster:
        wk, src, cov, conf = fv.project_player(p, props.get(fv.norm_name(p["name"]), {}))
        rows.append({
            "Slot": p.get("slot"), "Player": p.get("name"), "Team": p.get("team"),
            "Pos": p.get("pos"), "Proj (wk)": wk, "Source": src,
            "Conf": round(conf * 100) if conf is not None else None,
            "Cov": ",".join(fv.SHORT[m] for m in cov) if cov else "",
            "_rank": fv.slot_rank(p.get("slot")),
        })
    return (pd.DataFrame(rows)
            .sort_values(["_rank", "Proj (wk)"], ascending=[True, False], na_position="last")
            .drop(columns="_rank").reset_index(drop=True))


def odds_lines_df(props: dict) -> pd.DataFrame:
    recs = [{"Player": d.get("_name"), "Market": fv.SHORT.get(m, m), "Line": round(v, 3)}
            for d in props.values() for m, v in d.items() if m != "_name"]
    return (pd.DataFrame(recs).sort_values(["Player", "Market"]).reset_index(drop=True)
            if recs else pd.DataFrame(columns=["Player", "Market", "Line"]))


# --------------------------------------------------------------------------- UI
st.title("🏈 Roster Value")
st.caption("Market-implied weekly projections (half-PPR) from The Odds API, with a "
           "season-baseline fallback and a per-player confidence score.")

week = fv.nfl_week_key()

with st.sidebar:
    st.header("Roster")
    roster_source = st.radio("Roster source", ["Yahoo (live)", "Hardcoded"], index=0,
                             help="Yahoo reflects trades/adds automatically. "
                                  "Falls back to the built-in roster if Yahoo fails.")
    st.header("View")
    view = st.radio("Show", ["All", "Starters", "Bench"], horizontal=True, index=0)

    st.divider()
    st.header("Odds source")
    odds_source = st.radio(
        "Where should odds come from?",
        ["Upload saved file", "Live pull (uses credits)", "Baselines only"], index=0,
        help="The app never calls the odds API on its own — you choose here.")
    upload = None
    if odds_source == "Upload saved file":
        upload = st.file_uploader("Snapshot (.json) or odds-lines (.csv)", type=["json", "csv"])
    elif odds_source == "Live pull (uses credits)":
        if st.button(f"⤓ Fetch live odds for NFL week {week}"):
            st.session_state["live_on"] = week
        if st.button("↻ Force re-pull (clear cache)"):
            st.cache_data.clear()
            st.session_state["live_on"] = week

# ---- resolve roster
roster, roster_label, roster_warn = get_roster(
    "Hardcoded" if roster_source == "Hardcoded" else "Yahoo", week)
if roster_warn:
    st.warning(f"Yahoo roster unavailable — using hardcoded roster. ({roster_warn})")

# ---- resolve odds
props, raw, err, note = {}, None, None, None
if odds_source == "Upload saved file":
    if upload is None:
        note = ("Upload a saved **snapshot (.json)** or **odds-lines (.csv)** to reuse a "
                "past pull with **0 API calls**, or switch to *Live pull*.")
    else:
        try:
            if upload.name.lower().endswith(".json"):
                raw = json.loads(upload.getvalue())
                props = fv.aggregate_props(raw)
            else:
                props = props_from_lines_csv(upload)
            st.sidebar.success(f"Loaded '{upload.name}' — no API calls made.")
        except Exception as e:  # noqa: BLE001
            err = f"Could not read '{upload.name}': {type(e).__name__}"
elif odds_source == "Live pull (uses credits)":
    if st.session_state.get("live_on") == week:
        raw, err = pull_odds(week, json.dumps(roster))
        props = fv.aggregate_props(raw) if raw else {}
    else:
        note = "Click **Fetch live odds** in the sidebar to pull. Nothing is called until you do."

if err:
    st.warning(f"Odds unavailable — showing season-projection baselines. ({err})")
elif note:
    st.info(note)

# ---- table + filter
df = build_projection_df(roster, props)
slots_upper = df["Slot"].astype(str).str.upper()
if view == "Starters":
    df_view = df[~slots_upper.isin(BENCH_SLOTS | INACTIVE_SLOTS)]
elif view == "Bench":
    df_view = df[slots_upper.isin(BENCH_SLOTS)]
else:
    df_view = df

st.caption(f"Roster: **{roster_label}**  ·  NFL week {week}")
st.dataframe(
    df_view, hide_index=True, use_container_width=True,
    column_config={
        "Proj (wk)": st.column_config.NumberColumn("Proj (wk)", format="%.2f"),
        "Conf": st.column_config.ProgressColumn(
            "Conf", help="Share of expected value backed by live market lines",
            min_value=0, max_value=100, format="%d%%"),
    },
)

# ---- downloads
c1, c2, c3 = st.columns(3)
c1.download_button("⬇ Projections (CSV)", df.to_csv(index=False).encode("utf-8"),
                   file_name=f"projections_{week}.csv", mime="text/csv")
if props:
    c2.download_button("⬇ Odds lines (CSV, re-uploadable)",
                       odds_lines_df(props).to_csv(index=False).encode("utf-8"),
                       file_name=f"odds_lines_{week}.csv", mime="text/csv")
if raw:
    c3.download_button("⬇ Odds snapshot (JSON, re-uploadable)",
                       json.dumps(raw).encode("utf-8"),
                       file_name=f"odds_snapshot_{week}.json", mime="application/json",
                       help="Save this and re-upload later to recompute with 0 API calls.")

st.caption(
    "Source col: **odds** = market-driven, **blend** = partial (rest from this player's "
    "baseline), **odds~** = partial, no baseline, **baseline** = season/17, **n/a** = no "
    "data.  ·  Cov = live markets (PaYd/PaTD/RuYd/ReYd/Rec/TD)."
)
