import re
from typing import List, AsyncIterable
from urllib.parse import urlparse, urljoin, quote

from bs4 import BeautifulSoup

from plugins.client import MangaClient, MangaCard, MangaChapter

class MangaKatanaClient(MangaClient):
    base_url = "https://mangakatana.com"
    pre_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
        'Referer': base_url
    }

    def __init__(self, *args, name="MangaKatana", **kwargs):
        super().__init__(*args, name=name, headers=self.pre_headers, **kwargs)

    def mangas_from_page(self, page: bytes):
        bs = BeautifulSoup(page, "html.parser")
        
        # Selector based on Kotlin: div#book_list > div.item
        items = bs.select("div#book_list > div.item")
        
        mangas = []
        for item in items:
            text_div = item.select_one("div.text")
            if not text_div: continue
            
            link = text_div.select_one("h3 > a")
            if not link: continue
            
            name = link.text.strip()
            url = link["href"]
            
            img_tag = item.select_one("img")
            picture_url = img_tag["src"] if img_tag else ""
            
            mangas.append(MangaCard(self, name, url, picture_url))
            
        return mangas

    def chapters_from_page(self, page: bytes, manga: MangaCard = None):
        bs = BeautifulSoup(page, "html.parser")
        
        # Selector based on Kotlin: tr:has(.chapter)
        # Actually standard HTML structure is usually a table or list
        rows = bs.select(".chapter")
        
        chapters = []
        for row in rows:
            link = row.select_one("a")
            if not link: continue
            
            url = link["href"]
            name = link.text.strip()
            
            chapters.append(MangaChapter(self, name, url, manga, []))
            
        return chapters

    async def pictures_from_chapters(self, content: bytes, response=None):
        # Extract images from script tag
        # Kotlin regex: data-src['"],\s*(\w+)  -> finds variable name
        # Then: var variable_name=[ ... ]
        
        html_str = content.decode("utf-8", errors="ignore")
        
        # 1. Find the variable name used for images
        # The script usually looks like: ... data-src', ytaw); ...
        # So we look for "data-src" followed by a comma and a variable name
        var_name_match = re.search(r"data-src['\"],\s*(\w+)", html_str)
        
        if not var_name_match:
            return []
            
        var_name = var_name_match.group(1)
        
        # 2. Find the array definition: var ytaw=['url1','url2',...]
        # Regex: var ytaw=\[([^\[]*)\]
        array_match = re.search(f"var {var_name}=\[([^\]]*)\]", html_str)
        
        if not array_match:
            return []
            
        # 3. Extract URLs from the array string
        # The array string is like: 'url1','url2','url3'
        raw_array = array_match.group(1)
        
        # Regex to capture content inside single quotes
        urls = re.findall(r"'([^']*)'", raw_array)
        
        return urls

    async def search(self, query: str = "", page: int = 1) -> List[MangaCard]:
        # Search URL: https://mangakatana.com/page/1?search=query&search_by=book_name
        query = quote(query)
        url = f"{self.base_url}/page/{page}?search={query}&search_by=book_name"
        
        content = await self.get_url(url)
        return self.mangas_from_page(content)

    async def get_chapters(self, manga_card: MangaCard, page: int = 1) -> List[MangaChapter]:
        content = await self.get_url(manga_card.url)
        return self.chapters_from_page(content, manga_card)[(page - 1) * 20 : page * 20]

    async def iter_chapters(self, manga_url: str, manga_name) -> AsyncIterable[MangaChapter]:
        manga_card = MangaCard(self, manga_name, manga_url, '')
        content = await self.get_url(manga_url)
        for chapter in self.chapters_from_page(content, manga_card):
            yield chapter

    async def contains_url(self, url: str):
        return url.startswith(self.base_url)

    async def check_updated_urls(self, last_chapters):
        # Use latest updates page: https://mangakatana.com/page/1
        content = await self.get_url(f"{self.base_url}/page/1")
        bs = BeautifulSoup(content, "html.parser")
        
        updated_urls = set()
        items = bs.select("div#book_list > div.item")
        for item in items:
            link = item.select_one("div.text > h3 > a")
            if link:
                updated_urls.add(link["href"])
        
        updated = []
        not_updated = []
        for lc in last_chapters:
            if lc.url in updated_urls:
                updated.append(lc.url)
            else:
                not_updated.append(lc.url)
                
        return updated, not_updated