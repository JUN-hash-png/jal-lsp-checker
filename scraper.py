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
DATE_RANGE_RE = re.compile(
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"\s*[～〜~－—-]\s*"
    r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)
FEATURE_URL_RE = re.compile(
    r"(?:https?://partner\.jal\.co\.jp)?"
    r"(?P<path>/jmb/partner/feature/\d+/?)(?:[\"'?#\s]|$)"
)
EXCLUSION_PHRASES = (
    "JALマイレージパーク経由によるマイル・Life Statusポイントは積算対象外",
    "JALマイレージパーク経由によるマイル・Life Status ポイントは積算対象外",
)
FIRST_ONLY_PHRASES = (
    "初回のみ", "初回購入", "初回利用", "初めてご利用", "新規購入",
    "新規入会", "新規登録", "新規申込",
)



FIRST_CONDITION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("定期初回のみ", (
        "定期購入制度", "定期購入", "定期コース", "定期便", "定期お届け",
    )),
    ("初回購入のみ", (
        "初回購入", "初回のご購入", "初めてのご購入", "初回のみ",
    )),
    ("初回利用のみ", (
        "初回利用", "初めてご利用", "初めてのご利用", "初回レンタル",
    )),
    ("新規会員限定", (
        "新規会員", "新規入会", "新規登録",
    )),
    ("新規申込限定", (
        "新規申込", "新規お申し込み", "新規契約", "新規成約",
    )),
)

FIRST_SENTENCE_RE = re.compile(
    r"([^。！？]{0,80}(?:初回|初めて|新規)[^。！？]{0,120}[。！？]?)"
)

CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("PC・ソフト", (
        "dell", "デル", "hp", "fmv", "dynabook", "ノートン", "トレンドマイクロ",
        "ソースネクスト", "ポケトーク", "パソコン", "セキュリティ", "ウイルスバスター",
    )),
    ("美容・コスメ", (
        "dhc", "クラランス", "nars", "koh gen do", "江原道", "サニーヘルス",
        "サントリーウエルネス", "化粧品", "コスメ", "美容", "スキンケア",
    )),
    ("食品・健康", (
        "山田養蜂場", "食品", "グルメ", "健康食品", "サプリ", "はちみつ",
        "ショップジャパン", "サントリー",
    )),
    ("ファッション", (
        "aoki", "bonaventura", "グンゼ", "メガネ", "オンデーズ", "眼鏡",
        "アディダス", "adidas", "ファッション", "バッグ", "衣料",
    )),
    ("花・ギフト", (
        "イイハナ", "e87", "リンベル", "ギフト", "フラワー", "花",
    )),
    ("旅行・交通", (
        "jal abc", "旅行", "ホテル", "レンタカー", "空港", "宅配", "手荷物",
    )),
    ("ふるさと納税", (
        "ふるさと納税", "自治体",
    )),
    ("家電・通販", (
        "高島屋", "イオン", "通販", "家電", "オンラインショップ", "e shop",
    )),
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
    detail_found: bool
    category: str
    status: str
    first_condition_type: str
    first_condition_note: str
    previous_cost: int | None
    change_note: str
    checked_at: str
    image_url: str


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


def find_detail_url(block: Tag, base_url: str) -> str | None:
    """
    Find the destination URL belonging to one result card.

    JAL uses several link shapes, not only /jmb/partner/feature/.
    Prefer links inside the result block and image/name anchors, then inspect
    only the nearest wrappers. Avoid page navigation and generic search links.
    """
    scopes: list[Tag] = [block]

    parent = block.parent
    for _ in range(3):
        if not isinstance(parent, Tag):
            break
        scopes.append(parent)
        parent = parent.parent

    rejected_parts = (
        "#",
        "javascript:",
        "/jmb/index.html",
        "/jmb/partner/index",
        "/jmb/partner/search",
        "?pn=",
    )

    def acceptable(raw_href: str) -> str | None:
        href = raw_href.strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "tel:")):
            return None

        absolute = urljoin(base_url, href)
        parsed = urlsplit(absolute)

        if any(part in absolute for part in rejected_parts):
            return None
        if parsed.scheme not in ("http", "https"):
            return None

        if any(token in parsed.path for token in (
            "/jmb/partner/",
            "/jmb/mileage/",
            "/jmb/campaign/",
        )):
            return absolute

        if parsed.netloc and parsed.netloc != urlsplit(base_url).netloc:
            return absolute

        return None

    for scope_index, scope in enumerate(scopes):
        anchors = []
        if scope.name == "a":
            anchors.append(scope)
        anchors.extend(scope.find_all("a", href=True))

        if scope_index > 0:
            local_anchors = []
            for anchor in anchors:
                if block in anchor.descendants or anchor is block.parent:
                    local_anchors.append(anchor)
            if local_anchors:
                anchors = local_anchors

        for anchor in anchors:
            candidate = acceptable(str(anchor.get("href", "")))
            if candidate:
                return candidate

    candidates: list[Tag] = [block]
    candidates.extend(block.find_all(True))
    for tag in candidates:
        for attr_value in tag.attrs.values():
            values = attr_value if isinstance(attr_value, list) else [attr_value]
            for value in values:
                raw = str(value)

                match = FEATURE_URL_RE.search(raw)
                if match:
                    return urljoin(base_url, match.group("path"))

                for found in re.findall(
                    r'(?:https?://[^\\s"\'<>]+|/[A-Za-z0-9_./?=&%+\\-]+)',
                    raw,
                ):
                    candidate = acceptable(found)
                    if candidate:
                        return candidate

    return None


def find_image_url(block: Tag, base_url: str) -> str:
    """Return the most likely merchant/card image URL from a result block."""
    candidates: list[Tag] = [block]
    parent = block.parent
    for _ in range(2):
        if not isinstance(parent, Tag):
            break
        candidates.append(parent)
        parent = parent.parent

    for scope in candidates:
        for img in scope.find_all("img"):
            raw = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-original")
                or img.get("data-lazy")
                or ""
            )
            raw = str(raw).strip()
            if not raw or raw.startswith("data:"):
                continue
            return urljoin(base_url, raw)
    return ""

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
    service = re.sub(r"\s*(?:商品のご購入|サービスのご利用|ご利用)\s*$", "", service)
    service = re.sub(r"\s*商品(?:の)?ご購入\s*$", "", service)
    return service or "名称取得失敗", condition




