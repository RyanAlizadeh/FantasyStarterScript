# Deploying the Roster Value app to Streamlit Community Cloud

The app runs on Streamlit's servers, which make the odds API call — so the corporate
Zscaler block does not apply. Open the resulting URL from any computer.

## Files that get pushed to GitHub
- `app.py`, `fantasy_value.py`, `requirements.txt`, `.gitignore`, `DEPLOY.md`

## Files that MUST NOT be pushed (already in .gitignore)
- `oauth2.json` (Yahoo secrets), `odds_api_key.txt` (your API key),
  `odds_cache.json`, `venv/`, `Notes`

## One-time setup
1. Create a **private** GitHub repo and push this folder. Confirm the key file did
   NOT get committed:  `git ls-files | findstr odds_api_key`  should print nothing.
2. Go to https://share.streamlit.io  ->  **New app**  ->  choose your repo/branch and
   set the main file to `app.py`.
3. Open **Advanced settings -> Secrets** and paste (use YOUR key from
   `odds_api_key.txt` — do not commit it):

       ODDS_API_KEY = "paste-your-key-here"

4. Click **Deploy**. You'll get a URL like `https://<name>.streamlit.app`.

## Keep it private
- App **Settings -> Sharing** -> restrict viewers to your email(s). Otherwise anyone
  with the URL can trigger pulls against your Odds API quota.

## Using it
- Open the URL from any computer.
- Sidebar has **Starters only** and **Force refresh lines**.
- Odds are cached ~6h and per NFL week (auto-refresh each Tuesday).

## Local testing before deploy (on a non-Zscaler machine)
    pip install -r requirements.txt
    streamlit run app.py

## If the key is ever exposed
Regenerate it at the-odds-api.com, update the Streamlit secret and
`odds_api_key.txt`. Rotating is instant and free.
