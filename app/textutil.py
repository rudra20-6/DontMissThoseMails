import re

from bs4 import BeautifulSoup

_WS = re.compile(r"[ \t ]+")
_NL = re.compile(r"\n{3,}")
_URL = re.compile(r"https?://[^\s<>\"')\]]+")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    # keep link targets, they often are the registration / submission links
    for a in soup.find_all("a"):
        href = a.get("href")
        if href and href.startswith("http") and href not in a.get_text():
            a.append(f" ({href})")
    return clean(soup.get_text("\n"))


def clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(_WS.sub(" ", line).strip() for line in text.split("\n"))
    return _NL.sub("\n\n", text).strip()


def extract_urls(text: str) -> list[str]:
    seen: list[str] = []
    for url in _URL.findall(text):
        url = url.rstrip(".,;")
        if url not in seen:
            seen.append(url)
    return seen


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"
