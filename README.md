# Dedicated News

A morning briefing for a team of financial advisors in Tampa Bay. It is a single page: market figures, a client talking-points script, and the headlines that matter before the open. No ads. No accounts. Each headline opens the original story in a new tab.

The page is rebuilt from free public sources. You change sources, keywords, and market symbols in `feeds.yml`. You change the client stories in `prompts/stories.yml`, and the voice of an optional model in `prompts/talking_points.md`. You do not need to write code.

**The site:** https://bglisson1.github.io/Dedicated-News/

## When it updates

Times below are Eastern.

Weekdays:

- 6:00 AM
- 7:30 AM
- 8:45 AM
- 12:00 PM
- 4:30 PM

Saturday and Sunday: once, at 8:00 AM.

GitHub's clock is UTC and does not move for daylight saving. The schedule lists each of those times twice, once for daylight time and once for standard time. The run checks whether it is EDT or EST and skips the copy, so each slot builds once. It also builds when a change is saved on the `main` branch, and when you run it by hand.

GitHub turns off scheduled updates on a public project after 60 days with no new commit. Each run on `main` turns that schedule back on with the token GitHub already gives the Action. There is no password to add. If the project has gone 45 days without a commit, the run also saves a date in a file named `.keepalive`. That file only exists so the schedule stays on. You can ignore it.

## The one setup step

GitHub has to be told to publish the page. Do this once, if it is not already set:

