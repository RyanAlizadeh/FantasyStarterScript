#!/usr/bin/env python3
"""
app.py — Streamlit web UI for the roster value projections.

Reuses all the logic in fantasy_value.py. The app NEVER calls the Odds API on its
own — you choose the source in the sidebar:

  * Upload saved file  -> reuse a snapshot (.json) or odds-lines (.csv); 0 API calls
  * Live pull          -> click a button to fetch fresh lines (~36 credits/week, cached)
  * Baselines only     -> season-projection/17, no network

The odds pull always covers the FULL roster (starters + bench) and is cached by NFL
week, so switching the All/Starters/Bench view never triggers another API call.
Because the request is made from Streamlit's servers (not your laptop), the corporate
Zscaler block does not apply.

Local test:  streamlit run app.py
Deploy:      see DEPLOY.md
"""
import os
import json
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
INACTIVE_SLOTS = fv.SKIP_SLOTS            # IR, PUP, etc.
SHORT_INV = {v: k for k, v in fv.SHORT.items()}  # "PaYd" -> "player_pass_yds"


@st.cache_data(ttl=6 * 3600, show_spinner="Pulling odds…")
def pull_odds(week_key: str):
    """Full-roster live pull, cached by NFL week. Returns (odds_by_event, err)."""
    key = fv.load_odds_key()
    if not key:
        return {}, "No ODDS_API_KEY configured — add it to Streamlit secrets."
    try:
        with requests.Session() as s:
            events = fv.get_events(key, s)                          # free call
            plan, skipped = fv.build_plan(fv.DEFAULT_ROSTER, events, starters_only=False)
            if not plan:
                why = "; ".join(f"{n} ({r})" for n, r in skipped[:3])
                return {}, ("No games within ~9 days to pull — player props are only "
                            f"posted about a week before kickoff. e.g. {why}")
            return fv.fetch_props(key, plan, s), None
    except Exception as e:  # noqa: BLE001 - surface any failure to the UI
        return {}, f"{type(e).__name__}: {e}"


def props_from_snapshot(raw: dict) -> dict:
    """Aggregate a raw odds_by_event snapshot -> {norm_name: {market: line, _name}}."""
    return fv.aggregate_props(raw)


def props_from_lines_csv(file) -> dict:
    """Rebuild player props directly from a downloaded odds-lines CSV."""
    df = pd.read_csv(file)
    props = {}
    for _, r in df.iterrows():
        name = str(r["Player"])
        d = props.setdefault(fv.norm_name(name), {"_name": name})
        mkey = SHORT_INV.get(str(r["Market"]).strip())
        if mkey is not None and pd.notna(r["Line"]):
            d[mkey] = float(r["Line"])
    return props


def build_projection_df(props: dict) -> pd.DataFrame:
    rows = []
    for p in fv.DEFAULT_ROSTER:
        wk, src, cov, conf = fv.project_player(p, props.get(fv.norm_name(p["name"]), {}))
        rows.append({
            "Slot": p["slot"], "Player": p["name"], "Team": p["team"], "Pos": p["pos"],
            "Proj (wk)": wk,
            "Source": src,
            "Conf": round(conf * 100) if conf is not None else None,
            "Cov": ",".join(fv.SHORT[m] for m in cov) if cov else "",
            "_rank": fv.slot_rank(p["slot"]),
        })
    return (pd.DataFrame(rows)
            .sort_values(["_rank", "Proj (wk)"], ascending=[True, False], na_position="last")
            .drop(columns="_rank")
            .reset_index(drop=True))


def odds_lines_df(props: dict) -> pd.DataFrame:
    """Flat, human-readable + re-uploadable table of the raw market lines."""
    recs = []
    for d in props.values():
        name = d.get("_name")
        for mkey, val in d.items():
            if mkey == "_name":
                continue
            recs.append({"Player": name, "Market": fv.SHORT.get(mkey, mkey),
                         "Line": round(val, 3)})
    return (pd.DataFrame(recs).sort_values(["Player", "Market"]).reset_index(drop=True)
            if recs else pd.DataFrame(columns=["Player", "Market", "Line"]))


# --------------------------------------------------------------------------- UI
st.title("🏈 Roster Value")
st.caption("Market-implied weekly projections (half-PPR) from The Odds API, with a "
           "season-baseline fallback and a per-player confidence score.")

week = fv.nfl_week_key()

with st.sidebar:
    st.header("View")
    view = st.radio("Show", ["All", "Starters", "Bench"], horizontal=True, index=0)

    st.divider()
    st.header("Odds source")
    source = st.radio(
        "Where should odds come from?",
        ["Upload saved file", "Live pull (uses credits)", "Baselines only"],
        index=0,
        help="The app never calls the API on its own — you choose here.")

    upload = None
    if source == "Upload saved file":
        upload = st.file_uploader("Snapshot (.json) or odds-lines (.csv)",
                                  type=["json", "csv"])
    elif source == "Live pull (uses credits)":
        if st.button(f"⤓ Fetch live odds for NFL week {week} (~36 credits)"):
            st.session_state["live_on"] = week          # remember the opt-in
        if st.button("↻ Force re-pull (clear cache)"):
            st.cache_data.clear()
            st.session_state["live_on"] = week

# ---------------------------------------------------- resolve odds by source
props, raw, err, note = {}, None, None, None

if source == "Upload saved file":
    if upload is None:
        note = ("Upload a saved **snapshot (.json)** or **odds-lines (.csv)** to reuse a "
                "past pull with **0 API calls**, or switch to *Live pull*. Showing "
                "baselines until then.")
    else:
        try:
            if upload.name.lower().endswith(".json"):
                raw = json.loads(upload.getvalue())
                props = props_from_snapshot(raw)
            else:
                props = props_from_lines_csv(upload)
            st.sidebar.success(f"Loaded '{upload.name}' — no API calls made.")
        except Exception as e:  # noqa: BLE001
            err = f"Could not read '{upload.name}': {type(e).__name__}"

elif source == "Live pull (uses credits)":
    if st.session_state.get("live_on") == week:
        raw, err = pull_odds(week)                       # cached per week
        props = props_from_snapshot(raw)
    else:
        note = "Click **Fetch live odds** in the sidebar to pull. Nothing is called until you do."

# "Baselines only" -> props stays {}

if err:
    st.warning(f"Odds unavailable — showing season-projection baselines. ({err})")
elif note:
    st.info(note)

# --------------------------------------------------------------- table + filter
df = build_projection_df(props)
slots_upper = df["Slot"].str.upper()
if view == "Starters":
    df_view = df[~slots_upper.isin(BENCH_SLOTS | INACTIVE_SLOTS)]
elif view == "Bench":
    df_view = df[slots_upper.isin(BENCH_SLOTS)]
else:
    df_view = df

st.dataframe(
    df_view, hide_index=True, use_container_width=True,
    column_config={
        "Proj (wk)": st.column_config.NumberColumn("Proj (wk)", format="%.2f"),
        "Conf": st.column_config.ProgressColumn(
            "Conf", help="Share of expected value backed by live market lines",
            min_value=0, max_value=100, format="%d%%"),
    },
)

# --------------------------------------------------------------- downloads
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
    f"NFL week {week}  ·  Source col: **odds** = fully market-driven, **blend** = partial "
    "(missing components from this player's baseline), **baseline** = season/17, "
    "**n/a** = no data.  ·  Cov = live markets (PaYd/PaTD/RuYd/ReYd/Rec/TD)."
)
