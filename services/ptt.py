import re
import copy
import asyncio
import requests
from datetime import datetime
import discord
from bs4 import BeautifulSoup

# 模擬一般桌面瀏覽器請求標頭
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}
# 繞過 18 禁驗證 Cookie
OVER18_COOKIES = {'over18': '1'}


def _fetch_ptt_article_sync(target_ptt_url: str) -> dict | None:
    """
    同步爬取與清洗 PTT 官方文章（在背景執行緒執行）
    """
    try:
        res = requests.get(target_ptt_url, headers=REQUEST_HEADERS, cookies=OVER18_COOKIES, timeout=10)
        if res.status_code != 200:
            print(f"[PTT] 請求失敗，狀態碼: {res.status_code}")
            return None

        soup = BeautifulSoup(res.text, 'html.parser')
        main_content = soup.find('div', id='main-content')
        if not main_content:
            return None

        # 1. 提取作者、看板、標題、時間
        meta_values = main_content.find_all('span', class_='article-meta-value')
        board = meta_values[1].text.strip() if len(meta_values) > 1 else ""
        title = meta_values[2].text.strip() if len(meta_values) > 2 else "PTT 文章"
        raw_date_str = meta_values[3].text.strip() if len(meta_values) > 3 else ""

        # 時間格式化：Fri Sep  4 13:33:05 2026 -> 2026/09/04 13:33
        date_str = raw_date_str
        if raw_date_str:
            try:
                dt = datetime.strptime(re.sub(r'\s+', ' ', raw_date_str), '%a %b %d %H:%M:%S %Y')
                date_str = dt.strftime('%Y/%m/%d %H:%M')
            except Exception:
                date_str = raw_date_str

        # 2. 備份 DOM 並剔除推文、meta 標籤、引文與浮水印
        content_copy = copy.copy(main_content) if 'copy' in globals() else BeautifulSoup(str(main_content), 'html.parser')

        for elem in content_copy.find_all(['div', 'span'], class_=['article-metaline', 'article-metaline-right', 'push', 'f2']):
            elem.decompose()

        for f6_elem in content_copy.find_all('span', class_='f6'):
            f6_elem.decompose()

        # 3. 提取圖片與 YouTube 連結
        first_image = None
        for a_tag in content_copy.find_all('a', href=True):
            href = a_tag['href']
            if re.search(r'\.(jpg|jpeg|png|gif|webp)(\?.*)?$', href, re.I) or 'i.meee.com.tw' in href or 'imgur.com' in href:
                first_image = href
                break

        # 4. 清理純文字、簽名檔與圖片連結
        raw_text = content_copy.get_text()
        raw_text = re.split(r'※\s*發信站:|--', raw_text)[0]
        raw_text = re.sub(r'https?://\S+?\.(?:jpg|jpeg|png|gif|webp)(?:\?\S*)?', '', raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r'https?://(?:i\.)?imgur\.com/[a-zA-Z0-9]{5,7}', '', raw_text)

        # 排除回文引用行（: 或 ： 開頭）
        raw_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        clean_lines = []
        for line in raw_lines:
            if line.startswith('※ 引述') or '之銘言：' in line:
                continue
            if re.match(r'^[::\uff1a]', line):
                continue
            clean_lines.append(line)

        # 防呆機制：全篇皆為引文時回退顯示原始文字
        if not clean_lines and raw_lines:
            clean_lines = raw_lines

        description = "\n".join(clean_lines)
        if len(description) > 150:
            description = description[:150] + "..."

        return {
            "title": title,
            "board": board,
            "date_str": date_str,
            "description": description if description else "點擊標題閱讀全文",
            "first_image": first_image
        }

    except Exception as e:
        print(f"[PTT] 爬蟲解析過程發生錯誤: {e}")
        return None


async def process_ptt_embed(target_ptt_url: str, display_url: str, message: discord.Message, source_name: str, pending_suppress_ids: set):
    """
    非同步處理 PTT 與 PTTWeb 預覽發送與原訊息壓抑
    """
    data = await asyncio.to_thread(_fetch_ptt_article_sync, target_ptt_url)
    if not data:
        return

    # 組裝自製 Embed
    embed = discord.Embed(
        title=data["title"],
        url=display_url,  # 點擊標題跳轉至貼文原始網址
        description=data["description"],
        color=0xF3F3F3   # PTT 經典簡潔淺灰
    )

    if data["first_image"]:
        embed.set_image(url=data["first_image"])

    # Footer 格式：來源 (PTT 或 PTTWeb) • 看板名稱 • 發文時間
    footer_parts = [source_name]
    if data["board"]:
        footer_parts.append(data["board"])
    if data["date_str"]:
        footer_parts.append(data["date_str"])
    embed.set_footer(text=" • ".join(footer_parts))

    await message.channel.send(embed=embed)

    # 登記並壓抑原始訊息預覽
    pending_suppress_ids.add(message.id)
    try:
        await message.edit(suppress=True)
    except Exception as e:
        print(f"[{source_name}] 無法隱藏原始訊息預覽: {e}")


async def handle_pttweb_url(raw_pttweb_url: str, match: re.Match, message: discord.Message, pending_suppress_ids: set):
    """
    PTTWeb 轉址調度器：將標準網址與短網址轉換為官方 PTT 網址後呼叫共用解析
    """
    target_ptt_url = None

    # 情況 A：標準網址 /bbs/{看板}/{文章ID}
    if match.group(1) and match.group(2):
        board = match.group(1)
        article_id = match.group(2)
        if not article_id.endswith('.html'):
            article_id += '.html'
        target_ptt_url = f"https://www.ptt.cc/bbs/{board}/{article_id}"

    # 情況 B：短網址 /s/{看板}/{短代碼}
    elif match.group(3) and match.group(4):
        try:
            s_res = await asyncio.to_thread(requests.get, raw_pttweb_url, headers=REQUEST_HEADERS, timeout=5)
            if s_res.status_code == 200:
                real_ptt_match = re.search(r'https?://www\.ptt\.cc/bbs/[^/]+/[A-Za-z0-9\._]+\.html', s_res.text)
                if real_ptt_match:
                    target_ptt_url = real_ptt_match.group(0)
        except Exception as e:
            print(f"[PTTWeb] 解析短網址異常: {e}")

    if target_ptt_url:
        await process_ptt_embed(
            target_ptt_url=target_ptt_url,
            display_url=raw_pttweb_url,
            message=message,
            source_name="PTTWeb",
            pending_suppress_ids=pending_suppress_ids
        )