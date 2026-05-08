import re
import requests
from bs4 import BeautifulSoup

# Max characters of page content to pass to Claude (keeps token cost reasonable)
MAX_CONTENT_LENGTH = 4000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

URL_PATTERN = re.compile(r'https?://[^\s]+')


def extract_urls(text: str) -> list[str]:
    """Find all URLs in a message."""
    return URL_PATTERN.findall(text)


def fetch_url(url: str, timeout: int = 8) -> tuple[bool, str]:
    """
    Fetch a URL and return its plain text content.
    Returns (success, content_or_error_message).
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return False, f"Unsupported content type: {content_type}"

        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove noise: scripts, styles, nav, footer, ads
        for tag in soup(["script", "style", "nav", "footer", "header",
                          "aside", "form", "noscript", "iframe"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)

        # Collapse excessive blank lines
        lines = [line for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)

        if not text:
            return False, "Page appears to be empty or JavaScript-rendered — try a different link."

        # Truncate to keep token cost reasonable
        if len(text) > MAX_CONTENT_LENGTH:
            text = text[:MAX_CONTENT_LENGTH] + "\n\n[Content truncated for length]"

        return True, text

    except requests.Timeout:
        return False, f"Request timed out fetching {url}"
    except requests.HTTPError as e:
        return False, f"HTTP {e.response.status_code} error fetching {url}"
    except Exception as e:
        return False, f"Could not fetch {url}: {str(e)}"


def build_url_context(urls: list[str]) -> str:
    """
    Fetch all URLs in a message and build a context block to prepend to the Claude prompt.
    Returns an empty string if nothing was fetched successfully.
    """
    if not urls:
        return ""

    sections = []
    for url in urls[:3]:  # Limit to 3 URLs per message
        success, content = fetch_url(url)
        if success:
            sections.append(f"--- Content from {url} ---\n{content}")
        else:
            sections.append(f"--- {url} ---\n[Could not load: {content}]")

    if not sections:
        return ""

    return "The user shared the following web content for reference:\n\n" + "\n\n".join(sections) + "\n\n"
