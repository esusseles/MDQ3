# MD 2026 General Tracker: Governor + Question 3

Built from the MD-6 primary tracker. It looks the same and uses the same tabs, dashboard, drop feed, what-if, and operator lock. The difference is that it now holds **two races** and you switch between them with the 🏛 Governor / 🗳 Question 3 toggle in the header. A strip under the tabs always shows both races at a glance.

## Files
| File | What it is |
|---|---|
| `MD_General_Tracker.html` | The tracker page |
| `MD_General_Scraper.py` | Scraper and server. It also **serves the tracker page at `/`**, so a hosted copy gives you one link for everyone |
| `Dockerfile`, `requirements.txt` | For Railway or Render |
| `Start_Scraper.command` | Double-click to run on your Mac. It opens http://localhost:8765 |

## What gets scraped
**SBOE**: the statewide page (vote-method columns), the county breakdown page, and all 24 by-county pages, for each race.
- The scraper tries `/elections/2026/general_results/` and other capitalisations (SBOE used `General_Results` in 2024) until one loads.
- Governor: `gen_results_2026_1.html`, `gen_detail_results_2026_1_1.html`, `gen_results_2026_by_county_{1-24}.html`
- Question 3: `gen_qresults_2026_3.html`, `gen_detail_qresults_2026_3_1.html`, `gen_qresults_2026_by_county_{1-24}.html`

These file names follow SBOE's 2022/2024 pattern. They can't be confirmed until SBOE posts tonight.

**AP**: the % of expected vote counted for each county and statewide, plus AP's vote totals. These drive "AP implies X total turnout" and the projections.
- AP pages (built in): `https://apnews.com/projects/elections-2026/maryland-governor-results/` and `https://apnews.com/projects/elections-2026/maryland-question-3-results/`. The scraper reads AP's background data file, which includes all 24 counties (even the ones hidden behind the "show more" arrow), plus AP's expected vote by method for each county. The tracker uses those as the ballot universe when you leave the Ballot Universe boxes blank.
- The scraper tells Question 3 apart from Questions 1 and 2 using AP's race info. To see what it found, open the **Open scraper debug ↗** link (`/debug`) and look under `ap.races_seen`.

## Election-night checklist
1. Open the tracker, click ⚙️ Admin and enter the password. The default is `mdnight`; change it with the `OPERATOR_PASSWORD` variable on Railway.
2. Once SBOE posts, open `/debug`. Under `urls`, every page should show data, and `lastLog` should be all ✅. If a page shows ⏳/404, paste the correct SBOE link in Setup and press Send.
3. Paste the AP links (see above).
4. Optional: fill in Ballot Universe with mail returned and early votes. It's shared between both races, so you only enter it once.

## Prefilled placeholders (replace with your numbers)
- Expected turnout comes from 2022 governor turnout by county. Question 3 is set to 94% of that, to allow for undervote.
- Governor priors use the 2022 Moore/Cox share, with Libertarian votes folded into Cox. Ellis/White are set at about 1%.
- Question 3 priors take each county's 2022 Moore share and scale it so the statewide For is about 47%. This is based on the Sept 2026 poll showing 53% opposed.

## Hosting (later)
This works the same way as MD-6:
1. Create a new GitHub repo.
2. Upload the 4 files: scraper, tracker HTML, Dockerfile, requirements.txt.
3. On Railway, choose New Project → Deploy from repo.
4. Add a Volume mounted at `/app/data`. This keeps the feed and settings through restarts.
5. Set `OPERATOR_PASSWORD`, then Generate Domain.

That domain is the link you share, because the tracker loads at the root. You don't need Netlify. Render works the same way: create a Docker web service and add a disk at `/app/data`.

## Tested
Tested against a mock SBOE site and a mock AP page built in the same formats as the real 2022/2026 pages:
- Base-folder discovery worked.
- Baltimore City and Baltimore County stayed separate.
- The Comptroller, Question 1, local-question, and "Other Write-Ins" tables were correctly ignored.
- Drop cards were labelled with their vote method.
- AP Question 3 was told apart from Question 1.
- Both race dashboards and the phone layout rendered with no errors.

## Projection model: county × vote method (the default)
- **Governor:** Moore's Election Day share in each county starts from his early + mail share there, then applies that county's 2022 Election Day shift. Statewide in 2022, Moore got 74.6% of early + mail votes but 55.2% on Election Day. As Election Day votes come in, the actual results take over.
- **Question 3:** Election Day For = Moore's projected Election Day share + the For-vs-Moore gap seen in that county's early + mail votes. Q3 ballots left = governor ballots left × the roll-off seen so far. As Election Day votes come in, the actual Election Day gap takes over.
- Both shifts are applied proportionally (in log-odds), so a gap shrinks where Moore is very weak or very strong.
- **Sliders on the dashboard card:** "Election Day shift adjustment" (affects both races) and "Q3 Election Day gap adjustment".
- **Ballots left** come from the Ballot Universe tab if you fill it in. Otherwise they're expected turnout (or AP's implied total) minus what's been counted.
- To go back to the old priors-based model, go to Setup → Projection Settings and change "Projection model".
