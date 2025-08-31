import requests
import os
import json
from urllib.parse import urljoin, urlparse
import re
from html import unescape
from datetime import datetime
from gemini import ask_gemini
def main():
    # Minimal .env loader for DISCORD_WEBHOOK if not already in env
    def _load_dotenv_minimal(path: str = ".env") -> None:
        if "DISCORD_WEBHOOK" in os.environ:
            return
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'").strip('"')
                    if k == "DISCORD_WEBHOOK" and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass

    _load_dotenv_minimal()
    # Persistence file for last notified chase
    LAST_CHASE_FILE = "last_chase.json"
    # This is the news urls

    news="""https://www.nbclosangeles.com/
    https://www.presstelegram.com
    https://www.pasadenastarnews.com
    https://smdp.com
    https://glendalenewspress.com
    https://burbankleader.com
    https://signalscv.com
    https://www.dailybreeze.com
    https://inglewoodtoday.com
    https://wehoonline.com
    https://www.culvercityobserver.com
    https://beverlyhillscourier.com
    https://www.ocregister.com
    https://www.pressenterprise.com
    https://www.sbsun.com
    https://www.dailybulletin.com
    https://www.avpress.com
    https://www.sgvtribune.com"""

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # (name, regex pattern, weight)
    PATTERNS = [
        ("pursuit", re.compile(r"\bpursuit(s)?\b", re.I), 6),
        ("police chase", re.compile(r"\bpolice\s+chase(s)?\b", re.I), 7),
        ("car chase", re.compile(r"\bcar\s+chase(s)?\b", re.I), 6),
        ("vehicle chase", re.compile(r"\bvehicle\s+chase(s)?\b", re.I), 5),
        ("high-speed chase", re.compile(r"\bhigh[-\s]?speed\s+chase\b", re.I), 8),
        ("PIT maneuver", re.compile(r"\b(pit|p\.i\.t\.)\s+maneuver\b", re.I), 8),
        ("spike strip", re.compile(r"\bspike\s+strip(s)?\b", re.I), 5),
        ("pursuit ends", re.compile(r"\bpursuit\s+end(s|ed)?\b", re.I), 5),
        ("termination of pursuit", re.compile(r"\btermination\s+of\s+pursuit\b", re.I), 6),
        ("suspect + pursuit", re.compile(r"\bsuspect\b.*?\bpursuit\b|\bpursuit\b.*?\bsuspect\b", re.I | re.S), 5),
        ("evading", re.compile(r"\b(fleeing|evading|eluding)\b", re.I), 3),
        ("wrong-way driver", re.compile(r"\bwrong[-\s]?way\s+driver\b", re.I), 4),
        (
            "freeway/roads",
            re.compile(
                r"\b(?:I[-\s]?(?:5|10|60|91|101|105|110|210|405))\b|"
                r"\b(?:SR|US|CA)[-\s]?(?:5|10|60|91|101|105|110|210|405)\b|"
                r"\b(?:Freeway|Highway|Hwy|\d{2,3}\s*Freeway)\b",
                re.I,
            ),
            2,
        ),
        ("news chopper", re.compile(r"\bnews\s?chopper\b|\bsky\s?fox\b|\bsky\s?5\b", re.I), 3),
        ("live pursuit", re.compile(r"\blive\s+pursuit\b", re.I), 7),
        ("LAPD/CHP pursuit", re.compile(r"\b(LAPD|CHP)\b.*\bpursuit\b", re.I), 6),
    ]

    MIN_SCORE_TO_LOG = 6  # conservative threshold to avoid noise

    # Core signals that must appear somewhere on the page to avoid freeway/weather false positives
    CORE_NAMES = {
        "pursuit",
        "police chase",
        "car chase",
        "vehicle chase",
        "high-speed chase",
        "PIT maneuver",
        "spike strip",
        "pursuit ends",
        "termination of pursuit",
        "live pursuit",
        "LAPD/CHP pursuit",
    }
    CORE_PATTERNS = [pat for (name, pat, _w) in PATTERNS if name in CORE_NAMES]

    def _strip_scripts_and_styles(html: str) -> str:
        if not html:
            return ""
        cleaned = re.sub(r"(?is)<(script|style|noscript|iframe|svg|meta|link)[^>]*>.*?</\1>", " ", html)
        cleaned = re.sub(r"(?is)<br\s*/?>", "\n", cleaned)
        return cleaned

    def extract_text_blocks(html: str):
        cleaned = _strip_scripts_and_styles(html)
        blocks = []
        # Gather headings and paragraphs first
        for tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li"):
            for m in re.findall(rf"<\s*{tag}[^>]*>(.*?)</\s*{tag}\s*>", cleaned, flags=re.I | re.S):
                text = re.sub(r"(?is)<[^>]+>", " ", m)
                text = unescape(text)
                text = re.sub(r"\s+", " ", text).strip()
                if not text:
                    continue
                # Keep headings even if short; other tags need some length or keyword presence
                if tag not in ("h1", "h2", "h3") and len(text) < 30:
                    if not any(pat.search(text) for _, pat, _ in PATTERNS):
                        continue
                blocks.append(text)
        # Include anchor texts that look like headlines
        for m in re.findall(r"<\s*a[^>]*>(.*?)</\s*a\s*>", cleaned, flags=re.I | re.S):
            text = re.sub(r"(?is)<[^>]+>", " ", m)
            text = unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                continue
            if len(text) >= 25 or any(pat.search(text) for _, pat, _ in PATTERNS):
                blocks.append(text)
        # Deduplicate while preserving order
        seen = set()
        unique_blocks = []
        for b in blocks:
            if b in seen:
                continue
            seen.add(b)
            unique_blocks.append(b)
        return unique_blocks[:600]

    def _extract_anchors(html: str):
        cleaned = re.sub(r"(?is)<(script|style|noscript|iframe|svg|meta|link)[^>]*>.*?</\1>", " ", html)
        anchors = []
        for m in re.findall(r"<\s*a[^>]*href=\s*['\"]([^'\"]+)['\"][^>]*>(.*?)</\s*a\s*>", cleaned, flags=re.I | re.S):
            href, inner = m
            text = re.sub(r"(?is)<[^>]+>", " ", inner)
            text = unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                continue
            anchors.append((text, href))
        return anchors

    def _extract_headings(html: str):
        cleaned = _strip_scripts_and_styles(html)
        heads = []
        for tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            for m in re.findall(rf"<\s*{tag}[^>]*>(.*?)</\s*{tag}\s*>", cleaned, flags=re.I | re.S):
                text = re.sub(r"(?is)<[^>]+>", " ", m)
                text = unescape(text)
                text = re.sub(r"\s+", " ", text).strip()
                if not text:
                    continue
                heads.append(text)
        return heads

    def _choose_best_title_and_link(html: str, base_url: str):
        anchors = _extract_anchors(html)
        headings = _extract_headings(html)

        best_title = None
        best_href = None
        best_score = 0

        # Score anchors first; prefer those with LIVE in text
        for text, href in anchors:
            s, _ = score_text_block(text)
            if re.search(r"\blive\b", text, flags=re.I):
                s += 6
            if s > best_score:
                best_score = s
                best_title = text
                best_href = urljoin(base_url, href)

        # Consider headings if stronger
        for text in headings:
            s, _ = score_text_block(text)
            if re.search(r"\blive\b", text, flags=re.I):
                s += 6
            if s > best_score:
                best_score = s
                best_title = text
                best_href = None

        return best_title, best_href

    def _find_any_live_link(html: str, base_url: str):
        for text, href in _extract_anchors(html):
            if not href:
                continue
            if re.search(r"\blive|watch\s+live|live\s+stream|live\s+coverage\b", text, flags=re.I) or re.search(r"/live|live-", href, flags=re.I):
                return urljoin(base_url, href)
        return None

    def _ask_is_live_ongoing(title: str) -> bool:
        if not title:
            return False
        prompt = (
            "You are a strict classifier. Given the news headline, decide if it indicates a LIVE ongoing police pursuit/chase that is currently happening and being broadcast live. "
            "Respond with a single token: YES or NO. If unsure, respond NO.\n"
            f"Headline: \"{title}\"\nAnswer:"
        )
        try:
            resp = ask_gemini(prompt)
        except Exception:
            return False
        ans = (resp or "").strip().split()[0].upper() if resp else ""
        return ans == "YES"

    def _send_discord_alert(message: str):
        webhook = os.getenv("DISCORD_WEBHOOK")
        if not webhook:
            return
        try:
            requests.post(webhook, json={"content": message}, timeout=10)
        except Exception:
            pass

    def _has_hours_ago(text: str) -> bool:
        if not text:
            return False
        return re.search(r"\b\d+\s*(?:hour|hours)\s+ago\b", text, flags=re.I) is not None

    def _domain_of(url: str) -> str:
        try:
            return urlparse(url).netloc.lower()
        except Exception:
            return ""

    def _utcnow_iso() -> str:
        return datetime.utcnow().isoformat(timespec="seconds") + "Z"

    def _load_last_chase(path: str = LAST_CHASE_FILE):
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _save_last_chase(info: dict, path: str = LAST_CHASE_FILE) -> None:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _ask_is_same_chase(previous: dict, current: dict) -> bool:
        """Use Gemini to decide if current refers to the same ongoing chase as previous. YES/NO only."""
        if not previous:
            return False
        prev_summary = (
            f"prev_title: {previous.get('title') or ''}\n"
            f"prev_text: {previous.get('text') or ''}\n"
            f"prev_site: {previous.get('source_site') or ''}\n"
            f"prev_url: {previous.get('page_url') or ''}\n"
            f"prev_live: {previous.get('live_link') or ''}\n"
            f"prev_time_utc: {previous.get('alerted_at') or ''}\n"
        )
        curr_summary = (
            f"curr_title: {current.get('title') or ''}\n"
            f"curr_text: {current.get('text') or ''}\n"
            f"curr_site: {current.get('source_site') or ''}\n"
            f"curr_url: {current.get('page_url') or ''}\n"
            f"curr_live: {current.get('live_link') or ''}\n"
            f"curr_time_utc: {current.get('evaluated_at') or ''}\n"
        )
        prompt = (
            "You are a strict deduplication classifier for police pursuits. There is only one chase at a time.\n"
            "Determine if the CURRENT chase is the same ongoing event as the PREVIOUS chase (possibly covered by a different outlet or link).\n"
            "Use location cues, suspect/vehicle details, and close time proximity. If it likely refers to the same ongoing event or a continuation/update, answer YES. Otherwise NO.\n"
            "Output only YES or NO.\n\n"
            "PREVIOUS:\n" + prev_summary + "\nCURRENT:\n" + curr_summary + "\nAnswer:"
        )
        try:
            resp = ask_gemini(prompt)
        except Exception:
            return False
        ans = (resp or "").strip().split()[0].upper() if resp else ""
        return ans == "YES"

    def score_text_block(text: str):
        score = 0
        hits = {}
        for name, pat, weight in PATTERNS:
            found = pat.findall(text)
            count = len(found)
            if count:
                hits[name] = hits.get(name, 0) + count
                score += count * weight
        return score, hits

    def split_into_sentences(text: str):
        text = text.replace("\n", " ").strip()
        if not text:
            return []
        parts = re.split(r"(?<=[\.!?…])\s+", text)
        return [p.strip() for p in parts if p.strip()]

    def best_sentence_snippet(block_text: str):
        sentences = split_into_sentences(block_text)
        if not sentences:
            return None, 0, {}
        best_idx = 0
        best_score = 0
        best_hits = {}
        for i, sent in enumerate(sentences):
            score, hits = score_text_block(sent)
            if score > best_score:
                best_idx = i
                best_score = score
                best_hits = hits
        # Expand snippet with neighbors if helpful and within length
        start = max(0, best_idx - 1)
        end = min(len(sentences), best_idx + 2)
        snippet = " ".join(sentences[start:end]).strip()
        if len(snippet) > 700:
            snippet = snippet[:700].rsplit(" ", 1)[0] + " …"
        return snippet, best_score, best_hits

    def scan_page_for_chase(url: str):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
        except requests.RequestException:
            return None
        if resp.status_code >= 400:
            return None
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" not in content_type:
            return None
        html = resp.text or ""
        if "Access to this site has been denied" in html:
            return None
        blocks = extract_text_blocks(html)
        # Gate by core signals somewhere on the page
        page_has_core = any(pat.search(html) for pat in CORE_PATTERNS)
        best_snippet = None
        best_score = 0
        best_hits = {}
        for block in blocks:
            snippet, s, h = best_sentence_snippet(block)
            if s > best_score and snippet:
                best_score = s
                best_snippet = snippet
                best_hits = h
        if not page_has_core or best_score < MIN_SCORE_TO_LOG or not best_snippet:
            return None
        confidence = min(1.0, best_score / 10.0)
        return {
            "url": url,
            "score": best_score,
            "confidence": round(confidence, 2),
            "text": best_snippet,
            "hits": best_hits,
            "html": html,
        }

    def log_pursuit(findings: dict):
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        line = (
            f"[{ts}] URL: {findings['url']} | score={findings['score']} | "
            f"confidence={findings['confidence']} | hits={findings['hits']}\n"
            f"TEXT: {findings['text']}\n---\n"
        )
        try:
            with open("log.txt", "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass
        print(line, end="")

    news=news.split("\n")

    for news_url in news:
        result = scan_page_for_chase(news_url)
        if not result:
            continue
        log_pursuit(result)

        # Extract a plausible title and candidate href from the page
        title, candidate_href = _choose_best_title_and_link(result.get("html", ""), result["url"])
        if not title:
            title = result.get("text") or ""
        print(title,candidate_href)
        # Skip clearly stale headlines like "17 hours ago"
        # if _has_hours_ago(title) or _has_hours_ago(result.get("text") or ""):
            # continue
        # Ask Gemini to confirm if this is a LIVE ongoing chase
        if _ask_is_live_ongoing(title):
            print("is live ongoing")
            live_link = _find_any_live_link(result.get("html", ""), result["url"]) or candidate_href or result["url"]

            # Duplicate suppression: compare with last saved chase
            last = _load_last_chase()
            current_obj = {
                "title": title,
                "text": result.get("text") or "",
                "page_url": result["url"],
                "source_site": _domain_of(result["url"]),
                "live_link": live_link,
                "evaluated_at": _utcnow_iso(),
            }
            if last and _ask_is_same_chase(last, current_obj):
                print("duplicate of last ongoing chase; skipping alert")
                continue

            alert_msg = f"@everyone LIVE ONGOING CHASE: {title}\n{live_link}"
            _send_discord_alert(alert_msg)

            # Persist this alert as the last chase
            _save_last_chase({
                "title": title,
                "text": result.get("text") or "",
                "page_url": result["url"],
                "source_site": _domain_of(result["url"]),
                "live_link": live_link,
                "alerted_at": _utcnow_iso(),
            })
        else:
            print("is not live ongoing")