def classify_first_condition(text: str) -> tuple[str, str]:
    """
    Return a human-readable first-condition type and the relevant sentence.
    Example:
      「定期購入制度」でのご購入は、初回のみマイル積算の対象です。
      -> ("定期初回のみ", that sentence)
    """
    normalized = normalize(text)
    if not any(token in normalized for token in ("初回", "初めて", "新規")):
        return "", ""

    note_match = FIRST_SENTENCE_RE.search(normalized)
    note = note_match.group(1).strip() if note_match else ""

    # Prefer contextual classifications over the generic "初回のみ".
    for label, keywords in FIRST_CONDITION_RULES:
        if label == "定期初回のみ":
            if any(k in normalized for k in keywords) and "初回" in normalized:
                return label, note
        elif any(k in normalized for k in keywords):
            return label, note

    return "初回条件あり", note


def categorize(service: str, detail_text: str = "") -> str:
    haystack = normalize(f"{service} {detail_text}").lower()
    for category, keywords in CATEGORY_RULES:
        if any(keyword.lower() in haystack for keyword in keywords):
            return category
    return "その他"


def offer_identity(
    service: str,
    detail_url: str,
    detail_found: bool,
    condition: str = "",
) -> str:
    """
    Stable per-offer identity.

    Detail URLs are not reliable enough to use as the sole key: JAL sometimes
    exposes the same wrapper URL to multiple cards. Use normalized service name
    and mileage condition so favorites and change tracking stay independent.
    """
    normalized_service = re.sub(
        r"[^0-9a-zA-Zぁ-んァ-ヶ一-龠]+",
        "",
        service.lower(),
    )
    normalized_condition = re.sub(
        r"[^0-9a-zA-Zぁ-んァ-ヶ一-龠]+",
        "",
        condition.lower(),
    )
    return f"{normalized_service}::{normalized_condition}"

def load_previous_offers() -> dict[str, dict[str, str]]:
    path = ROOT / "data" / "offers.csv"
    if not path.exists():
        return {}

    previous: dict[str, dict[str, str]] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                service = row.get("service", "")
                detail_url = row.get("detail_url", "")
                detail_found = str(row.get("detail_found", "")).lower() in ("true", "1", "yes")
                key = offer_identity(service, detail_url, detail_found, row.get("condition", ""))
                if key:
                    previous[key] = row
    except Exception as exc:
        print(f"Previous CSV could not be read: {type(exc).__name__}")
    return previous


def detect_status(
    offer: Offer,
    previous: dict[str, dict[str, str]],
) -> tuple[str, int | None, str]:
    key = offer_identity(offer.service, offer.detail_url, offer.detail_found, offer.condition)
    old = previous.get(key)
    if old is None:
        normalized_service = re.sub(
            r"[^0-9a-zA-Zぁ-んァ-ヶ一-龠]+",
            "",
            offer.service.lower(),
        )
        matches = [
            row for row in previous.values()
            if re.sub(
                r"[^0-9a-zA-Zぁ-んァ-ヶ一-龠]+",
                "",
                str(row.get("service", "")).lower(),
            ) == normalized_service
        ]
        if len(matches) == 1:
            old = matches[0]

    if old is None:
        return "new", None, "新規掲載"

    old_cost_raw = str(old.get("spend_for_1_lsp", "")).strip()
    try:
        old_cost = int(old_cost_raw) if old_cost_raw else None
    except ValueError:
        old_cost = None

    changes: list[str] = []

    if str(old.get("condition", "")) != offer.condition:
        changes.append("マイル条件")

    current_cost = "" if offer.spend_for_1_lsp is None else str(offer.spend_for_1_lsp)
    if old_cost_raw != current_cost:
        if old_cost is not None and offer.spend_for_1_lsp is not None:
            diff = offer.spend_for_1_lsp - old_cost
            if diff < 0:
                changes.append(f"単価改善 ¥{abs(diff):,}")
            elif diff > 0:
                changes.append(f"単価悪化 ¥{diff:,}")
        else:
            changes.append("LSP単価")

    if str(old.get("campaign_end", "")) != offer.campaign_end:
        changes.append("終了日")

    if changes:
        return "changed", old_cost, " / ".join(changes)
    return "same", old_cost, ""


def write_history(offers: list[Offer]) -> None:
    """Append one daily snapshot per offer to data/history.csv."""
    path = ROOT / "data" / "history.csv"
    today = datetime.now().astimezone().date().isoformat()
    existing_rows: list[dict[str, str]] = []
    existing_keys: set[tuple[str, str]] = set()

    if path.exists():
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                existing_rows = list(csv.DictReader(f))
                existing_keys = {
                    (row.get("date", ""), row.get("identity", ""))
                    for row in existing_rows
                }
        except Exception as exc:
            print(f"History CSV could not be read: {type(exc).__name__}")
            existing_rows = []
            existing_keys = set()

    for offer in offers:
        identity = offer_identity(offer.service, offer.detail_url, offer.detail_found, offer.condition)
        key = (today, identity)
        if key in existing_keys:
            continue
        existing_rows.append({
            "date": today,
            "identity": identity,
            "service": offer.service,
            "category": offer.category,
            "condition": offer.condition,
            "spend_for_1_lsp": "" if offer.spend_for_1_lsp is None else str(offer.spend_for_1_lsp),
            "campaign_end": offer.campaign_end,
            "detail_url": offer.detail_url if offer.detail_found else "",
        })
        existing_keys.add(key)

    fieldnames = [
        "date", "identity", "service", "category", "condition",
        "spend_for_1_lsp", "campaign_end", "detail_url",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(existing_rows)

def parse_campaign_end(text: str) -> str:
    # Prefer the end of an explicit date range, e.g. 2026年5月1日～8月2日.
    ranges = list(DATE_RANGE_RE.finditer(text))
    if ranges:
        match = ranges[-1]
        start_year, _, _, end_year, end_month, end_day = match.groups()
        year = int(end_year or start_year)
        return f"{year:04d}-{int(end_month):02d}-{int(end_day):02d}"

    candidates: list[tuple[int, str]] = []
    for match in DATE_RE.finditer(text):
        y, m, d = map(int, match.groups())
        context = text[max(0, match.start() - 40):match.end() + 30]
        priority = 0 if any(k in context for k in ("終了", "まで", "期限")) else 1
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
    previous = load_previous_offers()
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
            image_url = find_image_url(block, search_url)

            detail_url = find_detail_url(block, search_url)
            detail_found = detail_url is not None
            if not detail_url:
                detail_url = search_url

            detail_text = ""
            fetch_warning = ""
            if detail_found:
                detail_text, fetch_warning = parse_detail(session, detail_url)

            combined = normalize(f"{block_text} {detail_text}")
            calc = calculate(combined, condition)

            warnings: list[str] = []
            if fetch_warning:
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

            first_condition_type, first_condition_note = classify_first_condition(combined)
            first_only = bool(first_condition_type)
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
                detail_found=detail_found,
                category=categorize(service, detail_text),
                status="same",
                first_condition_type=first_condition_type,
                first_condition_note=first_condition_note,
                previous_cost=None,
                change_note="",
                checked_at=checked_at,
                image_url=image_url,
            )

            if detail_found:
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

    for offer in offers:
        offer.status, offer.previous_cost, offer.change_note = detect_status(offer, previous)

    current_keys = {
        offer_identity(o.service, o.detail_url, o.detail_found, o.condition)
        for o in offers
    }
    ended_rows = [
        row for key, row in previous.items()
        if key not in current_keys
    ]
    ended_path = ROOT / "data" / "ended.csv"
    if ended_rows:
        with ended_path.open("w", encoding="utf-8-sig", newline="") as f:
            fieldnames = sorted({k for row in ended_rows for k in row.keys()})
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(ended_rows)
    elif ended_path.exists():
        ended_path.unlink()

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



