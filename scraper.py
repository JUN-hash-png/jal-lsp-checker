from __future__ import annotations

import csv
import html
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; jal-lsp-checker/1.0; "
        "+https://github.com/JUN-hash-png/jal-lsp-checker)"
    )
}

MILE_RULE_RE = re.compile(
    r"(?P<yen>[0-9,]+)\s*円(?:\(税込\)|（税込）)?\s*ごとに\s*"
    r"(?P<miles>[0-9,]+)\s*マイル"
)
FIXED_MILES_RE = re.compile(r"(?:利用|購入|申込|成約|登録)[^。\n]{0,40}?([0-9,]+)\s*マイル")
MIN_SPEND_RE = re.compile(
    r"(?P<yen>[0-9,]+)\s*円(?:\(税込\)|（税込）)?\s*以上"
)
DATE_RE = re.compile(
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)
EXCLUSION_PHRASES = (
    "JALマイレージパーク経由によるマイル・Life Statusポイントは積算対象外",
    "JALマイレージパーク経由によるマイル・Life Status ポイントは積算対象外",
)
FIRST_ONLY_PHRASES = (
    "初回のみ", "初回購入", "初回利用", "初めてご利用", "新規購入",
    "新規入会", "新規登録", "新規申込",
)


@dataclass
class Offer:
    service: str
    condition: str
    unit_yen: int | None
    unit_miles: int | None
    spend_for_1_lsp: int | None
    miles_at_1_lsp: int | None
    lsp_at_minimum: float | None
    minimum_spend: int | None
    first_only: bool
    campaign_end: str
    warning: str
    detail_url: str
    checked_at: str


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def with_page(url: str, page: int) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k != "pn"]
    if page > 1:
        query.append(("pn", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def get(session: requests.Session, url: str) -> str:
    response = session.get(
        url,
        headers=HEADERS,
        timeout=CONFIG.get("timeout_seconds", 30),
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def _usable_detail_anchor(block: Tag) -> Tag | None:
    """Return a normal detail-page link from a result card."""
    candidates = []
    if block.name == "a" and block.get("href"):
        candidates.append(block)
    candidates.extend(block.find_all("a", href=True))

    for anchor in candidates:
        href = (anchor.get("href") or "").strip()
        lowered = href.lower()
        if not href or lowered.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin("https://partner.jal.co.jp/", href)
        if "search_result" in absolute:
            continue
        return anchor
    return None


def choose_detail_anchor(block: Tag, base_url: str) -> Tag | None:
    """Choose the most likely partner-detail link from a result card."""
    anchors = block.find_all("a", href=True)
    scored: list[tuple[int, Tag]] = []

    for anchor in anchors:
        raw = anchor.get("href", "").strip()
        if not raw or raw.startswith(("#", "javascript:", "mailto:")):
            continue

        absolute = urljoin(base_url, raw)
        lower = absolute.lower()

        # Logo/image files are not detail pages.
        if re.search(r"\.(?:jpg|jpeg|png|gif|webp|svg)(?:\?.*)?$", lower):
            continue
        if "search_result" in lower:
            continue

        score = 0
        anchor_text = normalize(anchor.get_text(" ", strip=True))
        if anchor_text:
            score += 4
        if "partner.jal.co.jp" in lower:
            score += 2
        if any(token in lower for token in ("shop", "detail", "partner", "emile")):
            score += 1

        scored.append((score, anchor))

    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def result_blocks(soup: BeautifulSoup) -> list[Tag]:
    """
    Collect result cards from their LSP marker.

    JAL splits the mileage sentence across nested HTML elements, so matching an
    individual text node misses every normal card. Each normal offer is inside
    an <li> and contains an LSP marker, which is a much more stable anchor.
    """
    found: list[Tag] = []
    seen: set[int] = set()

    marker_nodes = soup.find_all(
        string=lambda s: bool(
            s
            and "Life Status" in normalize(str(s))
            and "ポイント" in normalize(str(s))
        )
    )

    for node in marker_nodes:
        chosen: Tag | None = None
        current = node.parent

        if isinstance(current, Tag):
            li = current.find_parent("li")
            if isinstance(li, Tag):
                li_text = normalize(li.get_text(" ", strip=True))
                if "積算対象" in li_text and MILE_RULE_RE.search(li_text):
                    chosen = li

        if chosen is None:
            current = node.parent
            for _ in range(10):
                if not isinstance(current, Tag):
                    break
                block_text = normalize(current.get_text(" ", strip=True))
                marker_count = block_text.count("Life Status")
                rule_count = len(MILE_RULE_RE.findall(block_text))
                if "積算対象" in block_text and rule_count == 1:
                    chosen = current
                    if marker_count == 1:
                        break
                current = current.parent

        if chosen is not None and id(chosen) not in seen:
            seen.add(id(chosen))
            found.append(chosen)

    for anchor in soup.find_all("a", href=True):
        anchor_text = normalize(anchor.get_text(" ", strip=True))
        if (
            "Life Status" in anchor_text
            and "積算対象" in anchor_text
            and MILE_RULE_RE.search(anchor_text)
            and id(anchor) not in seen
        ):
            seen.add(id(anchor))
            found.append(anchor)

    return found

def extract_service_and_condition(block: Tag) -> tuple[str, str]:
    text = normalize(block.get_text(" ", strip=True))
    rule = MILE_RULE_RE.search(text)

    if rule:
        condition = rule.group(0)
        before = text[: rule.start()].strip(" -–—|")
    else:
        fixed = FIXED_MILES_RE.search(text)
        condition = fixed.group(0) if fixed else ""
        before = text[: fixed.start()].strip(" -–—|") if fixed else text

    noise = (
        "JMB", "Life Status ポイント積算対象", "Life Statusポイント積算対象",
        "決済でマイルが2倍", "JALカード", "JAL Pay"
    )
    service = before
    for token in noise:
        service = service.replace(token, " ")
    service = normalize(service)

    # Image alt text or campaign badges can appear before the actual name.
    service = re.sub(r"^(?:Image:?|[0-9]+倍)\s*", "", service, flags=re.I)
    return service or "名称取得失敗", condition


def parse_campaign_end(text: str) -> str:
    candidates: list[tuple[int, str]] = []
    for match in DATE_RE.finditer(text):
        y, m, d = map(int, match.groups())
        context = text[max(0, match.start() - 30):match.end() + 20]
        priority = 0 if any(k in context for k in ("終了", "まで", "期間")) else 1
        candidates.append((priority, f"{y:04d}-{m:02d}-{d:02d}"))
    return sorted(candidates)[0][1] if candidates else ""


def calculate(text: str, condition: str) -> dict:
    match = MILE_RULE_RE.search(condition or text)
    minimum_match = MIN_SPEND_RE.search(text)
    minimum_spend = int(minimum_match.group("yen").replace(",", "")) if minimum_match else None

    result = {
        "unit_yen": None,
        "unit_miles": None,
        "spend_for_1_lsp": None,
        "miles_at_1_lsp": None,
        "lsp_at_minimum": None,
        "minimum_spend": minimum_spend,
    }

    if match:
        yen = int(match.group("yen").replace(",", ""))
        miles = int(match.group("miles").replace(",", ""))
        units = math.ceil(100 / miles)
        spend = units * yen
        earned = units * miles
        result.update(
            unit_yen=yen,
            unit_miles=miles,
            spend_for_1_lsp=spend,
            miles_at_1_lsp=earned,
        )
        if minimum_spend:
            min_units = minimum_spend // yen
            result["lsp_at_minimum"] = (min_units * miles) / 100
    return result


def parse_detail(session: requests.Session, url: str) -> tuple[str, str]:
    try:
        detail_html = get(session, url)
        soup = BeautifulSoup(detail_html, "lxml")
        return normalize(soup.get_text(" ", strip=True)), ""
    except Exception as exc:
        return "", f"詳細ページ取得失敗: {type(exc).__name__}"


def scrape() -> list[Offer]:
    session = requests.Session()
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    offers_by_key: dict[tuple[str, str], Offer] = {}

    for page in range(1, int(CONFIG.get("pages", 4)) + 1):
        search_url = with_page(CONFIG["search_url"], page)
        search_html = get(session, search_url)
        debug_dir = ROOT / "data" / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"search-page-{page}.html").write_text(search_html, encoding="utf-8")
        soup = BeautifulSoup(search_html, "lxml")
        blocks = result_blocks(soup)
        print(f"page {page}: {len(blocks)} offers found")

        for block in blocks:
            block_text = normalize(block.get_text(" ", strip=True))
            service, condition = extract_service_and_condition(block)

            anchor = choose_detail_anchor(block, search_url)
            detail_url = search_url
            if anchor:
                raw_href = anchor.get("href", "").strip()
                candidate_url = urljoin(search_url, raw_href)
                if urlsplit(candidate_url).scheme in ("http", "https"):
                    detail_url = candidate_url
                else:
                    anchor = None

            # The search-result page alone is enough for the basic row.
            # Detail-page enrichment is attempted only when a credible link exists.
            detail_text = ""
            fetch_warning = ""
            if anchor:
                detail_text, fetch_warning = parse_detail(session, detail_url)

            combined = normalize(f"{block_text} {detail_text}")
            calc = calculate(combined, condition)

            warnings: list[str] = []
            if not anchor:
                warnings.append("詳細ページURLを自動取得できません")
            elif fetch_warning:
                warnings.append(fetch_warning)

            if any(p in combined for p in EXCLUSION_PHRASES):
                warnings.append("Mileage Parkの100マイル=1LSP対象外の可能性。個別ルールを要確認")
                calc["spend_for_1_lsp"] = None
                calc["miles_at_1_lsp"] = None
                calc["lsp_at_minimum"] = None

            if calc["unit_yen"] is None:
                warnings.append("金額比例ルールを自動計算できません")
            if "キャンペーン" in combined and not parse_campaign_end(combined):
                warnings.append("キャンペーン終了日を自動取得できません")
            if service == "名称取得失敗":
                warnings.append("サービス名を要確認")

            first_only = any(p in combined for p in FIRST_ONLY_PHRASES)
            key = (service, condition)

            offers_by_key[key] = Offer(
                service=service,
                condition=condition,
                unit_yen=calc["unit_yen"],
                unit_miles=calc["unit_miles"],
                spend_for_1_lsp=calc["spend_for_1_lsp"],
                miles_at_1_lsp=calc["miles_at_1_lsp"],
                lsp_at_minimum=calc["lsp_at_minimum"],
                minimum_spend=calc["minimum_spend"],
                first_only=first_only,
                campaign_end=parse_campaign_end(combined),
                warning=" / ".join(dict.fromkeys(warnings)),
                detail_url=detail_url,
                checked_at=checked_at,
            )

            if anchor:
                time.sleep(float(CONFIG.get("request_interval_seconds", 1.0)))

        time.sleep(float(CONFIG.get("request_interval_seconds", 1.0)))

    offers = sorted(
        offers_by_key.values(),
        key=lambda x: (
            x.spend_for_1_lsp is None,
            x.spend_for_1_lsp if x.spend_for_1_lsp is not None else 10**12,
            x.service,
        ),
    )

    # A silent partial scrape is worse than a visible failure.
    expected_minimum = int(CONFIG.get("expected_minimum_offers", 20))
    if len(offers) < expected_minimum:
        raise RuntimeError(
            f"取得件数が少なすぎます: {len(offers)}件 "
            f"(最低想定 {expected_minimum}件)。JAL側のHTML変更を確認してください。"
        )

    return offers

def write_csv(offers: Iterable[Offer]) -> None:
    path = ROOT / "data" / "offers.csv"
    rows = [asdict(o) for o in offers]
    fieldnames = list(Offer.__dataclass_fields__)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def fmt_yen(value: int | None) -> str:
    return "要確認" if value is None else f"¥{value:,}"


def fmt_num(value) -> str:
    return "" if value is None else str(value)


def write_html(offers: list[Offer]) -> None:
    updated = offers[0].checked_at if offers else datetime.now().astimezone().isoformat(timespec="seconds")
    rows = []
    for offer in offers:
        tags = []
        if offer.first_only:
            tags.append('<span class="tag first">初回系</span>')
        if offer.warning:
            tags.append('<span class="tag warn">要確認</span>')
        else:
            tags.append('<span class="tag ok">自動計算</span>')

        rows.append(f"""
        <tr data-search="{html.escape((offer.service + ' ' + offer.condition + ' ' + offer.warning).lower())}"
            data-cost="{offer.spend_for_1_lsp if offer.spend_for_1_lsp is not None else 999999999}">
          <td class="service"><a href="{html.escape(offer.detail_url)}" target="_blank" rel="noopener">{html.escape(offer.service)}</a>
            <div class="tags">{''.join(tags)}</div>
          </td>
          <td>{html.escape(offer.condition or '要確認')}</td>
          <td class="number strong">{fmt_yen(offer.spend_for_1_lsp)}</td>
          <td class="number">{fmt_num(offer.miles_at_1_lsp)}</td>
          <td class="number">{fmt_yen(offer.minimum_spend) if offer.minimum_spend else ''}</td>
          <td>{html.escape(offer.campaign_end)}</td>
          <td class="warning">{html.escape(offer.warning)}</td>
        </tr>""")

    page = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JAL LSP Checker</title>
<style>
:root {{ color-scheme: light dark; --bg:#f5f5f7; --card:#fff; --text:#191919; --sub:#666; --line:#ddd; --accent:#b50018; }}
@media (prefers-color-scheme:dark) {{
  :root {{ --bg:#111; --card:#1b1b1d; --text:#f5f5f5; --sub:#aaa; --line:#3a3a3a; --accent:#ff5a6f; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:system-ui,-apple-system,"Segoe UI","Noto Sans JP",sans-serif; background:var(--bg); color:var(--text); }}
header {{ padding:20px 16px 12px; max-width:1300px; margin:auto; }}
h1 {{ margin:0 0 6px; font-size:1.55rem; }}
.note {{ color:var(--sub); font-size:.88rem; line-height:1.5; }}
.controls {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:14px; }}
input,select {{ padding:10px 12px; border:1px solid var(--line); border-radius:10px; background:var(--card); color:var(--text); font-size:16px; }}
input {{ flex:1; min-width:220px; }}
main {{ max-width:1300px; margin:auto; padding:0 10px 28px; }}
.table-wrap {{ overflow:auto; background:var(--card); border:1px solid var(--line); border-radius:14px; }}
table {{ width:100%; border-collapse:collapse; min-width:1050px; }}
th,td {{ padding:11px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; font-size:.9rem; }}
th {{ position:sticky; top:0; background:var(--card); z-index:1; white-space:nowrap; }}
.number {{ text-align:right; white-space:nowrap; }}
.strong {{ font-weight:800; font-size:1rem; }}
.service {{ min-width:190px; font-weight:650; }}
a {{ color:var(--accent); }}
.warning {{ min-width:260px; color:var(--sub); font-size:.82rem; }}
.tags {{ margin-top:6px; display:flex; gap:4px; }}
.tag {{ border-radius:999px; padding:2px 7px; font-size:.7rem; font-weight:600; }}
.tag.ok {{ background:#dff5e5; color:#176b2c; }}
.tag.warn {{ background:#fff0cc; color:#775400; }}
.tag.first {{ background:#e9e1ff; color:#56358c; }}
footer {{ max-width:1300px; margin:auto; padding:0 16px 30px; color:var(--sub); font-size:.8rem; }}
</style>
</head>
<body>
<header>
  <h1>JAL LSP Checker</h1>
  <div class="note">
    JAL Mileage Parkの検索結果と詳細ページから自動生成。<br>
    「1LSP必要額」は、通常のMileage Parkルール（100マイル=1LSP）で100マイル以上になる最小課金単位。
    個別LSPルール・固定ボーナス・除外条件は「要確認」として計算を止めます。<br>
    最終更新: {html.escape(updated)}
  </div>
  <div class="controls">
    <input id="q" type="search" placeholder="サービス名・条件・警告を検索">
    <select id="filter">
      <option value="all">すべて</option>
      <option value="auto">自動計算のみ</option>
      <option value="warn">要確認のみ</option>
      <option value="first">初回系のみ</option>
    </select>
    <select id="sort">
      <option value="cost">1LSP必要額が安い順</option>
      <option value="name">サービス名順</option>
    </select>
  </div>
</header>
<main>
<div class="table-wrap">
<table id="offers">
<thead><tr>
<th>サービス</th><th>マイル条件</th><th>1LSP必要額</th><th>獲得マイル</th>
<th>最低利用額</th><th>終了日</th><th>注意</th>
</tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
</div>
</main>
<footer>
自動抽出結果なので、利用前には必ずJALの詳細ページで最新条件を確認してください。
</footer>
<script>
const tbody = document.querySelector('#offers tbody');
const rows = [...tbody.querySelectorAll('tr')];
const q = document.querySelector('#q');
const filter = document.querySelector('#filter');
const sort = document.querySelector('#sort');

function refresh() {{
  const term = q.value.trim().toLowerCase();
  const mode = filter.value;
  rows.forEach(row => {{
    const textOK = !term || row.dataset.search.includes(term);
    const hasWarn = row.querySelector('.tag.warn');
    const hasFirst = row.querySelector('.tag.first');
    const modeOK = mode === 'all' || (mode === 'auto' && !hasWarn) ||
                   (mode === 'warn' && hasWarn) || (mode === 'first' && hasFirst);
    row.hidden = !(textOK && modeOK);
  }});

  const sorted = [...rows].sort((a,b) => {{
    if (sort.value === 'name') return a.cells[0].innerText.localeCompare(b.cells[0].innerText, 'ja');
    return Number(a.dataset.cost) - Number(b.dataset.cost);
  }});
  sorted.forEach(row => tbody.appendChild(row));
}}
q.addEventListener('input', refresh);
filter.addEventListener('change', refresh);
sort.addEventListener('change', refresh);
refresh();
</script>
</body>
</html>"""
    (ROOT / "docs" / "index.html").write_text(page, encoding="utf-8")


def main() -> None:
    offers = scrape()
    if not offers:
        raise RuntimeError(
            "対象案件を1件も取得できませんでした。JAL側のHTML変更または一時的なアクセス制限が考えられます。"
        )
    write_csv(offers)
    write_html(offers)
    print(f"wrote {len(offers)} offers")


if __name__ == "__main__":
    main()