1. Open [github.com/bglisson1/Dedicated-News](https://github.com/bglisson1/Dedicated-News).
2. Click **Settings** in the top menu of the repository.
3. In the left sidebar, click **Pages**.
4. Under **Build and deployment**, find **Source**.
5. Click the dropdown and choose **GitHub Actions**.

You do not pick a branch. Leave Source set to GitHub Actions. After the next successful run, the site address above will show the page. The first run can take a couple of minutes.

If the **Actions** tab shows a red X on the job named **Deploy to GitHub Pages**, the page did not publish. A single quote or a single news site being down does not cause a red X. A typo in `feeds.yml` does.

## Run an update by hand

1. Open the repository on GitHub.
2. Click the **Actions** tab.
3. In the left sidebar, click **Build dedicated news**.
4. Click the **Run workflow** button on the right.
5. Leave the branch set to **main**.
6. Click the green **Run workflow** button.

Refresh the site in a minute or two.

## What is on the page

The date and the "last updated" time are Eastern.

1. **At a glance.** Four cards: S&P 500, Dow, Russell 2000 (previous close, change, and arrow), and the Freddie Mac 30-year mortgage rate with the week-over-week change. On a phone those four sit in a two-by-two grid. When it fits on one line, the S&P and Dow cards also show a short futures percent.
2. **Client talking points.** One short piece in plain language for a client who does not follow markets: what is going on, a simple story, and a line or two the advisor can say. It does not promise returns or tell anyone what to buy or sell.
3. **Top 3 Financial News.** Business and markets stories, one line each. The same story in several outlets is shown once, and the stories the most outlets are carrying are listed first.
4. **Top 3 Political News.** Washington, policy, the Fed, taxes, trade, and elections.
5. **More markets.** Nasdaq, 10-year and 2-year Treasuries, the futures cards, oil, gold, the 10-year market yield, VIX, and Bitcoin. A missing quote is a dash. Each card says whether the figure is a previous close, a weekly rate, or a premarket / live price.
6. **More Market & Economy.** About a dozen further market links.
7. **Industry News.** Wealth management and advisor trade press. Those sites publish more slowly, so this section keeps about two weeks of headlines.
8. **Footer.** "For internal team prep only. Not investment advice." Plus where the figures came from.

Sports and celebrity headlines are left out. A story is not repeated in a second section.

## The client script

Every build writes the talking points from `prompts/stories.yml`. That file holds short stories and closing lines. Each story is tagged with the kind of day it fits: stocks up, stocks down, a flat day, rates up, rates down, a big news day, record highs, or a volatile day. The build picks one story from the tags that match today's figures. The pick is tied to the date, so the same conditions keep walking through the list and a story does not come back for about two weeks. The opening lines are written from the real figures, in plain words such as "stocks rose a bit." No model is required for this.

You can edit the stories. Keep each one to two or three sentences, keep the `conditions` list, and do not put statistics in a story.

### Optional: use your own model key

GitHub Models is no longer available, so the build does not call it. If you want a model to write the piece instead of the story library, add one repository secret. The build checks three names, in this order, and uses the first one it finds:

| Secret name | Model |
| --- | --- |
| `OPENAI_API_KEY` | `gpt-4.1-mini` |
| `ANTHROPIC_API_KEY` | `claude-haiku-4-5` |
| `XAI_API_KEY` | `grok-3-mini` |

The model names are in `feeds.yml` if you want a different small model. The instructions the model follows are in `prompts/talking_points.md`. Keep the request for a JSON object with `headline`, `passage`, and `say`. The build also hands the model two or three stories from the library as voice samples. If the call fails, or the writing is not usable, the page uses the story library.

To add a key:

1. Open [github.com/bglisson1/Dedicated-News](https://github.com/bglisson1/Dedicated-News).
2. Click **Settings** in the top menu of the repository.
3. In the left sidebar, click **Secrets and variables**.
4. Click **Actions**.
5. Click **New repository secret**.
6. In **Name**, enter `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY`, or `XAI_API_KEY`.
7. Paste the key into **Secret**.
8. Click **Add secret**.

The next build on `main` will use it. You do not put the key in `feeds.yml` or in the page.

On an ordinary day the script talks about this week and the long-term plan. It talks about today only when something large is actually in the data: a move of about 1.5% or more in the S&P, Dow, Nasdaq, or their futures, or a fresh headline about a Fed decision, a jobs report, or an inflation report. You can change that bar and those phrases in `feeds.yml` (`big_move_percent`, `big_day_phrases`).

## Change sources or keywords

Everything you would usually change is in `feeds.yml`. The notes at the top of that file walk through each kind of edit.

1. Open the repository on GitHub.
2. Click **feeds.yml**.
3. Click the pencil icon (**Edit this file**).
4. Make the change.
   - To add a source, copy a block that starts with `- name:` and paste it under `sources:` in Financial, Political, or Industry. Set the name, the feed address, and `max_items`.
   - To turn a source off, change `enabled: yes` to `enabled: no`.
   - To block a kind of headline, add a line under `block_keywords`.
   - To float a market topic, add a line under `boost_keywords`.
   - To change what counts as political news for clients, edit `political_focus_keywords`.
5. Click **Commit changes...**.
6. Leave **Commit directly to the `main` branch** selected.
7. Click **Commit changes**.

The page rebuilds from that save. Keep the quotes around web addresses. Keep the indentation lined up with the block you copied.

A feed address is the site's RSS or Atom link. It usually ends in `.xml`, `/feed/`, or `/rss`. Paste the whole address, including `https://`.

## Feeds and figures that needed a stand-in

These were checked live. A dead feed is skipped and the rest of the page still builds.

| Wanted | What happened | What the page uses |
| --- | --- | --- |
| FRED `DGS10`, `DGS2`, `MORTGAGE30US` | The build tries the FRED CSV first. From some networks FRED never answers. | U.S. Treasury daily yield curve for the 10-year and 2-year (the same figures FRED republishes). Freddie Mac's weekly PMMS file for the 30-year mortgage (the same series as `MORTGAGE30US`). The footer names whichever source answered. |
| Wall Street Journal RSS | `feeds.a.dj.com` was still serving stories from early 2025. | A Google News feed limited to recent markets stories on wsj.com. |
| Reuters and AP | Their old public RSS addresses are gone. | Google News feeds limited to those sites. |
| MarketWatch marketpulse | Frozen on old headlines. The top-stories feed is mostly personal-finance columns. | A Google News feed of MarketWatch market stories. |
| Yahoo Finance chart | Used for index closes, futures, oil, gold, VIX, Bitcoin, and the market 10-year. | `query1` and, if needed, `query2` `finance.yahoo.com/v8/finance/chart`. |
| InvestmentNews, ThinkAdvisor, Financial Planning, FA Magazine, Citywire RIA, Financial Advisor IQ | The direct RSS addresses returned "not found," "forbidden," or an empty file. | Google News feeds limited to each site. Citywire is limited to the RIA path. |
| Barron's Advisor | `barrons.com` RSS returns "unauthorized," and a news search was general Barron's, not the advisor desk. | Left off (`enabled: no`) so those stories do not crowd the industry section. |
| WealthManagement.com and PlanAdviser | Direct feeds answer. | Used as-is. PlanAdviser is an extra retirement-industry source. Turn it off in `feeds.yml` if you do not want it. |

Headlines older than 60 hours are left off on a normal weekday. On Saturday, Sunday, and Monday morning the window is 96 hours, so Friday's news is still there Monday before the open. Industry headlines are kept for 14 days. Both windows are in `feeds.yml`.