def days_remaining(date_text: str) -> int | None:
    if not date_text:
        return None
    try:
        end_date = datetime.strptime(date_text, "%Y-%m-%d").date()
        return (end_date - datetime.now().astimezone().date()).days
    except ValueError:
        return None


def write_html(offers: list[Offer]) -> None:
    updated = offers[0].checked_at if offers else datetime.now().astimezone().isoformat(timespec="seconds")
    total = len(offers)
    priced_count = sum(1 for o in offers if o.spend_for_1_lsp is not None)
    detail_count = sum(1 for o in offers if o.detail_found)
    first_count = sum(1 for o in offers if o.first_only)
    new_offers = [o for o in offers if o.status == "new"]
    changed_offers = [o for o in offers if o.status == "changed"]
    new_count = len(new_offers)
    changed_count = len(changed_offers)

    ended_rows: list[dict[str, str]] = []
    ended_path = ROOT / "data" / "ended.csv"
    if ended_path.exists():
        try:
            with ended_path.open("r", encoding="utf-8-sig", newline="") as f:
                ended_rows = list(csv.DictReader(f))
        except Exception:
            ended_rows = []
    ended_count = len(ended_rows)

    categories = sorted({o.category for o in offers})
    category_buttons = ''.join(
        f'<button type="button" class="cat-btn" data-category="{html.escape(c)}">{html.escape(c)}</button>'
        for c in categories
    )

    change_items: list[str] = []
    for offer in new_offers[:5]:
        change_items.append(
            f'<li><span class="mini-tag new">NEW</span>{html.escape(offer.service)}</li>'
        )
    for offer in changed_offers[:8]:
        change_items.append(
            f'<li><span class="mini-tag changed">変更</span>{html.escape(offer.service)}'
            f'<small>{html.escape(offer.change_note)}</small></li>'
        )
    for row in ended_rows[:5]:
        change_items.append(
            f'<li><span class="mini-tag ended">終了</span>{html.escape(row.get("service", "名称不明"))}</li>'
        )
    changes_html = (
        '<ul class="change-list">' + ''.join(change_items) + '</ul>'
        if change_items else '<div class="no-change">前回から実質的な条件変更はありません。</div>'
    )

    rows = []
    ranked = sorted(
        [o for o in offers if o.spend_for_1_lsp is not None],
        key=lambda o: (o.spend_for_1_lsp or 10**12, o.service),
    )
    rank_map = {
        offer_identity(o.service, o.detail_url, o.detail_found, o.condition): rank
        for rank, o in enumerate(ranked, start=1)
    }

    for offer in offers:
        tags = [f'<span class="tag category">{html.escape(offer.category)}</span>']
        if offer.status == "new":
            tags.append('<span class="tag new">NEW</span>')
        elif offer.status == "changed":
            change_class = "improved" if "改善" in offer.change_note else "changed"
            tags.append(
                f'<span class="tag {change_class}" title="{html.escape(offer.change_note)}">'
                f'{html.escape(offer.change_note or "条件変更")}</span>'
            )

        if offer.first_condition_type:
            first_class = "periodic" if offer.first_condition_type == "定期初回のみ" else "first"
            tags.append(
                f'<span class="tag {first_class}" title="{html.escape(offer.first_condition_note)}">'
                f'{html.escape(offer.first_condition_type)}</span>'
            )

        remaining = days_remaining(offer.campaign_end)
        if remaining is not None:
            if remaining < 0:
                tags.append('<span class="tag expired">終了日経過</span>')
            elif remaining <= 7:
                tags.append(f'<span class="tag soon">あと{remaining}日</span>')

        if not offer.detail_found:
            tags.append('<span class="tag linkless">検索結果のみ</span>')
        if offer.warning:
            tags.append('<span class="tag warn">要確認</span>')
        else:
            tags.append('<span class="tag ok">単価算出済み</span>')

        identity = offer_identity(offer.service, offer.detail_url, offer.detail_found, offer.condition)
        rank = rank_map.get(identity)
        if rank == 1:
            rank_html = '<span class="rank medal">🥇 1位</span>'
        elif rank == 2:
            rank_html = '<span class="rank medal">🥈 2位</span>'
        elif rank == 3:
            rank_html = '<span class="rank medal">🥉 3位</span>'
        else:
            rank_html = f'<span class="rank">#{rank}</span>' if rank and rank <= 20 else ""

        link_label = html.escape(offer.service)
        merchant_icon = (
            f'<img class="merchant-icon" src="{html.escape(offer.image_url, quote=True)}" '
            f'alt="" loading="lazy" referrerpolicy="no-referrer">'
            if offer.image_url else
            f'<span class="merchant-icon placeholder" aria-hidden="true">{html.escape(offer.service[:1] or "・")}</span>'
        )
        service_html = (
            f'<a href="{html.escape(offer.detail_url)}" target="_blank" rel="noopener">{link_label}</a>'
            if offer.detail_found
            else f'<span>{link_label}</span>'
        )

        favorite_key = html.escape(identity, quote=True)
        lsp_per_1000 = (
            1000 / offer.spend_for_1_lsp
            if offer.spend_for_1_lsp not in (None, 0)
            else None
        )
        lsp_per_1000_text = "" if lsp_per_1000 is None else f"{lsp_per_1000:.2f}"

        condition_note_html = ""
        if offer.first_condition_note:
            condition_note_html = (
                f'<details class="condition-note"><summary>初回条件の詳細</summary>'
                f'<div>{html.escape(offer.first_condition_note)}</div></details>'
            )

        search_blob = normalize(
            f"{offer.service} {offer.condition} {offer.warning} {offer.category} "
            f"{offer.first_condition_type} {offer.first_condition_note} {offer.change_note} "
            f"{'新着 new' if offer.status == 'new' else ''} "
            f"{'変更 changed' if offer.status == 'changed' else ''}"
        ).lower()

        row_classes = []
        if offer.status == "new":
            row_classes.append("row-new")
        elif offer.status == "changed":
            row_classes.append("row-changed")
        if remaining is not None and 0 <= remaining <= 7:
            row_classes.append("row-soon")

        rows.append(f"""
        <tr class="{' '.join(row_classes)}" data-search="{html.escape(search_blob)}"
            data-cost="{offer.spend_for_1_lsp if offer.spend_for_1_lsp is not None else 999999999}"
            data-detail="{'yes' if offer.detail_found else 'no'}"
            data-warning="{'yes' if offer.warning else 'no'}"
            data-first="{'yes' if offer.first_only else 'no'}"
            data-category="{html.escape(offer.category)}"
            data-status="{offer.status}"
            data-rank="{rank if rank else 999999}"
            data-ending-soon="{'yes' if remaining is not None and 0 <= remaining <= 7 else 'no'}"
            data-favorite-key="{favorite_key}"
            data-favorite-cost="{offer.spend_for_1_lsp if offer.spend_for_1_lsp is not None else 0}"
            data-favorite-lsp="{'1' if offer.spend_for_1_lsp is not None else '0'}"
            data-done="no">
          <td class="favorite-cell" data-label="計画">
            <button class="favorite-btn" type="button" title="お気に入り">☆</button>
            <button class="done-btn" type="button" title="取得済みにする">✓</button>
          </td>
          <td class="service" data-label="サービス"><div class="service-line">{merchant_icon}<div class="service-main">{rank_html}{service_html}<div class="tags">{''.join(tags)}</div>{condition_note_html}</div></div></td>
          <td data-label="マイル条件">{html.escape(offer.condition or '要確認')}</td>
          <td class="number strong" data-label="1LSP必要額">{fmt_yen(offer.spend_for_1_lsp)}</td>
          <td class="number" data-label="1000円でLSP">{lsp_per_1000_text}</td>
          <td class="number" data-label="獲得マイル">{fmt_num(offer.miles_at_1_lsp)}</td>
          <td class="number" data-label="最低利用額">{fmt_yen(offer.minimum_spend) if offer.minimum_spend else ''}</td>
          <td data-label="終了日">{html.escape(offer.campaign_end)}</td>
          <td class="warning" data-label="注意">{html.escape(offer.warning)}</td>
        </tr>""")

    page = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="theme-color" content="#ffffff">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" href="icon.svg" type="image/svg+xml">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JAL LSP Checker</title>
