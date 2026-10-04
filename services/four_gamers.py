import re
import asyncio
import requests
import discord
from bs4 import BeautifulSoup
from datetime import datetime

# 偽裝一般桌面瀏覽器請求標頭
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}


def _fetch_4gamers_data_sync(raw_url: str) -> dict | None:
    """
    同步爬取與解析 4Gamers 新聞文章（在獨立背景執行緒中執行）
    """
    try:
        res = requests.get(raw_url, headers=REQUEST_HEADERS, timeout=10)
        if res.status_code != 200:
            print(f"[4Gamers] 請求失敗，狀態碼: {res.status_code}")
            return None

        res.encoding = 'utf-8'
        soup = BeautifulSoup(res.text, 'html.parser')

        # 1. 解析標題
        title_tag = soup.find('h1')
        title = title_tag.text.strip() if title_tag else "4Gamers 新聞"

        # 2. 解析分類標籤 (鎖定原始 HTML 中的分類超連結，不強制加 #)
        tag_text = ""
        category_a_tag = soup.find('a', href=re.compile(r'/news/category/'))
        if category_a_tag and category_a_tag.text.strip():
            tag_text = category_a_tag.text.strip().lstrip('#')

        # 備用方案：若超連結失效，嘗試從 JSON-LD 結構化資料中提取
        if not tag_text:
            for script in soup.find_all('script', type='application/ld+json'):
                if script.string and "NewsArticle" in script.string:
                    try:
                        import json
                        data = json.loads(script.string)
                        if isinstance(data, dict) and data.get("articleSection"):
                            tag_text = data.get("articleSection").lstrip('#')
                            break
                    except Exception:
                        pass

        # 3. 解析發文時間 (讀取 time 標籤的 datetime 屬性)
        date_str = ""
        time_tag = soup.find('time')
        if time_tag:
            raw_dt = time_tag.get('datetime', '')
            if raw_dt:
                try:
                    # 處理 ISO 格式時間 (例如: 2026-09-08T02:59:40.501Z)
                    clean_iso = re.sub(r'\.\d+Z$', 'Z', raw_dt)
                    dt = datetime.fromisoformat(clean_iso.replace('Z', '+00:00'))
                    date_str = dt.strftime("%Y/%m/%d %H:%M")
                except Exception:
                    date_str = time_tag.text.strip()
            else:
                date_str = time_tag.text.strip()

        # 4. 解析內文 (定位 data-news-content 並清洗空段落與空行)
        clean_text = ""
        content_div = soup.find('div', attrs={'data-news-content': True}) or soup.find('article')
        if content_div:
            c_soup = BeautifulSoup(str(content_div), 'html.parser')
            # 移除所有無實質文字的空段落或純空白 (&nbsp;)
            for p in c_soup.find_all('p'):
                if not p.text.replace('\xa0', '').strip():
                    p.decompose()

            # 逐行清洗多餘空行
            raw_lines = c_soup.get_text(separator='\n').splitlines()
            clean_lines = [line.strip() for line in raw_lines if line.strip()]
            clean_text = "\n".join(clean_lines)

            # 150 字安全截斷
            if len(clean_text) > 150:
                clean_text = clean_text[:150]
                clean_text = re.sub(r'\[[^\]]*$|\[[^\]]*\]\([^)]*$', '', clean_text).strip()
                clean_text = re.sub(r'[-*]+$', '', clean_text).strip()
                clean_text += "..."

        # 5. 解析封面大圖
        cover_img = None
        picture_tag = soup.find('picture')
        if picture_tag and picture_tag.find('img'):
            cover_img = picture_tag.find('img').get('src') or picture_tag.find('img').get('data-src')

        if not cover_img:
            og_img = soup.find('meta', property='og:image')
            if og_img and og_img.get('content'):
                cover_img = og_img.get('content')

        return {
            "title": title,
            "url": raw_url,
            "description": clean_text if clean_text else "點擊標題前往 4Gamers 查看完整內容。",
            "cover_img": cover_img,
            "tag": tag_text,
            "date": date_str
        }

    except Exception as e:
        print(f"[4Gamers] 解析新聞時發生異常: {e}")
        return None


async def process_4gamers_embed(target_url: str, message: discord.Message, pending_suppress_ids: set):
    """
    非同步處理 4Gamers 預覽發送與原訊息壓抑
    """
    data = await asyncio.to_thread(_fetch_4gamers_data_sync, target_url)
    if not data:
        return

    # 恢復為原本精確的 LOGO 深藍色 (0x0E2338)
    embed = discord.Embed(
        title=data["title"],
        url=data["url"],
        description=data["description"],
        color=0x0E2338
    )

    if data["cover_img"] and data["cover_img"].startswith('http'):
        embed.set_image(url=data["cover_img"])

    # Footer 格式：4Gamers • 分類標籤 • 發文時間
    footer_parts = ["4Gamers"]
    if data["tag"]:
        footer_parts.append(data["tag"])
    if data["date"]:
        footer_parts.append(data["date"])
    embed.set_footer(text=" • ".join(footer_parts))

    await message.channel.send(embed=embed)

    # 登記並壓抑原始訊息預覽
    pending_suppress_ids.add(message.id)
    try:
        await message.edit(suppress=True)
    except Exception as e:
        print(f"[4Gamers] 無法壓抑原始預覽: {e}")