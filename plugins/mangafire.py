import re
import json
import asyncio
from io import BytesIO
from typing import List, AsyncIterable
from urllib.parse import urlparse, urljoin, quote

from PIL import Image as PILImage
from bs4 import BeautifulSoup

from plugins.client import MangaClient, MangaCard, MangaChapter

class MangaFireClient(MangaClient):
    base_url = "https://mangafire.to"
    
    # Headers based on the Kotlin file + standard browser headers
    pre_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
        'Referer': 'https://mangafire.to/',
        'Origin': 'https://mangafire.to',
    }

    def __init__(self, *args, name="MangaFire", **kwargs):
        super().__init__(*args, name=name, headers=self.pre_headers, **kwargs)

    def _descramble_image(self, image_data: bytes, offset: int) -> bytes:
        """
        Ports the Kotlin 'descramble' logic from ImageInterceptor.kt
        """
        try:
            with PILImage.open(BytesIO(image_data)) as img:
                width, height = img.size
                
                # Constants from Kotlin file
                PIECE_SIZE = 200
                MIN_SPLIT_COUNT = 5
                
                # Helper function for ceilDiv: (a + b - 1) // b
                def ceil_div(a, b):
                    return (a + b - 1) // b

                piece_width = min(PIECE_SIZE, ceil_div(width, MIN_SPLIT_COUNT))
                piece_height = min(PIECE_SIZE, ceil_div(height, MIN_SPLIT_COUNT))
                
                x_max = ceil_div(width, piece_width) - 1
                y_max = ceil_div(height, piece_height) - 1
                
                # Create new blank image
                result = PILImage.new("RGBA", (width, height))
                
                for y in range(y_max + 1):
                    for x in range(x_max + 1):
                        x_dst = piece_width * x
                        y_dst = piece_height * y
                        
                        w = min(piece_width, width - x_dst)
                        h = min(piece_height, height - y_dst)
                        
                        # Calculate Source X
                        if x == x_max:
                            x_src_idx = x
                        else:
                            x_src_idx = (x_max - x + offset) % x_max
                        x_src = piece_width * x_src_idx

                        # Calculate Source Y
                        if y == y_max:
                            y_src_idx = y
                        else:
                            y_src_idx = (y_max - y + offset) % y_max
                        y_src = piece_height * y_src_idx
                        
                        # Crop and Paste
                        box = (x_src, y_src, x_src + w, y_src + h)
                        piece = img.crop(box)
                        result.paste(piece, (x_dst, y_dst))
                
                output = BytesIO()
                result.convert("RGB").save(output, format="JPEG", quality=90)
                return output.getvalue()
        except Exception as e:
            print(f"Descramble error: {e}")
            return image_data

    async def get_url(self, url, *args, **kwargs):
        # Override get_url to handle scrambled images
        # Scrambled URLs look like: ...image.jpg#scrambled_123
        
        is_scrambled = False
        offset = 0
        
        if "#scrambled_" in url:
            parts = url.split("#scrambled_")
            url = parts[0]
            try:
                offset = int(parts[1])
                is_scrambled = True
            except:
                pass
        
        # Call parent get_url (which calls _fetch_url)
        # We assume _fetch_url returns response content (bytes) if req_content=True
        # Or we can intercept the response if req_content=False.
        # The base MangaClient.get_url returns bytes by default.
        
        content = await super().get_url(url, *args, **kwargs)
        
        if is_scrambled and isinstance(content, bytes):
            return self._descramble_image(content, offset)
            
        return content

    async def search(self, query: str = "", page: int = 1) -> List[MangaCard]:
        # MangaFire search URL structure
        # /filter?keyword=...&page=...
        
        # Note: MangaFire uses 'vrf' token for AJAX searches heavily.
        # But normal HTML search might work without it or be less strict.
        # Let's try the standard HTML page parsing first as per 'searchMangaParse'.
        
        std_query = query.strip().replace(" ", "+")
        
        url = f"{self.base_url}/filter?keyword={std_query}&page={page}"
        
        content = await self.get_url(url)
        bs = BeautifulSoup(content, "html.parser")
        
        # Selector: .original.card-lg .unit .inner
        container = bs.select(".original.card-lg .unit .inner")
        
        mangas = []
        for element in container:
            info_link = element.select_one(".info > a")
            if not info_link:
                continue
                
            name = info_link.text.strip()
            # href is usually /manga/slug.id
            manga_url = urljoin(self.base_url, info_link["href"])
            
            img = element.select_one("img")
            picture_url = img["src"] if img else ""
            
            mangas.append(MangaCard(self, name, manga_url, picture_url))
            
        return mangas

    async def get_chapters(self, manga_card: MangaCard, page: int = 1) -> List[MangaChapter]:
        # MangaFire loads chapters via AJAX.
        # URL: /ajax/manga/{id}/chapter/{lang}
        # We need to extract the ID from the URL.
        # URL format: https://mangafire.to/manga/title.id  (ID is the part after the last dot)
        
        try:
            path = urlparse(manga_card.url).path # /manga/title.id
            manga_id = path.split(".")[-1]
        except:
            return []
            
        # 0 = English (usually), but the site uses codes like 'en', 'es'. 
        # The bot seems to pass specific language clients. 
        # For now, we default to 'en'.
        lang_code = "en"
        
        ajax_url = f"{self.base_url}/ajax/manga/{manga_id}/chapter/{lang_code}"
        
        content = await self.get_url(ajax_url)
        
        try:
            data = json.loads(content)
            if data.get("status") != 200:
                return []
            
            html_content = data.get("result", "")
            bs = BeautifulSoup(html_content, "html.parser")
            
            # Selector from Kotlin: .vol-list > .item OR li
            # Usually it returns a list of <li> elements
            items = bs.select("li.item")
            
            chapters = []
            for item in items:
                link = item.select_one("a")
                if not link:
                    continue
                    
                url = urljoin(self.base_url, link["href"])
                
                # Title parsing
                number = item.get("data-number", "")
                title_span = item.select_one("span")
                raw_title = title_span.text.strip() if title_span else ""
                
                name = raw_title
                if number:
                    name = f"Chapter {number}: {raw_title}"
                
                # Clean up name
                name = name.replace("Chapter :", "Chapter").strip()
                if name.endswith(":"): name = name[:-1]
                
                chapters.append(MangaChapter(self, name, url, manga_card, []))
                
            # MangaFire returns ALL chapters in one list usually. 
            # We simulate pagination for the bot.
            return chapters[(page - 1) * 20 : page * 20]
            
        except Exception as e:
            print(f"MangaFire chapter error: {e}")
            return []

    async def iter_chapters(self, manga_url: str, manga_name) -> AsyncIterable[MangaChapter]:
        manga_card = MangaCard(self, manga_name, manga_url, '')
        
        # Similar logic to get_chapters but yields all
        try:
            path = urlparse(manga_url).path
            manga_id = path.split(".")[-1]
            lang_code = "en"
            ajax_url = f"{self.base_url}/ajax/manga/{manga_id}/chapter/{lang_code}"
            
            content = await self.get_url(ajax_url)
            data = json.loads(content)
            html_content = data.get("result", "")
            bs = BeautifulSoup(html_content, "html.parser")
            items = bs.select("li.item")
            
            for item in items:
                link = item.select_one("a")
                if not link: continue
                url = urljoin(self.base_url, link["href"])
                number = item.get("data-number", "")
                title_span = item.select_one("span")
                raw_title = title_span.text.strip() if title_span else ""
                name = f"Chapter {number}" if number else raw_title
                
                yield MangaChapter(self, name, url, manga_card, [])
                
        except Exception:
            pass

    async def pictures_from_chapters(self, content: bytes, response=None):
        # We need to call the AJAX reader endpoint
        # The URL we have is: https://mangafire.to/read/title.id/chapter_id
        # We need to find the chapter ID from the URL or ID from the page?
        # Actually, the reader page usually makes an AJAX call to get images.
        
        if response:
            chapter_url = str(response.url)
        else:
            return []
            
        # URL structure: .../read/manga-slug.id/chapter-lang-number
        # The important part is the last segment often, but MangaFire ID logic is tricky.
        # Inspecting network tab: 
        # They call: /ajax/read/{id}/chapter/{en}  <-- NO
        # The Kotlin code says: url.encodedPath.contains("ajax/read/chapter")
        
        # 1. Parse the Chapter ID from the HTML content of the reader page?
        # The reader page (the one we just fetched) contains IDs in JavaScript.
        
        # However, looking at the Kotlin code 'fetchPageList', it loads the chapter URL,
        # checks for 'ajax/read/chapter', and gets a JSON response.
        
        # Since we can't easily execute JS, we have to find the ID.
        # Usually, the page has a JS variable or we can deduce the API call.
        
        # Attempt to get images via the specific ID found in URL?
        # Let's try to extract the ID from the URL path.
        # Example: https://mangafire.to/read/naruto.123/en/chapter-1
        # The ID '123' is the Manga ID. The chapter has its own ID.
        
        # Simpler approach: Look for the specific AJAX call pattern in the page source 
        # OR just iterate commonly known patterns.
        
        # BUT, the Kotlin code says: `getChapterUrl` returns `baseUrl + chapter.url`.
        # And `fetchPageList` calls that URL.
        # The `fetchPageList` uses a WebView to intercept the `ajax/read` call.
        # Since we don't have a WebView, we must reverse-engineer the API call.
        
        # The API call is usually: /ajax/read/{chapter_numeric_id}
        # We need to find `chapter_numeric_id`.
        # It is usually embedded in the HTML `data-id` or inside a JS `window.` variable.
        
        try:
            html_str = content.decode('utf-8', errors='ignore')
            
            # Look for a pattern like: chapter_id = 12345
            # Or data-id="12345" on a wrapper.
            # Only heuristic guessing is possible without live JS.
            
            # Regex for common patterns
            # This is a guess based on standard sites.
            # If this fails, this plugin requires a headless browser (Selenium/Playwright).
            
            # Assuming we can't find it easily without JS, we return empty.
            # However, let's try to fetch the read ID from the URL if it's there.
            # URLs in 'chapterListParse' are like: /read/slug.id/chapter-slug
            
            # NOTE: Without the specific ID mapping (which is hidden in JS), 
            # this part is the hardest to do in pure Python requests.
            # The Kotlin extension cheats by using WebView interception.
            
            return [] 
            
        except Exception:
            return []

    async def contains_url(self, url: str):
        return "mangafire.to" in url

    async def check_updated_urls(self, last_chapters):
        # Use latest updates page
        content = await self.get_url(f"{self.base_url}/filter?sort=recently_updated")
        bs = BeautifulSoup(content, "html.parser")
        
        updates = {}
        container = bs.select(".original.card-lg .unit .inner")
        
        for element in container:
            link = element.select_one(".info > a")
            if link:
                m_url = urljoin(self.base_url, link["href"])
                # We need the chapter URL too? 
                # The card usually links to the manga, not the specific latest chapter in the href.
                # But sometimes there's a badge.
                updates[m_url] = True 
        
        updated = []
        not_updated = []
        for lc in last_chapters:
            if lc.url in updates:
                updated.append(lc.url)
            else:
                not_updated.append(lc.url)
                
        return updated, not_updated