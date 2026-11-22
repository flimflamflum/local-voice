"""Web search the model can call as a tool (the ddgs package: DuckDuckGo and friends, no API key).

The model gets compact text: a handful of result snippets plus the most relevant lines
from the top couple of pages, since snippets alone often miss the actual answer (scores,
temperatures, prices). Everything is trimmed hard to fit a small context window.
"""
import concurrent.futures
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import requests

MAX_RESULTS = 5
BACKENDS = ("auto", "bing", "brave")  # ddgs search engines, tried in turn until one answers
SNIPPET_CHARS = 280
PAGES_TO_READ = 2         # top results whose page text we pull in
PAGE_CHARS = 1200         # per page, after keeping only the most relevant lines
PAGE_TIMEOUT = 4          # seconds; a slow site shouldn't hold up the answer
SKIP_PAGES = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "facebook.com", "x.com", "twitter.com")
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

# Ollama / OpenAI-style tool definition. The description is what tells the model when to use it.
TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the internet. Use it for anything current or recent (news, weather, sports results, "
            "prices, releases, who currently holds a job or title), anything that may have changed after your "
            "training, when the user asks you to look something up or check something, and for facts you "
            "aren't sure of. Don't use it for small talk, opinions, maths, or general knowledge you're sure of."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A short web search query, like you'd type into a search engine"},
            },
            "required": ["query"],
        },
    },
}

_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = {"the", "and", "for", "what", "who", "how", "when", "where", "which", "with", "today", "latest", "now", "news"}


def search(query: str) -> str:
    """Run a search and return text for the model (an error message if it failed)."""
    query = (query or "").strip()
    if not query:
        return "No search query was given."
    results, error = [], None
    for backend in BACKENDS:  # one engine rate-limiting or coming back empty is common; try the next
        try:
            from ddgs import DDGS

            results = DDGS(timeout=6).text(query, max_results=MAX_RESULTS, backend=backend) or []
        except Exception as e:  # noqa: BLE001 - rate limits, no network, no results
            error = e
            continue
        if results:
            break
    # Weather sites draw their numbers with JavaScript, so scraping them gets nothing; ask a
    # weather service directly instead.
    weather = _weather(query) if _WEATHER.search(query) else ""
    if not results:
        if weather:
            return weather
        if error is not None and "no results" not in str(error).lower():
            return f"The web search failed ({error}). Tell the user you couldn't search right now."
        return f'No web results for "{query}".'

    today = datetime.now().strftime("%A, %B %d, %Y").replace(" 0", " ")
    lines = [weather, ""] if weather else []
    lines.append(f'Web results for "{query}" (searched {today}):')
    for i, r in enumerate(results, 1):
        body = _squash(r.get("body", ""))[:SNIPPET_CHARS]
        lines.append(f"{i}. {_squash(r.get('title', ''))[:100]} ({_site(r.get('href', ''))}): {body}")

    urls = [r["href"] for r in results if r.get("href") and not _site(r["href"]).endswith(SKIP_PAGES)]
    pages = [] if weather else _read_pages(urls[:PAGES_TO_READ], query)
    for url, text in pages:
        lines.append(f"\nFrom {_site(url)}:\n{text}")
    return "\n".join(lines)


def _read_pages(urls, query):
    if not urls:
        return []
    with concurrent.futures.ThreadPoolExecutor(len(urls)) as pool:
        texts = list(pool.map(lambda u: _page_text(u, query), urls))
    return [(u, t) for u, t in zip(urls, texts) if t]


