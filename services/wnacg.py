import re
import asyncio
from urllib.parse import urljoin
import discord
from bs4 import BeautifulSoup
from curl_cffi import requests

REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7'
}


class ReadButtonView(discord.ui.View):
    """跳轉線上閱讀按鈕 View"""
    def __init__(self, read_url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="📖 直接閱讀",
            url=read_url,
            style=discord.ButtonStyle.link
        ))


def _fetch_wnacg_data_sync(target_url: str) -> dict | None:
    """
    在背景執行緒同步爬取 WNACG 資料 (使用 curl_cffi 偽裝指紋)
    """
    try:
        res = requests.get(
            target_url,
            headers=REQUEST_HEADERS,
            impersonate="chrome124",
            timeout=8
        )
        if res.status_code != 200:
            print(f"[WNACG] 請求失敗，狀態碼: {res.status_code}")
            return None

        soup = BeautifulSoup(res.text, 'html.parser')

        # 1. 抓取標題
        title_tag = soup.find('h2')
        title = title_tag.text.strip() if title_tag else "未知標題"

        # 2. 抓取封面圖
        cover_img = None
        thumb_div = soup.find('div', class_='uwthumb') or soup.find('div', class_='pic_box')
        if thumb_div and thumb_div.find('img'):
            img_tag = thumb_div.find('img')
            raw_src = str(img_tag.get('data-original') or img_tag.get('src') or '').strip()
            if raw_src:
                clean_path = re.sub(r'^(https?:)?/+', '', raw_src)
                if clean_path.startswith('www.wnacg.com') or '.' not in clean_path.split('/')[0]:
                    clean_path = re.sub(r'^www\.wnacg\.com/+', '', clean_path)
                    cover_img = f"https://www.wnacg.com/{clean_path}"
                else:
                    cover_img = f"https://{clean_path}"

        # 3. 精確抓取「分類」 (排除頁數、章節、編號等後續欄位)
        category_str = ""
        category_match = re.search(r'分類[：:]\s*([^頁章\s<]+)', soup.text)
        if category_match:
            category_str = category_match.group(1).strip()

        # 4. 判斷是否為合集，並動態提取「共 X 話」或「X 頁」
        count_str = ""
        if "合集" in category_str:
            # 合集：提取章節話數 (例如：章節：2 話 -> 共 2 話)
            chapter_match = re.search(r'章節[：:]\s*(\d+)', soup.text)
            if chapter_match:
                count_str = f"共 {chapter_match.group(1)} 話"
        else:
            # 一般作品：提取頁數 (例如：105P -> 105 頁)
            page_match = re.search(r'(\d+)\s*[P頁p]', soup.text)
            if page_match:
                count_str = f"{page_match.group(1)} 頁"

        # 5. 精準抓取「上傳日期」 (只保留 YYYY-MM-DD)
        upload_date = ""
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})', soup.text)
        if date_match:
            upload_date = date_match.group(1)

        # 6. 抓取線上閱讀按鈕連結
        read_button = soup.find('a', class_='btn_read')
        read_url = target_url
        if read_button and read_button.get('href'):
            read_url = urljoin(target_url, read_button['href'])

        return {
            "title": title,
            "cover_img": cover_img,
            "category": category_str,
            "count_str": count_str,
            "upload_date": upload_date,
            "read_url": read_url
        }

    except Exception as e:
        print(f"[WNACG] 解析過程發生異常: {e}")
        return None


async def process_wnacg_embed(target_url: str, message: discord.Message, pending_suppress_ids: set):
    """
    非同步處理 WNACG 預覽發送與原訊息壓抑
    """
    data = await asyncio.to_thread(_fetch_wnacg_data_sync, target_url)
    if not data:
        return

    # 組裝極簡乾淨 Embed
    embed = discord.Embed(
        title=data["title"],
        url=target_url,
        color=0xF4A7B9  # WNACG 專屬粉色
    )

    if data["cover_img"]:
        embed.set_image(url=data["cover_img"])

    # 組合指定 Footer 格式：紳士漫畫 • 分類 • 數量(頁數/話數) • 日期
    footer_parts = ["紳士漫畫"]
    if data["category"]:
        footer_parts.append(data["category"])
    if data["count_str"]:
        footer_parts.append(data["count_str"])
    if data["upload_date"]:
        footer_parts.append(data["upload_date"])

    embed.set_footer(text=" • ".join(footer_parts))

    view = ReadButtonView(read_url=data["read_url"])

    await message.channel.send(embed=embed, view=view)

    # 登記並壓抑原始訊息預覽
    pending_suppress_ids.add(message.id)
    try:
        await message.edit(suppress=True)
    except Exception as e:
        print(f"[WNACG] 無法隱藏原始訊息預覽: {e}")