"""
One-off correction for the markets[0] resolution bug.

Re-checks EVERY Notion row that has a Polymarket link against the CORRECT
sub-market (matched by title), and fixes rows that were:
  * resolved WIN/LOSS from the wrong sub-market, or
  * marked resolved when the correct sub-market is still open (revert to PENDING).

Dry-run by default. Pass --apply to write changes to Notion.
Caches gamma-api results per slug (many rows share one event).
"""
import io, os, sys, re, json, time, urllib.request
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

TOK = os.getenv("NOTION_TOKEN", "").strip()
DB  = os.getenv("NOTION_DB_ID", "33e8b842-ae95-81d3-8a6e-eed814ab9f81").strip()
if not TOK:
    sys.exit("NOTION_TOKEN not set (put it in .env)")
APPLY = "--apply" in sys.argv
H = {"Authorization": f"Bearer {TOK}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
SLUG_RE = re.compile(r"polymarket\.com/event/([^/?#]+)")

def notion(path, method="GET", body=None):
    req = urllib.request.Request(f"https://api.notion.com/v1{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None, headers=H)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def fetch_all():
    rows, cur = [], None
    while True:
        b = {"page_size": 100}
        if cur: b["start_cursor"] = cur
        d = notion(f"/databases/{DB}/query", "POST", b)
        rows.extend(d["results"])
        if not d.get("has_more"): break
        cur = d["next_cursor"]
    return rows

def text(p, k):
    it = p.get(k, {}).get("title") or p.get(k, {}).get("rich_text") or []
    return "".join(i.get("plain_text", "") for i in it)
def sel(p, k): return (p.get(k, {}).get("select") or {}).get("name") or ""
def url(p, k): return p.get(k, {}).get("url") or ""

def norm(s): return re.sub(r"[^a-z0-9]", "", (s or "").lower())
def side_yes(s):
    s = s.lower()
    if any(k in s for k in ("yes", "up", "above", "over", "high")): return True
    if any(k in s for k in ("no", "down", "below", "under", "low")): return False
    return None  # team / ambiguous

_cache = {}
def get_event(slug):
    if slug in _cache: return _cache[slug]
    try:
        u = f"https://gamma-api.polymarket.com/events?slug={slug}&limit=1"
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            ev = json.load(r)
        time.sleep(0.25)
    except Exception:
        ev = None
    _cache[slug] = ev
    return ev

def correct_result(title, side, slug):
    """Return 'WIN' | 'LOSS' | 'SKIP' | None(open/unknown)."""
    ev = get_event(slug)
    if not ev: return None
    e = ev[0] if isinstance(ev, list) else ev
    mkts = e.get("markets", [])
    if not mkts: return None
    mkt = None
    if len(mkts) == 1:
        mkt = mkts[0]
    else:
        nt = norm(title)
        for m in mkts:
            if norm(m.get("question", "")) == nt: mkt = m; break
        if mkt is None:
            subs = [m for m in mkts if nt and (nt in norm(m.get("question","")) or norm(m.get("question","")) in nt)]
            if len(subs) == 1: mkt = subs[0]
        if mkt is None: return None
    if not mkt.get("closed") and not mkt.get("resolved"):
        return None
    pr = mkt.get("outcomePrices", "[]")
    pr = json.loads(pr) if isinstance(pr, str) else pr
    if not pr: return None
    yes_won = float(pr[0]) >= 0.99
    ry = side_yes(side)
    if ry is None: return "SKIP"
    return "WIN" if ry == yes_won else "LOSS"

def set_result(page_id, value):
    body = {"properties": {"Result": {"select": ({"name": value} if value else None)}}}
    notion(f"/pages/{page_id}", "PATCH", body)

print(f"Mode: {'APPLY' if APPLY else 'DRY-RUN'}\nFetching rows…")
rows = fetch_all()
print(f"{len(rows)} rows.\n")

fix_wl = revert = skip_fix = unchanged = noslug = 0
for row in rows:
    p = row["properties"]
    link = url(p, "Link")
    m = SLUG_RE.search(link or "")
    if not m:
        noslug += 1
        continue
    title = text(p, "Market"); side = text(p, "Side to Buy")
    current = sel(p, "Result")
    correct = correct_result(title, side, m.group(1))

    # map "still open / unknown" -> should be PENDING (empty)
    target = correct if correct else ""
    # Only act on meaningful differences; don't touch SKIP<->empty churn for unknowns
    if target == "" and current == "":
        unchanged += 1; continue
    if target == current:
        unchanged += 1; continue
    # classify
    if current in ("WIN", "LOSS") and target == "":
        revert += 1; kind = "REVERT→PENDING"
    elif current in ("WIN", "LOSS") and target in ("WIN", "LOSS"):
        fix_wl += 1; kind = f"{current}→{target}"
    elif target == "SKIP":
        skip_fix += 1; kind = f"{current or 'empty'}→SKIP"
    else:
        kind = f"{current or 'empty'}→{target}"
        fix_wl += 1
    print(f"  {kind:16s} | {title[:52]}")
    if APPLY:
        try:
            set_result(row["id"], target)
        except Exception as e:
            print(f"    !! update failed: {e}")

print(f"\n==== {'APPLIED' if APPLY else 'WOULD CHANGE'} ====")
print(f"  WIN/LOSS flipped     : {fix_wl}")
print(f"  reverted to PENDING  : {revert}")
print(f"  set to SKIP          : {skip_fix}")
print(f"  unchanged            : {unchanged}")
print(f"  no polymarket link   : {noslug}")
if not APPLY:
    print("\nRe-run with --apply to write these corrections.")