def _page_text(url, query):
    """The page's lines that share the most words with the query, in page order."""
    try:
        r = requests.get(url, timeout=PAGE_TIMEOUT, headers={"User-Agent": _UA})
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return ""
        from lxml import html as lxml_html

        doc = lxml_html.fromstring(r.content)
        for bad in doc.xpath("//script|//style|//noscript|//nav|//header|//footer|//aside|//form|//svg"):
            bad.drop_tree()
        text = doc.text_content()
    except Exception:  # noqa: BLE001 - timeouts, bad HTML, blocked
        return ""
    lines = [_squash(line) for line in text.splitlines()]
    lines = list(dict.fromkeys(line for line in lines if len(line) >= 30))  # drop repeats, keep order
    if not lines:
        return ""
    words = {w for w in _WORD.findall(query.lower()) if w not in _STOP}
    scored = sorted(
        range(len(lines)),
        key=lambda i: (-len(words & set(_WORD.findall(lines[i].lower()))), i),
    )
    keep, total = [], 0
    for i in scored:
        if total >= PAGE_CHARS:
            break
        keep.append(i)
        total += len(lines[i]) + 1
    return "\n".join(lines[i] for i in sorted(keep))[:PAGE_CHARS]


_WEATHER = re.compile(r"\b(weather|forecast|temperature|how (hot|cold|warm)|raining|rain|snow(ing)?|sunny|humid(ity)?)\b", re.I)
_NOT_PLACE = re.compile(
    r"\b(what'?s|what is|is it|will it|the|weather|forecast|temperature|current(ly)?|right|now|today|tonight|"
    r"tomorrow|this|week(end)?|morning|afternoon|evening|like|going|to|be|in|for|at|of|and|how|hot|cold|warm|"
    r"raining|rain|snow(ing)?|sunny|humid(ity)?|outside|there|here|me|my|area|local|near|degrees|celsius|"
    r"fahrenheit|c|f|please|check|look|up|°)\b",
    re.I,
)


def _weather(query):
    """Current conditions + today/tomorrow from wttr.in (free, no key), or "" if it fails."""
    place = re.sub(r"\s+", " ", _NOT_PLACE.sub(" ", re.sub(r"[?!.,]", " ", query))).strip()
    try:
        r = requests.get(f"https://wttr.in/{requests.utils.quote(place)}", params={"format": "j1"},
                         timeout=PAGE_TIMEOUT, headers={"User-Agent": "curl/8"})
        j = r.json()
        c = j["current_condition"][0]
        area = j.get("nearest_area", [{}])[0]
        name = ", ".join(x[0]["value"] for x in (area.get("areaName"), area.get("country")) if x)
    except Exception:  # noqa: BLE001
        return ""
    where = f"{place} ({name})" if place else f"the user's area, guessed from their internet connection ({name})"
    lines = [
        f"Live weather for {where} from wttr.in:",
        f"Now: {c['weatherDesc'][0]['value'].strip()}, {c['temp_C']}°C ({c['temp_F']}°F), feels like "
        f"{c['FeelsLikeC']}°C ({c['FeelsLikeF']}°F), humidity {c['humidity']}%, wind {c['windspeedKmph']} km/h "
        f"({c['windspeedMiles']} mph) from the {c['winddir16Point']}.",
    ]
    today = date.today()
    labels = {today.isoformat(): "Today", (today + timedelta(days=1)).isoformat(): "Tomorrow"}
    # wttr.in sometimes serves a cached forecast that starts yesterday: go by the dates, not the order.
    for day in j.get("weather", []):
        label = labels.get(day.get("date"))
        if not label:
            continue
        rain = max(int(h.get("chanceofrain", 0)) for h in day["hourly"])
        desc = day["hourly"][len(day["hourly"]) // 2]["weatherDesc"][0]["value"].strip()
        lines.append(f"{label} ({day['date']}): {desc}, high {day['maxtempC']}°C ({day['maxtempF']}°F), "
                     f"low {day['mintempC']}°C ({day['mintempF']}°F), up to {rain}% chance of rain.")
    return "\n".join(lines)


def _site(url):
    host = urlparse(url).netloc.lower()
    return host.removeprefix("www.") or url


def _squash(s):
    return re.sub(r"\s+", " ", s or "").strip()
