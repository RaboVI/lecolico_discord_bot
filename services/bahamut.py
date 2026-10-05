import os
import re
import asyncio
import urllib.parse
import requests
from datetime import datetime
import discord
from bs4 import BeautifulSoup

# 常駐連線與共用請求標頭
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}


class BahaSessionManager:
    """巴哈姆特專用常駐連線管理器：自動維護 CookieJar 與身分輪轉"""
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        self.load_initial_cookies()

    def load_initial_cookies(self):
        # 讀取環境變數中的小屋真實 Cookie，若無則帶入基本過年齡檢查 Cookie
        init_cookie_str = os.environ.get("BAHA_HOME_COOKIE", "ckR18=1;")
        for item in init_cookie_str.split(';'):
            if '=' in item:
                k, v = item.strip().split('=', 1)
                self.session.cookies.set(k.strip(), v.strip(), domain='.gamer.com.tw')

    def get(self, url: str, **kwargs):
        return self.session.get(url, timeout=10, **kwargs)


# 建立模組內全域 Session
baha_client = BahaSessionManager()


def _filter_and_extract_images(soup_or_div) -> list[str]:
    """通用圖片過濾器：支援 data-src 懶加載，嚴格排除表情符號、佔位圖與編輯器貼圖"""
    img_urls = []
    for img in soup_or_div.find_all('img'):
        src = img.get('data-src') or img.get('src') or ""
        class_name = " ".join(img.get('class', [])).lower()

        # 排除表情貼圖、內建 emoji 與 1x1 佔位圖
        if 'smilie' in class_name or 'emoji' in class_name:
            continue
        if 'plugins/smiles' in src or 'forum/smiles' in src or 'editor/emotion' in src:
            continue

        if src and src.startswith('http') and '1x1.gif' not in src:
            if src not in img_urls:
                img_urls.append(src)
    return img_urls