<style>
:root {{ color-scheme: light dark; --bg:#f5f5f7; --card:#fff; --text:#191919; --sub:#666; --line:#ddd; --accent:#b50018; }}
@media (prefers-color-scheme:dark) {{
  :root {{ --bg:#111; --card:#1b1b1d; --text:#f5f5f5; --sub:#aaa; --line:#3a3a3a; --accent:#ff5a6f; }}
}}
:root[data-theme="light"] {{ --bg:#f7f7f8; --card:#fff; --text:#222; --sub:#666; --line:#ddd; --accent:#c90023; }}
:root[data-theme="dark"] {{ --bg:#111; --card:#1b1b1d; --text:#f5f5f5; --sub:#aaa; --line:#3a3a3a; --accent:#ff5a6f; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:system-ui,-apple-system,"Segoe UI","Noto Sans JP",sans-serif; background:var(--bg); color:var(--text); }}
header {{ padding:18px 12px 10px; max-width:1400px; margin:auto; }}
.title-row {{ display:flex; justify-content:space-between; align-items:center; gap:12px; }}
h1 {{ margin:0 0 6px; font-size:1.55rem; }}
.header-actions {{ display:flex; gap:7px; }}
.header-actions button, #resetView {{ border:1px solid var(--line); border-radius:9px; background:var(--card); color:var(--text); padding:7px 10px; cursor:pointer; }}
#themeToggle {{ min-width:40px; font-size:1rem; }}
.note {{ color:var(--sub); font-size:.86rem; line-height:1.5; }}
.stats {{ display:grid; grid-template-columns:repeat(4,minmax(120px,1fr)); gap:8px; margin-top:12px; }}
.stat {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:10px 12px; }}
.stat b {{ display:block; font-size:1.2rem; }}
.stat span {{ color:var(--sub); font-size:.75rem; }}
.changes {{ margin-top:10px; padding:10px 12px; border:1px solid var(--line); border-radius:12px; background:var(--card); font-size:.86rem; }}
.changes-head {{ display:flex; gap:14px; flex-wrap:wrap; font-weight:700; }}
.change-list {{ margin:9px 0 0; padding:0; list-style:none; display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:6px; }}
.change-list li {{ padding:7px 8px; border:1px solid var(--line); border-radius:8px; }}
.change-list small {{ display:block; color:var(--sub); margin:3px 0 0 43px; }}
.mini-tag {{ display:inline-block; min-width:38px; margin-right:6px; border-radius:999px; padding:2px 6px; font-size:.68rem; text-align:center; }}
.mini-tag.new {{ background:#ffe1e5; color:#a50018; }}
.mini-tag.changed {{ background:#fff0cc; color:#775400; }}
.mini-tag.ended {{ background:#e7e7e7; color:#555; }}
.no-change {{ margin-top:8px; color:var(--sub); }}
.my-plan {{ margin-top:10px; padding:11px 12px; border:1px solid var(--line); border-radius:12px; background:var(--card); display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; }}
.my-plan strong {{ margin-right:10px; }}
.my-plan span {{ color:var(--sub); }}
.plan-summary {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; }}
.target-box {{ display:flex; gap:7px; align-items:center; font-size:.82rem; }}
.target-box input {{ width:90px; padding:7px 8px; font-size:14px; }}
.target-box span {{ font-weight:700; color:var(--text); }}
.progress-wrap {{ min-width:220px; display:flex; align-items:center; gap:8px; }}
.progress-bar {{ width:150px; height:9px; border-radius:999px; background:var(--line); overflow:hidden; }}
.progress-bar span {{ display:block; width:0; height:100%; background:var(--accent); transition:width .2s ease; }}
#progressLabel {{ font-size:.75rem; white-space:nowrap; }}
.plan-actions {{ display:flex; gap:7px; flex-wrap:wrap; }}
.plan-actions button, .import-label {{ border:1px solid var(--line); border-radius:9px; background:var(--card); color:var(--text); padding:7px 10px; font-size:.8rem; cursor:pointer; }}
.import-label input {{ display:none; }}
.result-tools {{ display:flex; justify-content:space-between; align-items:center; gap:8px; margin-top:7px; }}
.result-count {{ color:var(--sub); font-size:.8rem; margin:0 2px; }}
#resetView {{ font-size:.75rem; padding:5px 8px; }}
.controls {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }}
input,select {{ padding:10px 12px; border:1px solid var(--line); border-radius:10px; background:var(--card); color:var(--text); font-size:16px; }}
input {{ flex:1; min-width:220px; }}
.today-summary {{ margin-top:10px; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
.today-summary span {{ border-radius:999px; padding:4px 9px; font-size:.76rem; font-weight:700; }}
.today-new {{ background:#ffe1e5; color:#a50018; }}
.today-changed {{ background:#fff0cc; color:#775400; }}
.today-ended {{ background:#e7e7e7; color:#555; }}
.categories {{ display:flex; gap:6px; overflow-x:auto; padding:10px 0 2px; }}
.quick-filters {{ display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; }}
.quick-btn {{ border:1px solid var(--line); border-radius:999px; background:var(--card); color:var(--text); padding:6px 10px; cursor:pointer; font-size:.78rem; }}
.quick-btn.active {{ border-color:var(--accent); color:var(--accent); font-weight:700; }}
.cat-btn {{ flex:0 0 auto; border:1px solid var(--line); border-radius:999px; background:var(--card); color:var(--text); padding:7px 11px; cursor:pointer; }}
.cat-btn.active {{ border-color:var(--accent); color:var(--accent); font-weight:700; }}
main {{ max-width:1400px; margin:auto; padding:0 8px 28px; }}
.table-wrap {{ overflow:auto; background:var(--card); border:1px solid var(--line); border-radius:14px; }}
table {{ width:100%; border-collapse:collapse; min-width:1250px; }}
th,td {{ padding:11px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; font-size:.9rem; }}
th {{ position:sticky; top:0; background:var(--card); z-index:1; white-space:nowrap; }}
.number {{ text-align:right; white-space:nowrap; }}
.strong {{ font-weight:800; font-size:1rem; }}
.service {{ min-width:240px; font-weight:650; }}
a {{ color:var(--accent); }}
.warning {{ min-width:220px; color:var(--sub); font-size:.82rem; }}
.tags {{ margin-top:6px; display:flex; gap:4px; flex-wrap:wrap; }}
.tag {{ border-radius:999px; padding:2px 7px; font-size:.7rem; font-weight:600; }}
.tag.ok {{ background:#dff5e5; color:#176b2c; }}
.tag.warn {{ background:#fff0cc; color:#775400; }}
.tag.first {{ background:#e9e1ff; color:#56358c; }}
.tag.periodic {{ background:#dcecff; color:#205b92; }}
.tag.linkless {{ background:#e7e7e7; color:#555; }}
.tag.category {{ background:#e4f0ff; color:#285b8c; }}
.tag.new {{ background:#ffe1e5; color:#a50018; }}
.tag.changed {{ background:#fff0cc; color:#775400; }}
.tag.improved {{ background:#dff5e5; color:#176b2c; }}
.rank {{ display:inline-block; min-width:32px; margin-right:6px; color:var(--sub); font-size:.78rem; }}
.rank.medal {{ color:var(--text); font-weight:800; }}
.service-line {{ display:flex; align-items:flex-start; gap:10px; }}
.service-main {{ min-width:0; }}
.merchant-icon {{ width:42px; height:42px; flex:0 0 42px; object-fit:contain; border-radius:8px; background:#fff; border:1px solid var(--line); }}
.merchant-icon.placeholder {{ display:flex; align-items:center; justify-content:center; background:var(--bg); color:var(--sub); font-weight:800; font-size:1.05rem; }}
.favorite-cell {{ width:42px; text-align:center; }}
.favorite-btn, .done-btn {{ border:0; background:transparent; color:#999; cursor:pointer; line-height:1; padding:0; }}
.favorite-btn {{ font-size:1.45rem; }}
.favorite-btn.active {{ color:#d39a00; }}
.done-btn {{ margin-left:4px; font-size:1.05rem; border:1px solid var(--line); border-radius:999px; width:22px; height:22px; }}
.done-btn.active {{ color:#176b2c; background:#dff5e5; border-color:#b7dfc1; }}
tr.done-row td.service > a,
tr.done-row td.service > span {{ text-decoration:line-through; opacity:.65; }}
tr.done-row {{ background:color-mix(in srgb, var(--card) 90%, #dff5e5); }}
.condition-note {{ margin-top:7px; font-weight:400; color:var(--sub); font-size:.78rem; }}
.condition-note summary {{ cursor:pointer; }}
.condition-note div {{ margin-top:4px; line-height:1.45; }}
#backToTop {{ position:fixed; right:16px; bottom:18px; width:44px; height:44px; border-radius:50%; border:1px solid var(--line); background:var(--card); color:var(--text); box-shadow:0 4px 18px rgba(0,0,0,.18); cursor:pointer; font-size:1.15rem; opacity:0; pointer-events:none; transform:translateY(8px); transition:.2s; z-index:10; }}
#backToTop.visible {{ opacity:1; pointer-events:auto; transform:none; }}
footer {{ max-width:1400px; margin:auto; padding:0 12px 30px; color:var(--sub); font-size:.8rem; }}

@media (max-width:700px) {{
  header {{ padding-top:12px; }}
  .stats {{ grid-template-columns:repeat(2,1fr); }}
  .my-plan {{ align-items:flex-start; }}
  .plan-actions {{ width:100%; }}
  .progress-wrap {{ width:100%; }}
  .progress-bar {{ flex:1; }}
  .controls {{ display:grid; grid-template-columns:1fr 1fr; }}
  .controls input {{ grid-column:1 / -1; width:100%; }}
  .table-wrap {{ overflow:visible; border:0; background:transparent; }}
  table {{ min-width:0; display:block; }}
  thead {{ display:none; }}
  tbody {{ display:grid; gap:10px; }}
  tr {{ display:grid; grid-template-columns:1fr 1fr; background:var(--card); border:1px solid var(--line); border-radius:13px; padding:10px; }}
  td {{ display:flex; justify-content:space-between; gap:12px; border-bottom:1px dashed var(--line); padding:8px 4px; text-align:right; }}
  td::before {{ content:attr(data-label); color:var(--sub); font-size:.76rem; font-weight:600; text-align:left; }}
  td.service {{ grid-column:1 / -1; display:block; text-align:left; border-bottom:1px solid var(--line); padding:3px 4px 10px; }}
  td.service::before {{ display:none; }}
  td.favorite-cell {{ position:absolute; right:22px; width:auto; border:0; z-index:2; }}
  td.favorite-cell::before {{ display:none; }}
  td.warning {{ grid-column:1 / -1; }}
  td:empty {{ display:none; }}
  .number {{ text-align:right; }}
}}
</style>
</head>
<body>
<header>
  <div class="title-row">
    <h1>JAL LSP Checker <small style="font-size:.55em;color:var(--sub)">Ver.2.0.3</small></h1>
    <div class="header-actions">
      <button type="button" id="themeToggle" title="表示テーマを切り替える">🌙</button>
      <button type="button" id="installApp" hidden>ホーム画面に追加</button>
    </div>
  </div>
  <div class="note">
    JAL Mileage Parkの検索結果・詳細ページから自動生成。通常案件は100マイル＝1LSPとして計算。<br>
    最終更新: {html.escape(updated)}
  </div>
  <div class="stats">
    <div class="stat"><b>{total}</b><span>掲載案件</span></div>
    <div class="stat"><b>{priced_count}</b><span>1LSP単価算出済み</span></div>
    <div class="stat"><b>{detail_count}</b><span>詳細ページ取得済み</span></div>
    <div class="stat"><b>{first_count}</b><span>初回条件あり</span></div>
  </div>
  <div class="today-summary">
    <strong>今日の更新</strong>
    <span class="today-new">NEW {new_count}</span>
    <span class="today-changed">変更 {changed_count}</span>
    <span class="today-ended">終了 {ended_count}</span>
  </div>
  <div class="changes">
    <div class="changes-head">
      <span>前回からの変化</span>
      <span>NEW {new_count}</span>
      <span>条件変更 {changed_count}</span>
      <span>掲載終了 {ended_count}</span>
    </div>
    {changes_html}
  </div>
  <div class="my-plan">
    <div class="plan-summary">
      <strong>お気に入りプラン</strong>
      <span id="favoriteSummary">0件・合計 ¥0・獲得目安 0LSP</span>
      <span id="doneSummary">取得済み 0件・0LSP</span>
    </div>
    <label class="target-box">
      残り目標LSP
      <input id="targetLsp" type="number" min="0" step="1" inputmode="numeric" placeholder="例：312">
      <span id="targetResult"></span>
    </label>
    <div class="progress-wrap" aria-label="LSP進捗">
      <div class="progress-bar"><span id="progressFill"></span></div>
      <span id="progressLabel">目標未設定</span>
    </div>
    <div class="plan-actions">
      <button type="button" id="exportFavorites">お気に入りJSON</button>
      <button type="button" id="exportFavoritesCsv">お気に入りCSV</button>
      <label class="import-label">読み込む<input type="file" id="importFavorites" accept="application/json"></label>
      <button type="button" id="clearFavorites">すべて解除</button>
    </div>
  </div>
  <div class="controls">
    <input id="q" type="search" placeholder="サービス名・PC・コスメ・定期初回などで検索">
    <select id="filter">
      <option value="all">すべて</option>
      <option value="favorite">お気に入りのみ</option>
      <option value="favorite-undone">未取得のお気に入り</option>
      <option value="done">取得済みのみ</option>
      <option value="top20">効率TOP20</option>
      <option value="soon">終了7日以内</option>
      <option value="new">NEWのみ</option>
      <option value="changed">条件変更のみ</option>
      <option value="priced">単価算出済み</option>
      <option value="warn">要確認のみ</option>
      <option value="detail">詳細ページ取得済み</option>
      <option value="linkless">検索結果のみ</option>
      <option value="first">初回条件あり</option>
    </select>
    <select id="sort">
      <option value="cost">1LSP必要額が安い順</option>
      <option value="lsp1000">1000円あたりLSPが多い順</option>
      <option value="name">サービス名順</option>
      <option value="end">終了日が近い順</option>
      <option value="status">NEW・変更を上に</option>
      <option value="favorite">お気に入りを上に</option>
    </select>
  </div>
  <div class="categories">
    <button type="button" class="cat-btn active" data-category="">全部</button>
    {category_buttons}
  </div>
  <div class="quick-filters">
    <button type="button" class="quick-btn" data-filter="first">初回条件</button>
    <button type="button" class="quick-btn" data-filter="soon">終了間近</button>
    <button type="button" class="quick-btn" data-filter="detail">詳細取得済み</button>
    <button type="button" class="quick-btn" data-filter="linkless">検索結果のみ</button>
    <button type="button" class="quick-btn" data-filter="favorite">お気に入り</button>
    <button type="button" class="quick-btn" data-filter="done">取得済み</button>
  </div>
  <div class="result-tools">
    <div class="result-count" id="resultCount"></div>
    <button type="button" id="resetView">絞り込みをリセット</button>
  </div>
</header>
<main>
<div class="table-wrap">
<table id="offers">
<thead><tr>
<th>★</th><th>サービス</th><th>マイル条件</th><th>1LSP必要額</th><th>1000円でLSP</th>
<th>獲得マイル</th><th>最低利用額</th><th>終了日</th><th>注意</th>
</tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
</div>
</main>
<button type="button" id="backToTop" aria-label="ページ先頭へ戻る">↑</button>
<footer>お気に入りはこの端末のブラウザ内に保存されます。利用前には必ずJAL公式ページで最新条件を確認してください。</footer>
<script>
const tbody = document.querySelector('#offers tbody');
const rows = [...tbody.querySelectorAll('tr')];
const q = document.querySelector('#q');
const filter = document.querySelector('#filter');
const sort = document.querySelector('#sort');
const categoryButtons = [...document.querySelectorAll('.cat-btn')];
const quickButtons = [...document.querySelectorAll('.quick-btn')];
const favoriteSummary = document.querySelector('#favoriteSummary');
const doneSummary = document.querySelector('#doneSummary');
const targetLsp = document.querySelector('#targetLsp');
const targetResult = document.querySelector('#targetResult');
const progressFill = document.querySelector('#progressFill');
const progressLabel = document.querySelector('#progressLabel');
const resultCount = document.querySelector('#resultCount');
const exportFavorites = document.querySelector('#exportFavorites');
const exportFavoritesCsv = document.querySelector('#exportFavoritesCsv');
const importFavorites = document.querySelector('#importFavorites');
const clearFavorites = document.querySelector('#clearFavorites');
const themeToggle = document.querySelector('#themeToggle');
const installApp = document.querySelector('#installApp');
const resetView = document.querySelector('#resetView');
const backToTop = document.querySelector('#backToTop');
const FAVORITES_KEY = 'jal-lsp-favorites-v2';
const DONE_KEY = 'jal-lsp-done-v1';
const TARGET_KEY = 'jal-lsp-target-v1';
function loadStoredSet(key) {{
  try {{
    const value = JSON.parse(localStorage.getItem(key) || '[]');
    return new Set(Array.isArray(value) ? value.map(String) : []);
  }} catch (error) {{
    console.warn(`保存データを初期化しました: ${{key}}`, error);
    localStorage.removeItem(key);
    return new Set();
  }}
}}

let favorites = loadStoredSet(FAVORITES_KEY);
let doneOffers = loadStoredSet(DONE_KEY);
if (targetLsp) targetLsp.value = localStorage.getItem(TARGET_KEY) || '';
let activeCategory = '';
const THEME_KEY = 'jal-lsp-theme-v1';
let deferredInstallPrompt = null;

function applyTheme(theme) {{
  document.documentElement.dataset.theme = theme;
  themeToggle.textContent = theme === 'dark' ? '☀️' : '🌙';
  themeToggle.title = theme === 'dark' ? 'ライト表示に切り替える' : 'ダーク表示に切り替える';
  document.querySelector('meta[name="theme-color"]').setAttribute(
    'content', theme === 'dark' ? '#111111' : '#ffffff'
  );
}}

const savedTheme = localStorage.getItem(THEME_KEY);
const initialTheme = savedTheme ||
  (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
applyTheme(initialTheme);

function saveFavorites() {{
  localStorage.setItem(FAVORITES_KEY, JSON.stringify([...favorites]));
}}

function saveDone() {{
  localStorage.setItem(DONE_KEY, JSON.stringify([...doneOffers]));
}}

function syncFavoriteButtons() {{
  rows.forEach(row => {{
    const key = row.dataset.favoriteKey;
    const button = row.querySelector('.favorite-btn');
    const active = favorites.has(key);
    if (button) {{
      button.classList.toggle('active', active);
      button.textContent = active ? '★' : '☆';
    }}

    const doneButton = row.querySelector('.done-btn');
    const done = doneOffers.has(key);
    if (doneButton) {{
      doneButton.classList.toggle('active', done);
      doneButton.title = done ? '取得済みを解除' : '取得済みにする';
    }}
    row.classList.toggle('done-row', done);
    row.dataset.done = done ? 'yes' : 'no';
  }});
}}

function updateFavoriteSummary() {{
  let plannedCount = 0;
  let plannedCost = 0;
  let plannedLsp = 0;
  let doneCount = 0;
  let doneLsp = 0;

  rows.forEach(row => {{
    const key = row.dataset.favoriteKey;
    const lsp = Number(row.dataset.favoriteLsp || 0);
    const cost = Number(row.dataset.favoriteCost || 0);
    const done = doneOffers.has(key);

    if (done) {{
      doneCount += 1;
      doneLsp += lsp;
    }}

    if (favorites.has(key) && !done) {{
      plannedCount += 1;
      plannedCost += cost;
      plannedLsp += lsp;
    }}
  }});

  favoriteSummary.textContent =
    `未取得お気に入り ${{plannedCount}}件・合計 ¥${{plannedCost.toLocaleString('ja-JP')}}・予定 ${{plannedLsp}}LSP`;
  doneSummary.textContent = `取得済み ${{doneCount}}件・${{doneLsp}}LSP`;

  const target = Number(targetLsp.value || 0);
  if (target > 0) {{
    const achieved = Math.min(target, doneLsp);
    const afterPlan = Math.max(0, target - doneLsp - plannedLsp);
    const progress = Math.min(100, (achieved / target) * 100);
    progressFill.style.width = `${{progress}}%`;
    progressLabel.textContent = `${{doneLsp}} / ${{target}} LSP`;
    targetResult.textContent = `→ この計画後 残り${{afterPlan}}LSP`;
  }} else {{
    progressFill.style.width = '0%';
    progressLabel.textContent = '目標未設定';
    targetResult.textContent = '';
  }}
}}

function refresh() {{
  const term = q.value.trim().toLowerCase();
  const mode = filter.value;
  rows.forEach(row => {{
    const textOK = !term || row.dataset.search.includes(term);
    const categoryOK = !activeCategory || row.dataset.category === activeCategory;
    const isFavorite = favorites.has(row.dataset.favoriteKey);
    const modeOK =
      mode === 'all' ||
      (mode === 'favorite' && isFavorite) ||
      (mode === 'favorite-undone' && isFavorite && row.dataset.done !== 'yes') ||
      (mode === 'done' && row.dataset.done === 'yes') ||
      (mode === 'top20' && Number(row.dataset.rank) <= 20) ||
      (mode === 'soon' && row.dataset.endingSoon === 'yes') ||
      (mode === 'new' && row.dataset.status === 'new') ||
      (mode === 'changed' && row.dataset.status === 'changed') ||
      (mode === 'priced' && Number(row.dataset.cost) < 999999999) ||
      (mode === 'warn' && row.dataset.warning === 'yes') ||
      (mode === 'detail' && row.dataset.detail === 'yes') ||
      (mode === 'linkless' && row.dataset.detail === 'no') ||
      (mode === 'first' && row.dataset.first === 'yes');
    row.hidden = !(textOK && categoryOK && modeOK);
  }});

  const priority = {{new:0, changed:1, same:2}};
  const sorted = [...rows].sort((a,b) => {{
    if (sort.value === 'name') return a.cells[1].innerText.localeCompare(b.cells[1].innerText, 'ja');
    if (sort.value === 'end') {{
      const av = a.cells[7].innerText.trim() || '9999-12-31';
      const bv = b.cells[7].innerText.trim() || '9999-12-31';
      return av.localeCompare(bv);
    }}
    if (sort.value === 'status') return priority[a.dataset.status] - priority[b.dataset.status];
    if (sort.value === 'favorite') {{
      const af = favorites.has(a.dataset.favoriteKey) ? 0 : 1;
      const bf = favorites.has(b.dataset.favoriteKey) ? 0 : 1;
      if (af !== bf) return af - bf;
    }}
    return Number(a.dataset.cost) - Number(b.dataset.cost);
  }});
  sorted.forEach(row => tbody.appendChild(row));
  const visibleCount = rows.filter(row => !row.hidden).length;
  resultCount.textContent = `${{visibleCount}}件を表示`;
  updateFavoriteSummary();
}}

function refreshPreservingScroll() {{
  const x = window.scrollX;
  const y = window.scrollY;
  refresh();
  requestAnimationFrame(() => window.scrollTo(x, y));
}}

tbody.addEventListener('click', event => {{
  const button = event.target.closest('.favorite-btn, .done-btn');
  if (!button) return;

  event.preventDefault();
  event.stopPropagation();

  const row = button.closest('tr');
  if (!row) return;
  const key = row.dataset.favoriteKey;

  if (button.classList.contains('favorite-btn')) {{
    if (favorites.has(key)) favorites.delete(key); else favorites.add(key);
    saveFavorites();
  }} else {{
    if (doneOffers.has(key)) doneOffers.delete(key); else doneOffers.add(key);
    saveDone();
  }}

  syncFavoriteButtons();
  refreshPreservingScroll();
}});

exportFavorites?.addEventListener('click', () => {{
  const payload = {{
    version: 2,
    exportedAt: new Date().toISOString(),
    favorites: [...favorites],
    doneOffers: [...doneOffers],
    targetLsp: targetLsp.value,
  }};
  const blob = new Blob([JSON.stringify(payload, null, 2)], {{type: 'application/json'}});
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = 'jal-lsp-favorites.json';
  anchor.click();
  URL.revokeObjectURL(url);
}});

exportFavoritesCsv?.addEventListener('click', () => {{
  const rowsForCsv = rows.filter(row => favorites.has(row.dataset.favoriteKey));
  const header = ['サービス','マイル条件','1LSP必要額','1000円でLSP','終了日','取得済み'];
  const body = rowsForCsv.map(row => [
    row.cells[1].innerText.replace(/\\s+/g, ' ').trim(),
    row.cells[2].innerText.trim(),
    row.cells[3].innerText.trim(),
    row.cells[4].innerText.trim(),
    row.cells[7].innerText.trim(),
    doneOffers.has(row.dataset.favoriteKey) ? '済' : '未',
  ]);
  const escapeCsv = value => `"${{String(value).replaceAll('"', '""')}}"`;
  const csv = '\ufeff' + [header, ...body].map(cols => cols.map(escapeCsv).join(',')).join('\\r\\n');
  const blob = new Blob([csv], {{type: 'text/csv;charset=utf-8'}});
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = 'jal-lsp-favorites.csv';
  anchor.click();
  URL.revokeObjectURL(url);
}});

importFavorites?.addEventListener('change', async event => {{
  const file = event.target.files?.[0];
  if (!file) return;
  try {{
    const payload = JSON.parse(await file.text());
    const imported = Array.isArray(payload) ? payload : payload.favorites;
    if (!Array.isArray(imported)) throw new Error('invalid');
    favorites = new Set(imported.map(String));
    doneOffers = new Set((payload.doneOffers || []).map(String));
    targetLsp.value = payload.targetLsp || '';
    saveFavorites();
    saveDone();
    localStorage.setItem(TARGET_KEY, targetLsp.value);
    syncFavoriteButtons();
    refresh();
  }} catch {{
    alert('お気に入りファイルを読み込めませんでした。');
  }} finally {{
    event.target.value = '';
  }}
}});

clearFavorites?.addEventListener('click', () => {{
  if (!confirm('お気に入りをすべて解除しますか？')) return;
  favorites.clear();
  saveFavorites();
  syncFavoriteButtons();
  refresh();
}});

targetLsp?.addEventListener('input', () => {{
  localStorage.setItem(TARGET_KEY, targetLsp.value);
  updateFavoriteSummary();
}});

quickButtons.forEach(button => {{
  button.addEventListener('click', () => {{
    const value = button.dataset.filter;
    filter.value = filter.value === value ? 'all' : value;
    quickButtons.forEach(b => b.classList.toggle('active', b === button && filter.value === value));
    refresh();
  }});
}});

filter.addEventListener('change', () => {{
  quickButtons.forEach(button => button.classList.toggle('active', button.dataset.filter === filter.value));
  refresh();
}});

categoryButtons.forEach(button => {{
  button.addEventListener('click', () => {{
    activeCategory = button.dataset.category;
    categoryButtons.forEach(b => b.classList.toggle('active', b === button));
    refresh();
  }});
}});
q.addEventListener('input', refresh);
sort.addEventListener('change', refresh);

themeToggle?.addEventListener('click', () => {{
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
}});

resetView?.addEventListener('click', () => {{
  q.value = '';
  filter.value = 'all';
  sort.value = 'cost';
  activeCategory = '';
  categoryButtons.forEach(button => button.classList.toggle('active', button.dataset.category === ''));
  quickButtons.forEach(button => button.classList.remove('active'));
  refresh();
}});

window.addEventListener('scroll', () => {{
  backToTop.classList.toggle('visible', window.scrollY > 600);
}}, {{passive:true}});

backToTop?.addEventListener('click', () => {{
  window.scrollTo({{top:0, behavior:'smooth'}});
}});

window.addEventListener('beforeinstallprompt', event => {{
  event.preventDefault();
  deferredInstallPrompt = event;
  installApp.hidden = false;
}});

installApp?.addEventListener('click', async () => {{
  if (!deferredInstallPrompt) return;
  deferredInstallPrompt.prompt();
  await deferredInstallPrompt.userChoice;
  deferredInstallPrompt = null;
  installApp.hidden = true;
}});

window.addEventListener('appinstalled', () => {{
  deferredInstallPrompt = null;
  installApp.hidden = true;
}});

if ('serviceWorker' in navigator) {{
  window.addEventListener('load', () => navigator.serviceWorker.register('./sw.js'));
}}

syncFavoriteButtons();
refresh();
</script>
</body>
</html>"""
    docs_dir = ROOT / "docs"
    docs_dir.mkdir(exist_ok=True)
    (docs_dir / "index.html").write_text(page, encoding="utf-8")

    manifest = {
        "name": "JAL LSP Checker",
        "short_name": "LSP Checker",
        "description": "JAL Mileage ParkのLSP対象案件を確認するダッシュボード",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#ffffff",
        "lang": "ja",
        "icons": [
            {
                "src": "icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable"
            }
        ]
    }
    (docs_dir / "manifest.webmanifest").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    icon_svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="104" fill="#c90023"/>
<path d="M118 104h82v210c0 61-36 98-98 98H72v-72h22c17 0 24-9 24-28V104zm126 0h82v236h114v72H244V104z" fill="white"/>
</svg>'''
    (docs_dir / "icon.svg").write_text(icon_svg, encoding="utf-8")

    service_worker = '''const CACHE = "jal-lsp-v2.0.3";
const CORE = ["./", "./index.html", "./manifest.webmanifest", "./icon.svg"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(CORE)));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  if (event.request.method !== "GET") return;

  const requestUrl = new URL(event.request.url);
  const isPage = event.request.mode === "navigate" || requestUrl.pathname.endsWith("/index.html");

  if (isPage) {
    event.respondWith(
      fetch(event.request, {cache: "no-store"})
        .then(response => {
          const copy = response.clone();
          caches.open(CACHE).then(cache => cache.put("./index.html", copy));
          return response;
        })
        .catch(() => caches.match("./index.html"))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then(hit =>
      hit || fetch(event.request).then(response => {
        const copy = response.clone();
        caches.open(CACHE).then(cache => cache.put(event.request, copy));
        return response;
      })
    )
  );
});'''
    (docs_dir / "sw.js").write_text(service_worker, encoding="utf-8")

def main() -> None:
    offers = scrape()
    if not offers:
        raise RuntimeError(
            "対象案件を1件も取得できませんでした。JAL側のHTML変更または一時的なアクセス制限が考えられます。"
        )
    write_csv(offers)
    write_history(offers)
    write_html(offers)
    print(f"wrote {len(offers)} offers")


if __name__ == "__main__":
    main()
