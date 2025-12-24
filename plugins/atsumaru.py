import json
import urllib.parse
from typing import List, AsyncIterable
from urllib.parse import urlparse, urljoin, quote

from plugins.client import MangaClient, MangaCard, MangaChapter

class AtsumaruClient(MangaClient):
    base_url = "https://atsu.moe"
    api_headers = {
        'Accept': '*/*',
        'Host': 'atsu.moe',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36'
    }

    def __init__(self, *args, name="Atsumaru", **kwargs):
        super().__init__(*args, name=name, headers=self.api_headers, **kwargs)

    async def search(self, query: str = "", page: int = 1) -> List[MangaCard]:
        # Handle "Latest Updates" or "Popular" if query is empty
        if not query:
            # Using trending endpoint for popular/empty search
            # Page in API starts at 0, bot starts at 1
            api_page = page - 1
            url = f"{self.base_url}/api/infinite/trending?page={api_page}&types=Manga,Manwha,Manhua,OEL"
            content = await self.get_url(url)
            try:
                data = json.loads(content)
                # The Kotlin DTO says 'items' contains the list
                items = data.get("items", [])
            except json.JSONDecodeError:
                return []
        else:
            # Using search endpoint
            params = {
                "q": query,
                "query_by": "title,englishTitle,otherNames",
                "limit": "24",
                "page": str(page),
                "query_by_weights": "3,2,1",
                "include_fields": "id,title,englishTitle,poster",
                "num_typos": "4,3,2"
            }
            query_string = urllib.parse.urlencode(params)
            url = f"{self.base_url}/collections/manga/documents/search?{query_string}"
            
            content = await self.get_url(url)
            try:
                data = json.loads(content)
                # Search returns 'hits', each hit has a 'document'
                items = [hit['document'] for hit in data.get("hits", [])]
            except json.JSONDecodeError:
                return []

        mangas = []
        for item in items:
            manga_id = item.get("id")
            title = item.get("title")
            
            # Image handling based on DTO logic
            image_path = item.get("poster") or item.get("image")
            if isinstance(image_path, dict):
                 image_path = image_path.get("image")
            
            if image_path:
                # Remove prefixes if present (Kotlin logic)
                image_path = str(image_path).removeprefix("/").removeprefix("static/")
                thumbnail_url = f"{self.base_url}/static/{image_path}"
            else:
                thumbnail_url = ""

            # Store full URL for convenience
            manga_url = f"{self.base_url}/manga/{manga_id}"
            
            mangas.append(MangaCard(self, title, manga_url, thumbnail_url))

        return mangas

    async def get_chapters(self, manga_card: MangaCard, page: int = 1) -> List[MangaChapter]:
        # Extract ID (slug) from the URL
        # URL format: https://atsu.moe/manga/{slug}
        slug = manga_card.url.split("/")[-1]
        
        # API expects page starting at 0, bot uses 1. 
        # Note: Tachiyomi fetches all, but here we paginate by 1.
        # However, this API paginates chapters. 
        # If the bot requests page 1, we give page 0 of API.
        api_page = page - 1
        
        url = f"{self.base_url}/api/manga/chapters?id={slug}&filter=all&sort=desc&page={api_page}"
        content = await self.get_url(url)
        
        try:
            data = json.loads(content)
            chapters_data = data.get("chapters", [])
        except json.JSONDecodeError:
            return []

        chapters = []
        for ch in chapters_data:
            ch_id = ch.get("id")
            title = ch.get("title")
            number = ch.get("number", "")
            
            # Construct a unique URL for the chapter: https://atsu.moe/read/{slug}/{chapter_id}
            # We add a custom parameter so we can easily parse it back later
            chapter_url = f"{self.base_url}/read/{slug}/{ch_id}"
            
            # Clean up title
            display_title = f"{title}"
            if number:
                display_title = f"Ch. {number} - {title}"

            chapters.append(MangaChapter(self, display_title, chapter_url, manga_card, []))

        return chapters

    async def iter_chapters(self, manga_url: str, manga_name) -> AsyncIterable[MangaChapter]:
        manga_card = MangaCard(self, manga_name, manga_url, '')
        slug = manga_url.split("/")[-1]
        
        page = 0
        while True:
            url = f"{self.base_url}/api/manga/chapters?id={slug}&filter=all&sort=desc&page={page}"
            content = await self.get_url(url)
            try:
                data = json.loads(content)
                chapters_data = data.get("chapters", [])
                
                if not chapters_data:
                    break
                    
                for ch in chapters_data:
                    ch_id = ch.get("id")
                    title = ch.get("title")
                    number = ch.get("number", "")
                    chapter_url = f"{self.base_url}/read/{slug}/{ch_id}"
                    
                    display_title = f"{title}"
                    if number:
                        display_title = f"Ch. {number} - {title}"
                        
                    yield MangaChapter(self, display_title, chapter_url, manga_card, [])

                # Check pagination from DTO: hasNextPage logic (page + 1 < pages)
                total_pages = data.get("pages", 0)
                if page + 1 >= total_pages:
                    break
                page += 1
                
            except Exception:
                break

    async def pictures_from_chapters(self, content: bytes, response=None):
        # We need to extract the API URL from the passed Chapter URL
        # Chapter URL: https://atsu.moe/read/{slug}/{chapter_id}
        # API URL: https://atsu.moe/api/read/chapter?mangaId={slug}&chapterId={chapter_id}
        
        # 'response' object usually contains the URL requested. 
        # If 'content' is the HTML of the reader page, we might not need it if we call API directly.
        # But MangaClient structure usually calls 'get_url' on chapter.url before calling this.
        # Since the chapter URL is a valid page, 'content' will be the HTML.
        # However, it is cleaner to ignore the HTML and call the API directly.
        
        if response:
            url_str = str(response.url)
        else:
            return []

        try:
            parts = url_str.split("/")
            # read / slug / chapter_id
            # parts[-2] is slug, parts[-1] is chapter_id
            slug = parts[-2]
            chapter_id = parts[-1]
            
            api_url = f"{self.base_url}/api/read/chapter?mangaId={slug}&chapterId={chapter_id}"
            
            # We need to make a new request to the API
            json_content = await self.get_url(api_url)
            data = json.loads(json_content)
            
            # Parse logic from Kotlin: response.readChapter.pages.image
            pages = data.get("readChapter", {}).get("pages", [])
            
            images = []
            for p in pages:
                img_path = p.get("image")
                if img_path:
                    images.append(f"{self.base_url}{img_path}")
            
            return images
            
        except Exception as e:
            print(f"Error parsing Atsumaru images: {e}")
            return []

    async def contains_url(self, url: str):
        return url.startswith("https://atsu.moe/")

    async def check_updated_urls(self, last_chapters):
        # Use recentlyUpdated API
        url = f"{self.base_url}/api/infinite/recentlyUpdated?page=0&types=Manga,Manwha,Manhua,OEL"
        content = await self.get_url(url)
        try:
            data = json.loads(content)
            items = data.get("items", [])
            
            updated = []
            not_updated = []
            
            # Map of MangaID -> Latest Chapter Info
            latest_map = {}
            for item in items:
                manga_id = item.get("id")
                # The recently updated endpoint returns manga info, 
                # but to be 100% sure of the chapter URL, we might need to check chapter list.
                # However, usually checking if the manga is in the "Recently Updated" list is a good hint.
                # A safer way is to fetch the first chapter of every manga in 'last_chapters'
                pass
            
            # Since check_updated_urls logic can be complex, 
            # we will use the default "check every single one" fallback 
            # if we can't easily map the trending list to specific chapter URLs.
            # Returning empty lists triggers the default slow check in bot.py
            return [], [lc.url for lc in last_chapters]
            
        except Exception:
            return [], []