def _parse_gnn(soup: BeautifulSoup, target_url: str) -> dict:
    """GNN 新聞解析邏輯"""
    section_name = "GNN新聞"
    title_tag = soup.find('h1')
    title = title_tag.text.strip() if title_tag else "GNN新聞"

    content_div = soup.find('div', class_='GN-lbox3B')
    raw_content_text = content_div.text if content_div else ""

    # 解析時間
    date_str = ""
    date_match = re.search(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', soup.text)
    if date_match:
        try:
            dt = datetime.strptime(date_match.group(0), "%Y-%m-%d %H:%M:%S")
            date_str = dt.strftime("%Y/%m/%d %H:%M")
        except Exception:
            date_str = date_match.group(0)

    # 內文清洗與截斷
    lines = [line.strip() for line in raw_content_text.splitlines() if line.strip()]
    clean_text = "\n".join(lines)
    if len(clean_text) > 150:
        clean_text = clean_text[:150] + "..."

    # GNN 只取第一張重點圖片
    img_urls = _filter_and_extract_images(content_div or soup)
    preview_images = img_urls[:1]

    return {
        "title": title,
        "url": target_url,
        "description": clean_text if clean_text else "無文字內容",
        "date_str": date_str,
        "section_name": section_name,
        "preview_images": preview_images,
        "first_yt": None,
        "is_private": False
    }


def _parse_forum(soup: BeautifulSoup, target_url: str) -> dict | None:
    """哈啦板解析邏輯（首樓鎖定、看板名稱、不空行排版、多圖、YouTube 分離）"""
    first_post = soup.find('section', class_='c-section')
    if not first_post:
        return None

    # 看板名稱提取
    board_tag = soup.find('a', attrs={'data-gtm': '選單-看板名稱'})
    if board_tag and board_tag.get('title'):
        section_name = board_tag.get('title').strip()
    else:
        page_title = soup.find('title').text if soup.find('title') else ""
        board_match = re.search(r'@(.*?)\s+哈啦板', page_title)
        section_name = board_match.group(1).strip() if board_match else "哈啦板"

    title_tag = first_post.find('h1', class_='c-post__header__title')
    title = title_tag.text.strip() if title_tag else "哈啦版文章"

    date_tag = first_post.find('a', class_='edittime')
    date_str = date_tag.text.strip() if date_tag else ""

    # 純內文區塊
    article_content = first_post.find('div', class_='c-article__content')
    if article_content:
        raw_text = article_content.get_text(separator='\n').strip()
        clean_text = re.sub(r'\n{2,}', '\n', raw_text)
    else:
        clean_text = ""

    if len(clean_text) > 100:
        clean_text = clean_text[:100] + "..."

    # 提取多張圖片（最多 3 張）
    preview_images = _filter_and_extract_images(article_content or first_post)[:3]

    # YouTube 連結解析
    first_yt = None
    target_block = str(article_content) if article_content else str(first_post)
    yt_iframe = (
        first_post.find('iframe', attrs={'src': re.compile(r'youtube\.com/embed/')}) or
        first_post.find('iframe', attrs={'data-src': re.compile(r'youtube\.com/embed/')})
    )
    if yt_iframe:
        yt_src = yt_iframe.get('src') or yt_iframe.get('data-src') or ""
        match_yt = re.search(r'embed/([a-zA-Z0-9_-]+)', yt_src)
        if match_yt:
            first_yt = f"https://www.youtube.com/watch?v={match_yt.group(1)}"

    if not first_yt:
        yt_urls = re.findall(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[a-zA-Z0-9_-]+', target_block)
        if yt_urls:
            first_yt = yt_urls[0]

    return {
        "title": title,
        "url": target_url,
        "description": clean_text if clean_text else "無文字內容",
        "date_str": date_str,
        "section_name": section_name,
        "preview_images": preview_images,
        "first_yt": first_yt,
        "is_private": False
    }


def _parse_home(soup: BeautifulSoup, target_url: str) -> dict:
    """小屋創作解析邏輯（限制級檢測、Markdown 超連結修復、多圖）"""
    section_name = "小屋創作"
    article_content = soup.find('div', id='article_content')

    # 防呆機制：若被阻擋在權限牆外，回傳受限標記
    if not article_content:
        return {
            "title": "🔒 限制級或私密內容",
            "url": target_url,
            "description": "此小屋創作設有年齡限制、好友限定或已被刪除，請直接點擊標題前往網頁觀看。",
            "color": 0x2C2F33,
            "is_private": True
        }

    title_tag = soup.find('h1', class_='article-title')
    title = title_tag.text.strip() if title_tag else "小屋創作"

    # 發文時間
    date_str = ""
    for span in soup.find_all('span', class_='caption-text'):
        date_match = re.search(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', span.text)
        if date_match:
            try:
                dt = datetime.strptime(date_match.group(0), "%Y-%m-%d %H:%M:%S")
                date_str = dt.strftime("%Y/%m/%d %H:%M")
            except Exception:
                date_str = date_match.group(0)
            break

    # 轉換 Markdown 超連結
    for a in article_content.find_all('a'):
        href = a.get('href', '')
        if 'redir.php?url=' in href:
            try:
                encoded_url = href.split('redir.php?url=')[1].split('&')[0]
                href = urllib.parse.unquote(encoded_url)
            except Exception:
                pass

        link_text = a.get_text(separator='').strip()
        if link_text and href.startswith('http'):
            a.replace_with(f"[{link_text}]({href})")
        else:
            a.unwrap()

    # 排版換行處理
    for br in article_content.find_all('br'):
        br.replace_with('\n')
    for div in article_content.find_all(['div', 'p']):
        div.append('\n')

    raw_text = article_content.get_text(separator='').strip()
    clean_text = re.sub(r'\n{2,}', '\n', raw_text)

    # 150 字安全截斷，避免截斷 Markdown 超連結語法
    if len(clean_text) > 150:
        clean_text = clean_text[:150]
        clean_text = re.sub(r'\[[^\]]*$|\[[^\]]*\]\([^)]*$', '', clean_text).strip()
        clean_text = re.sub(r'[-*]+$', '', clean_text).strip()
        clean_text += "..."

    # 蒐集圖片（優先插圖區 div_illustration，接著是本文區）
    img_urls = []
    illustration_div = soup.find('div', id='div_illustration')
    if illustration_div:
        img_urls.extend(_filter_and_extract_images(illustration_div))

    for u in _filter_and_extract_images(article_content):
        if u not in img_urls:
            img_urls.append(u)

    preview_images = img_urls[:3]

    return {
        "title": title,
        "url": target_url,
        "description": clean_text if clean_text else "無文字內容",
        "date_str": date_str,
        "section_name": section_name,
        "preview_images": preview_images,
        "first_yt": None,
        "is_private": False
    }


def _fetch_bahamut_data_sync(raw_url: str) -> dict | None:
    """同步爬取與分流解析巴哈姆特三大板塊（背景執行緒執行）"""
    url = raw_url

    # 若為手機版網址，轉換為 PC 版標準網址發送請求
    if "m.gamer.com.tw/forum" in url:
        url = url.replace("m.gamer.com.tw/forum", "forum.gamer.com.tw")

    # 根據不同板塊設定 Cookie
    request_cookies = {}
    if "home.gamer.com.tw" not in url:
        request_cookies = {'BAHAID': 'discord_bot_preview', 'ckR18': '1'}

    try:
        if "home.gamer.com.tw" in url:
            res = baha_client.get(url)
        else:
            res = requests.get(url, headers=REQUEST_HEADERS, cookies=request_cookies, timeout=10)

        if res.status_code != 200:
            print(f"[Bahamut] 請求失敗，狀態碼: {res.status_code}")
            return None

        res.encoding = 'utf-8'
        soup = BeautifulSoup(res.text, 'html.parser')

        # 路由分流
        if "gnn.gamer.com.tw" in url:
            return _parse_gnn(soup, url)
        elif "forum.gamer.com.tw" in url:
            return _parse_forum(soup, url)
        elif "home.gamer.com.tw" in url:
            return _parse_home(soup, url)

    except Exception as e:
        print(f"[Bahamut] 解析巴哈姆特網址時發生錯誤: {e}")
        return None


async def process_bahamut_embed(raw_url: str, message: discord.Message, pending_suppress_ids: set):
    """
    巴哈姆特專用對外非同步調度入口
    """
    data = await asyncio.to_thread(_fetch_bahamut_data_sync, raw_url)
    if not data:
        return

    # 1. 處理限制級或私密內容提示卡片
    if data.get("is_private"):
        embed = discord.Embed(
            title=data["title"],
            url=data["url"],
            description=data["description"],
            color=data.get("color", 0x2C2F33)
        )
        await message.channel.send(embed=embed)
        pending_suppress_ids.add(message.id)
        try:
            await message.edit(suppress=True)
        except Exception:
            pass
        return

    # 2. 組裝標準 Embed 陣列 (支援多圖拼貼)
    embeds = []
    main_embed = discord.Embed(
        title=data["title"],
        url=data["url"],
        description=data["description"],
        color=0x00B4D8  # 巴哈姆特藍綠色
    )

    # 組合 Footer
    footer_text = f"巴哈姆特 • {data['section_name']}"
    if data["date_str"]:
        footer_text += f" • {data['date_str']}"
    main_embed.set_footer(text=footer_text)

    # 主圖
    preview_images = data.get("preview_images", [])
    if preview_images:
        main_embed.set_image(url=preview_images[0])
    embeds.append(main_embed)

    # 附屬圖片 Embed (達成 2~3 張圖並排)
    for img_url in preview_images[1:]:
        sub_embed = discord.Embed(url=data["url"])
        sub_embed.set_image(url=img_url)
        embeds.append(sub_embed)

    # 3. 發送訊息 (哈啦板若有 YT 先獨立發送)
    if data.get("first_yt"):
        await message.channel.send(content=data["first_yt"])

    await message.channel.send(embeds=embeds)

    # 4. 登記訊息並壓抑原始預覽
    pending_suppress_ids.add(message.id)
    try:
        await message.edit(suppress=True)
    except Exception as e:
        print(f"[Bahamut] 無法隱藏原始訊息預覽: {e}")