# Harvey pipeline review: 2026-10-26 13:25 IST

## State
- 69 companies / 610 observations / 0 prospects (read-only query of data/harvey.db).
- All 69 companies came from `discover:serper` on 2026-10-24 (about 10:00 IST). All are already profiled and prospect-checked, so Scout has nothing new to work on.
- Only 2 companies (SenseLive, TronicsZone) have CONTACT_FOUND/DECISION_MAKER_TITLE observations. With no Hunter or verifier key, none became prospects.
- About 25 of the 69 are junk for this ICP: job boards (naukri, jobrapido, jobsora, empleos), directories (IndiaMART, Justdial, Tradeindia, Etsy, BoardGameGeek, Team-BHP), foreign stores (Asmodee USA, Gamenerdz, Cogs Toys IE, TeacherSource), and gov/finance/NGO sites (hte.rajasthan.gov.in, Credila, ActionAid).

## Root cause of "Serper 400 on every call"
The key was never the problem. Serper **free accounts reject `num` > 10** with HTTP 400 `"Query pattern not allowed for free accounts."`. Scout sent `num=20` (MAX_RESULTS_PER_QUERY). Replaying the exact query gave 400 with num=20 and 200 with num=10.

## Fix applied (uncommitted, in working tree)
- `harvey/agents/scout.py`: new `SERPER_MAX_NUM = 10`, and Serper requests use `min(MAX_RESULTS_PER_QUERY, SERPER_MAX_NUM)`.
- `harvey/collectors/discover.py`: the Serper provider sends `num = min(depth, 10)`.
- Restarted the "Harvey Heartbeat" scheduled task at 13:24:36 (killed the old 2026-10-25 process tree, then `schtasks /Run`). First cycle 13:24:37 to 13:24:58: all 3 Serper calls returned **200 OK**, the cycle completed, and there were no errors.

## Still blocking prospects (needs an ICP decision)
1. The LinkedIn queries quote the literal industry label `"Engineering Education (colleges and schools)"`, and the titles include "Student" / "Engineering Student". These return 0 hits even with a working key. Students are also not buyers.
2. "Discovering companies" ends instantly with "No new companies". The heartbeat does not run paid Serper discovery on its own, so new companies only come from `harvey discover --city ...`.
3. Hunter and verifier keys are empty, so even found contacts stay `guess` and are not usable.

## Recommended next step
Rewrite the ICP in harvey.local.yaml: buyer titles (Founder, Lab In-charge, Product Design Head, Procurement), plain industry keywords, and a junk-domain exclusion. Then run `harvey discover --city "Jaipur, Rajasthan;Delhi, India"` (free Serper credits; num is now 10).
