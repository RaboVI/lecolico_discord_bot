import discord
import re
import os
import random
import requests
import asyncio
import copy
import urllib
import urllib.parse # <--- 新增此行，用於處理中日文 Hashtag 網址轉碼
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from dotenv import load_dotenv
from cachetools import TTLCache
# --- 引入外部 services 模組 ---
from services.hanime import process_hanime_embed
from services.pixiv import process_pixiv_embed
from services.Gamekee_BD2 import process_gamekee_bd2_embed
from services.Nikke_Official_tw import process_nikke_embed

# 自動讀取本地 .env 檔案中的環境變數
# 若在 Railway 線上運行，Railway 會直接提供環境變數，此函式會自動略過而不報錯
load_dotenv()

# 建立小屋創作 Embed 快取：最多儲存 100 筆，每筆有效時間 2 小時 (7200 秒)
baha_artwork_cache = TTLCache(maxsize=100, ttl=86400)

class BahaSessionManager:
    """巴哈姆特專用常駐連線管理器：自動維護 CookieJar 與 Token 輪轉"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://home.gamer.com.tw/'
        })
        self.load_initial_cookies()

    def load_initial_cookies(self):
        init_cookie_str = os.environ.get("BAHA_HOME_COOKIE", "ckR18=1;")
        for item in init_cookie_str.split(';'):
            if '=' in item:
                k, v = item.strip().split('=', 1)
                self.session.cookies.set(k.strip(), v.strip(), domain='.gamer.com.tw')

    def get(self, url, **kwargs):
        return self.session.get(url, timeout=10, **kwargs)


baha_client = BahaSessionManager()

# 設定 Intents 以便讀取訊息內容
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
pending_suppress_ids = set()    # 追蹤「Bot 已經自己產生過 Embed」的訊息 ID
# 用途：讓 on_message_edit 知道，如果 Discord 之後才把原生預覽貼到這則訊息上，
# 要主動把它隱藏掉——不再靠固定延遲去「賭」時間點


# 建立 Hashtag 自動超連結轉換函式
def linkify_hashtags(text):
    def replace_hashtag(match):
        tag = match.group(1)
        # 對標籤進行 URL 編碼 (確保中文/日文 Hashtag 連結能正常點擊)
        encoded_tag = urllib.parse.quote(tag)
        return f"[#{tag}](https://x.com/hashtag/{encoded_tag})"

    # 正則表達式：匹配獨立的 #標籤 (排除網址內部的 # 符號與標點符號)
    return re.sub(r'(?<!\S)#([^\s#.,!?:;，。！？]+)', replace_hashtag, text)


class ReadButtonView(discord.ui.View):
    def __init__(self, read_url: str):
        super().__init__(timeout=None) # 設定 timeout=None 讓按鈕長期有效
        # 新增一個跳轉連結按鈕
        self.add_item(discord.ui.Button(
            label="📖 直接閱讀",
            url=read_url,
            style=discord.ButtonStyle.link
        ))


# 用正規表達式比對 wnacg 的網址
URL_PATTERN = r"(https?://(www\.)?wnacg\.com/photos-index-aid-\d+\.html)"
# 匹配 bilibili.com 或 b23.tv 的網址 (避免重複處理已經是 vxbilibili 的連結)
BILIBILI_PATTERN = r"(https?://(www\.|m\.)?(bilibili\.com|b23\.tv)/[^\s]+)"
# 匹配 facebook.com 或 fb.watch 的網址
FACEBOOK_PATTERN = r"(https?://(www\.|m\.|web\.)?(facebook\.com|fb\.watch)/[^\s]+)"
# 新增：匹配 threads.net 的網址
THREADS_PATTERN = r"(https?://(www\.)?threads\.(net|com)/[^\s]+)"
# 新增：匹配 instagram.com 的網址
INSTAGRAM_PATTERN = r"(https?://(www\.)?instagram\.com/[^\s]+)"
# 匹配 x.com 或 twitter.com 的貼文網址
X_PATTERN = r"(https?://(www\.)?(x|twitter)\.com/[^\s]+/status/\d+)"
# 巴哈姆特網址正規表達式
BAHA_PATTERN = r"(https?://(?:(gnn|forum|home)\.gamer\.com\.tw|m\.gamer\.com\.tw/forum)/[^\s]+)"
# 4Gamers 網址正規表達式
FOURGAMERS_PATTERN = r"(https?://(www\.)?4gamers\.com\.tw/news/detail/\d+/[^\s]+)"
# 《勝利女神：妮姬》官網新聞網址正規表達式 (支援一般版與 /m/ 手機版)
NIKKE_PATTERN = r"(https?://nikke\.hotcool\.tw/(?:m/)?News_detail-\d+)"
# Hanime1 網址正規表達式
HANIME_PATTERN = r"(https?://hanime1\.me/watch\?v=\d+)"
# 匹配 Pixiv 網址 (支援 artworks/ID、member_illust.php 與 /i/ID)
PIXIV_PATTERN = r"(https?://(?:www\.)?pixiv\.net/(?:(?:en/)?artworks/|member_illust\.php\?illust_id=)(\d+)|https?://pixiv\.net/i/(\d+))"
# 匹配 Gamekee底下棕色塵埃2 網址
GAMEKEE_BD2_PATTERN = r"https?://(?:www\.)?gamekee\.com/zsca2/(\d+)\.html"


async def process_ptt_embed(target_ptt_url: str, display_url: str, message: discord.Message, source_name: str = "PTT"):
    """
    共用 PTT 解析函式：
    - target_ptt_url: 向 PTT 官方發送請求的網址 (例如 https://www.ptt.cc/bbs/...html)
    - display_url: Embed 標題要跳轉的超連結 (如果是 PTTWeb 傳入原始網址，若原生 PTT 則傳入 target_ptt_url)
    - source_name: Footer 顯示的來源名稱 ("PTT" 或 "PTTWeb")
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        cookies = {'over18': '1'}

        res = requests.get(target_ptt_url, headers=headers, cookies=cookies, timeout=10)
        if res.status_code != 200:
            return

        soup = BeautifulSoup(res.text, 'html.parser')
        main_content = soup.find('div', id='main-content')
        if not main_content:
            return

        # 1. 提取作者、看板、標題、時間等元數據
        meta_values = main_content.find_all('span', class_='article-meta-value')
        author = meta_values[0].text.strip() if len(meta_values) > 0 else ""
        board = meta_values[1].text.strip() if len(meta_values) > 1 else ""
        title = meta_values[2].text.strip() if len(meta_values) > 2 else "PTT 文章"
        raw_date_str = meta_values[3].text.strip() if len(meta_values) > 3 else ""

        # 時間格式化：將 "Wed Sep  9 13:09:36 2026" 轉換為 "2026/09/09 13:09"
        date_str = raw_date_str
        if raw_date_str:
            try:
                from datetime import datetime
                # PTT 的日期格式為 "%a %b %d %H:%M:%S %Y"（中間可能有多餘空格，strptime 會自動處理）
                dt = datetime.strptime(re.sub(r'\s+', ' ', raw_date_str), '%a %b %d %H:%M:%S %Y')
                date_str = dt.strftime('%Y/%m/%d %H:%M')
            except Exception:
                date_str = raw_date_str

        # 2. 移除推文、meta 標籤、引文與簽名檔，提取純內文與主文圖片
        content_copy = copy.copy(main_content) if 'copy' in globals() else BeautifulSoup(str(main_content),
                                                                                         'html.parser')

        # (a) 移除推文、上方 metadata 與 f2 雜訊行
        for elem in content_copy.find_all(['div', 'span'],
                                          class_=['article-metaline', 'article-metaline-right', 'push', 'f2']):
            elem.decompose()

        # (b) 拔除綠色引文節點 (span.f6 為 PTT 引文專用標籤)
        for f6_elem in content_copy.find_all('span', class_='f6'):
            f6_elem.decompose()

        # (c) 針對「作者自身發言的主文區塊」提取第一張圖片
        first_image = None
        for a_tag in content_copy.find_all('a', href=True):
            href = a_tag['href']
            if re.search(r'\.(jpg|jpeg|png|gif|webp)(\?.*)?$', href,
                         re.I) or 'i.meee.com.tw' in href or 'imgur.com' in href:
                first_image = href
                break

        # (d) 獲取純文字並截斷簽名檔 (※ 發信站:、-- 等)
        raw_text = content_copy.get_text()
        raw_text = re.split(r'※\s*發信站:|--', raw_text)[0]

        # 清理內文中的圖片網址 (包含常見圖床與副檔名)
        raw_text = re.sub(r'https?://\S+?\.(?:jpg|jpeg|png|gif|webp)(?:\?\S*)?', '', raw_text,
                          flags=re.IGNORECASE)
        raw_text = re.sub(r'https?://(?:i\.)?imgur\.com/[a-zA-Z0-9]{5,7}', '', raw_text)

        # 逐行清洗空行與文字
        raw_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        clean_lines = []
        for line in raw_lines:
            if line.startswith('※ 引述') or '之銘言：' in line:
                continue
            if re.match(r'^[::\uff1a]', line):
                continue
            clean_lines.append(line)

        # 防呆機制：若回文作者沒有寫字 (純引用)，退回顯示原本行避免空白
        if not clean_lines and raw_lines:
            clean_lines = raw_lines

        description = "\n".join(clean_lines)
        if len(description) > 150:
            description = description[:150] + "..."

        # 4. 組裝 Embed (標題超連結指定為 display_url)
        embed = discord.Embed(
            title=title,
            url=display_url,
            description=description,
            color=0xf3f3f3
        )

        if first_image:
            embed.set_image(url=first_image)

        # 組裝 Footer
        footer_parts = [source_name]
        if board:
            footer_parts.append(f"{board}")
        if date_str:
            footer_parts.append(date_str)
        embed.set_footer(text=" • ".join(footer_parts))

        # 發送 Embed
        await message.channel.send(embed=embed)

        # 登記這則訊息，交給 on_message_edit 負責後續補刀
        pending_suppress_ids.add(message.id)

        # 隱藏原訊息預覽
        try:
            await message.edit(suppress=True)
        except Exception as e:
            print(f"無法隱藏原始訊息預覽: {e}")

    except Exception as e:
        print(f"處理 PTT/PTTWeb 解析時發生錯誤: {e}")


@client.event
async def on_ready():
    print(f'Bot 已成功登入為 {client.user}')


@client.event
async def on_message_edit(before, after):
    # 忽略 Bot 自己的編輯事件
    if after.author.bot:
        return

    # 1. 處理待補刀名單中的訊息 (原有的 FB / 其他平台隱藏預覽邏輯)
    if after.id in pending_suppress_ids:
        if after.embeds and not after.flags.suppress_embeds:
            try:
                await after.edit(suppress=True)
            except Exception as e:
                print(f"on_message_edit 隱藏預覽失敗: {e}")
            finally:
                pending_suppress_ids.discard(after.id)
        return

@client.event
async def on_message(message):
    # 避免 Bot 回應自己的訊息
    if message.author == client.user:
        return

    # 檢查訊息中是否包含目標網址
    match = re.search(URL_PATTERN, message.content)
    if match:
        url = match.group(0)

        # 設定 User-Agent 模擬一般瀏覽器，避免被網站阻擋
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }

        try:
            # 請求網頁內容
            res = requests.get(url, headers=headers)
            res.encoding = 'utf-8'  # 確保中文不會亂碼
            soup = BeautifulSoup(res.text, 'html.parser')

            # 1. 解析標題
            title_tag = soup.find('title')
            title = title_tag.text if title_tag else "Wnacg 畫集"

            # --- 解析標籤 (Tags) ---
            tags_list = []

            # 1. 尋找包含標籤的容器 (class="addtk" 或包含 tag 的區塊)
            tag_container = soup.find('div', class_='addtk') or soup.find('div', class_=re.compile(r'tag'))

            if tag_container:
                # 逐一提取容器內每一個 <a> 標籤的文字
                for a_tag in tag_container.find_all('a'):
                    tag_text = a_tag.text.strip()
                    # 過濾掉 "+TAG" 按鈕與空白
                    if tag_text and not tag_text.startswith('+') and tag_text != 'TAG':
                        tags_list.append(f"`{tag_text}`")

            # 2. 備用方案：如果上面沒抓到，全頁搜尋帶有 tags/tag 連結的 <a> 標籤
            if not tags_list:
                for a_tag in soup.find_all('a', href=re.compile(r'tag')):
                    tag_text = a_tag.text.strip()
                    if tag_text and not tag_text.startswith('+') and tag_text != 'TAG':
                        tags_list.append(f"`{tag_text}`")

            tags_str = " ".join(tags_list) if tags_list else "無標籤"

            # --- 解析頁數 ---
            pages_str = "未知"
            # 在整頁文字中用正規表達式搜尋 "漢化" 或 "分類" 後面的頁數 (例如 28P 或 28頁)
            page_match = re.search(r'(\d+)\s*[P頁p]', soup.text)
            if page_match:
                pages_str = f"{page_match.group(1)} 頁"

            # --- 解析上傳日期 ---
            upload_date = ""
            # 使用正規表達式匹配 "上傳於" 或直接搜尋 YYYY-MM-DD 日期格式
            date_match = re.search(r'上傳[於[:\s]*(\d{4}-\d{2}-\d{2})', soup.text)
            if not date_match:
            # 備用方案：只搜尋 YYYY-MM-DD
                date_match = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', soup.text)

            if date_match:
                upload_date = f"上傳於 {date_match.group(1)}"

            # 2. 解析封面圖片 (通常位在 class="uwthumb" 內的 img)
            cover_img = None
            thumb_div = soup.find('div', class_='uwthumb')
            if thumb_div and thumb_div.find('img'):
                img_tag = thumb_div.find('img')
                raw_src = str(img_tag.get('data-original') or img_tag.get('src') or '').strip()

                if raw_src:
                    # 1. 強制清除開頭所有的 http:, https:, 與斜線 /
                    clean_path = re.sub(r'^(https?:)?/+', '', raw_src)

                    # 2. 判斷清洗後的路徑是包含第三方圖床網域（如 t4.qy0.ru）還是站內路徑
                    if clean_path.startswith('www.wnacg.com') or not '.' in clean_path.split('/')[0]:
                        # 如果是站內路徑或以 www.wnacg.com 開頭
                        clean_path = re.sub(r'^www\.wnacg\.com/+', '', clean_path)
                        cover_img = f"https://www.wnacg.com/{clean_path}"
                    else:
                        # 獨立圖床網址（如 t4.qy0.ru/data/...）
                        cover_img = f"https://{clean_path}"

            # 在終端機印出結果除錯
            # print(f"標題: {title}")
            # print(f"最終處理的圖片網址: {cover_img}")

            # 3. 生成閱讀頁面網址 (將 index 替換為 slide)
            read_url = url.replace('photos-index-aid-', 'photos-slide-aid-')

            # 4. 建立 Discord Embed 物件
            embed = discord.Embed(title=title, url=url, color=0xffb6c1)

            # 加入標籤與頁數欄位 (inline=True 代表兩個欄位會盡量並排)
            embed.add_field(name="🏷️ 標籤", value=tags_str, inline=False)

            # 嚴格把關：只有當 cover_img 存在且確定是 http/https 開頭的標準網址時，才放入 Embed
            if cover_img and cover_img.startswith('http'):
                embed.set_image(url=cover_img)
            else:
                print("⚠️ 圖片網址格式異常，本次 Embed 將不顯示封面圖。")

            # --- 整合頁數與日期至 Footer ---
            footer_parts = []

            if pages_str != "未知":
                footer_parts.append(f"📄 {pages_str}")

            if upload_date:
                footer_parts.append(upload_date)

            # 使用分隔點 " • " 將所有資訊串接在一起
            embed.set_footer(text=" • ".join(footer_parts))

            # 5. 建立按鈕 View 物件
            view = ReadButtonView(read_url=read_url)

            # 6. 發送 Embed 訊息並附帶按鈕 (view)
            await message.channel.send(embed=embed, view=view)

            # 登記這則訊息，交給 on_message_edit 負責後續補刀
            pending_suppress_ids.add(message.id)

            # 7. 隱藏使用者發送的原始連結預覽 (需要 Bot 具備管理訊息權限)
            await message.edit(suppress=True)

        except Exception as e:
            print(f"解析網址時發生錯誤: {e}")

    # ================= 處理 Bilibili 網址 =================
    # 確保訊息中包含 bilibili 或 b23，且不是已經轉換過的 vx 連結
    if re.search(BILIBILI_PATTERN, message.content) and not re.search(r"vx(bilibili|b23)", message.content):
        bili_match = re.search(BILIBILI_PATTERN, message.content)
        if bili_match:
            raw_bili_url = bili_match.group(0)

            # 根據網域精準替換
            if "b23.tv" in raw_bili_url:
                fix_url = raw_bili_url.replace("b23.tv", "vxb23.tv")
            else:
                fix_url = raw_bili_url.replace("bilibili.com", "vxbilibili.com")

            # 發送隱形字元加換行，讓 Discord 讀取網址產生卡片，但畫面上方不會有明顯網址
            await message.channel.send(f"[Bilifix]({fix_url})")

            # 登記這則訊息，交給 on_message_edit 負責後續補刀
            pending_suppress_ids.add(message.id)

            # 隱藏使用者發送的原始訊息預覽
            try:
                await message.edit(suppress=True)
            except Exception as e:
                print(f"無法隱藏原始訊息預覽: {e}")


    # ================= 處理 Facebook 網址 =================
    if re.search(FACEBOOK_PATTERN, message.content) and "facebed.com" not in message.content:
        fb_match = re.search(FACEBOOK_PATTERN, message.content)
        if fb_match:
            raw_fb_url = fb_match.group(0)
            target_fb_url = raw_fb_url

            # 針對 /share/ 短跳轉進行路徑還原
            if "/share/" in raw_fb_url:
                try:
                    headers = {
                        'User-Agent': 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)',
                        'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7'
                    }

                    # 1. 優先發送 HEAD 請求
                    head_res = requests.head(raw_fb_url, headers=headers, allow_redirects=False, timeout=5)
                    location = head_res.headers.get('Location', '')

                    # (A) 直接從 Location 提取 story_fbid (雲端機房最常見且穩定的返回格式)
                    fbid_match = re.search(r'story_fbid=(\d+)', location)
                    if fbid_match:
                        target_fb_url = f"https://www.facebook.com/reel/{fbid_match.group(1)}"

                    # (B) 若直接跳轉至完整路徑 (如 /reel/ 或 /posts/)
                    elif location and "/share/" not in location and "/login" not in location:
                        target_fb_url = location.split('?')[0]

                    # 2. 若 HEAD 未取得，降級至 GET 深度提取
                    if "/share/" in target_fb_url:
                        res = requests.get(raw_fb_url, headers=headers, allow_redirects=True, timeout=8)
                        final_url = res.url

                        # 從登入跳轉參數提取 (如 next=...story_fbid%3D123...)
                        if "/login" in final_url or "login.php" in final_url:
                            fbid_in_login = re.search(r'(?:story_fbid%3D|video_id%3D)(\d+)', final_url)
                            if fbid_in_login:
                                target_fb_url = f"https://www.facebook.com/reel/{fbid_in_login.group(1)}"
                            else:
                                match_next = re.search(r'[?&](?:next|u)=([^&]+)', final_url)
                                if match_next:
                                    decoded = urllib.parse.unquote(match_next.group(1))
                                    if "/share/" not in decoded and "/login" not in decoded:
                                        target_fb_url = decoded.split('?')[0]
                        elif "/share/" not in final_url:
                            target_fb_url = final_url.split('?')[0]

                except Exception as e:
                    print(f"解析 FB Share 短網址時發生錯誤: {e}")

            # 3. 終極防呆：若最終仍是登入頁或無效頁面，退回原始網址
            if "/login" in target_fb_url or "login.php" in target_fb_url or target_fb_url.endswith("/story.php"):
                target_fb_url = raw_fb_url

            # 替換為 facebed 代理
            fix_fb_url = re.sub(r"(facebook\.com|fb\.watch)", "facebed.com", target_fb_url)

            # 發送修復後的超連結
            await message.channel.send(f"[FBfix]({fix_fb_url})")
            pending_suppress_ids.add(message.id)

            try:
                await message.edit(suppress=True)
            except Exception as e:
                print(f"無法隱藏原始訊息預覽: {e}")

    # ================= 新增：處理 Threads 網址 =================
    if re.search(THREADS_PATTERN, message.content) and "fixthreads.seria.moe" not in message.content:
        threads_match = re.search(THREADS_PATTERN, message.content)
        if threads_match:
            raw_threads_url = threads_match.group(0)

            # 將 threads.net 替換為 fixthreads.seria.moe
            fix_threads_url = raw_threads_url.replace("www.threads.com", "fixthreads.seria.moe")

            # 由 Bot 發送代理網址以展示完整 Threads 卡片預覽
            await message.channel.send(f"[Threadsfix]({fix_threads_url})")

            # 登記這則訊息，交給 on_message_edit 負責後續補刀
            pending_suppress_ids.add(message.id)

            # 隱藏使用者發送的原始訊息預覽
            try:
                await message.edit(suppress=True)
            except Exception as e:
                print(f"無法隱藏原始訊息預覽: {e}")

    # ================= 新增：處理 Instagram 網址 =================
    # 確保訊息包含 IG 連結，且沒有被代理過 (排除 og/hh/kk)
    if re.search(INSTAGRAM_PATTERN, message.content) and not re.search(r"(og|hh|kk)instagram\.com", message.content):
        ig_match = re.search(INSTAGRAM_PATTERN, message.content)
        if ig_match:
            raw_ig_url = ig_match.group(0)

            # 建立代理伺服器清單
            ig_proxies = ["oginstagram.com", "hhinstagram.com", "kkinstagram.com"]

            # 隨機選擇一個代理
            chosen_proxy = random.choice(ig_proxies)

            # 將 instagram.com 替換為隨機選中的代理網域
            fix_ig_url = re.sub(r"instagram\.com", chosen_proxy, raw_ig_url)

            # 發送隱藏網址文字的超連結
            await message.channel.send(f"[IGfix]({fix_ig_url})")

            # 隱藏使用者發送的原始訊息預覽
            try:
                await message.edit(suppress=True)
            except Exception as e:
                print(f"無法隱藏原始訊息預覽: {e}")

    # ================= 處理 X / Twitter 網址 =================
    # 確保訊息包含 X 連結，且沒有被代理過 (排除 vx, fx, fixup, fixvx 等前綴)
    if re.search(X_PATTERN, message.content) and not re.search(r"(vx|fx|fixupx|fixvx)(x|twitter)\.com",
                                                               message.content):
        x_match = re.search(X_PATTERN, message.content)
        if x_match:
            raw_x_url = x_match.group(0)

            # 邏輯 4: 將網址轉換為 API 查詢網址 (將 x.com 或 twitter.com 替換為 api.vxtwitter.com)
            api_url = re.sub(r"(x|twitter)\.com", "api.vxtwitter.com", raw_x_url)

            try:
                # 請求 API 獲取推文資料
                response = requests.get(api_url, timeout=6)

                if response.status_code == 200:
                    tweet_data = response.json()

                    has_media = tweet_data.get("hasMedia", False)
                    media_extended = tweet_data.get("media_extended", [])

                    # 判斷是否有影片或 GIF
                    has_video_or_gif = any(m.get("type") in ["video", "gif"] for m in media_extended)

                    # 邏輯 7: 沒有媒體 (純文字推文) -> 保留原生預覽
                    if not has_media:
                        print("此為純文字推文，保留原生預覽 (不做事)。")

                    # 邏輯 9: 有影片，則隨機呼叫代理服務
                    elif has_video_or_gif:
                        x_proxies = ["fixvx.com", "fixupx.com"]
                        chosen_proxy = random.choice(x_proxies)

                        domain_match = re.search(r"(x|twitter)\.com", raw_x_url).group(0)
                        fix_x_url = raw_x_url.replace(domain_match, chosen_proxy)

                        await message.channel.send(f"[Xfix]({fix_x_url})")

                        # 登記這則訊息，交給 on_message_edit 負責後續補刀
                        pending_suppress_ids.add(message.id)

                        try:
                            await message.edit(suppress=True)
                        except Exception as e:
                            print(f"無法隱藏原始訊息預覽: {e}")

                    # 邏輯 5 & 8: 有媒體且非影片 (所有圖片推文統一自組 Embed，徹底消滅 18+ 成人限制擋板並支援多圖)
                    else:
                        raw_text = tweet_data.get("text", "")
                        likes = tweet_data.get("likes", 0)
                        views = tweet_data.get("views")
                        author_name = tweet_data.get("user_name", "")
                        author_screen_name = tweet_data.get("user_screen_name", "")
                        author_avatar = tweet_data.get("user_profile_image_url", "")
                        date_epoch = tweet_data.get("date_epoch", 0)

                        # 1. 將內文中的 Hashtag 轉為超連結
                        formatted_text = linkify_hashtags(raw_text)

                        # 2. 建立主卡片框架
                        primary_embed = discord.Embed(
                            description=formatted_text if formatted_text else None,
                            url=raw_x_url,
                            color=0x1DA1F2,  # Twitter 藍色
                            timestamp=datetime.fromtimestamp(date_epoch, timezone.utc)
                        )

                        # 設定作者與頭像
                        primary_embed.set_author(
                            name=f"{author_name} (@{author_screen_name})",
                            url=raw_x_url,
                            icon_url=author_avatar if author_avatar else None
                        )

                        # 篩選所有圖片 URL
                        image_urls = [m.get("url") for m in media_extended if m.get("url")]

                        # 設定第一張圖片
                        if image_urls:
                            primary_embed.set_image(url=image_urls[0])

                        # 3. 組合 Footer 文字 (支援千分位格式化)
                        footer_parts = ["X", f"❤️ {likes:,}"]
                        if views is not None:
                            footer_parts.append(f"📷 {views:,}")

                        primary_embed.set_footer(text="  •  ".join(footer_parts))

                        embeds_to_send = [primary_embed]

                        # 支援第 2 至 4 張圖片的原生拼貼效果 (同 url 即會自動並排)
                        for extra_url in image_urls[1:4]:
                            extra_embed = discord.Embed(url=raw_x_url)
                            extra_embed.set_image(url=extra_url)
                            embeds_to_send.append(extra_embed)

                        # 發送自製 Embed 並隱藏原連結預覽
                        await message.channel.send(embeds=embeds_to_send)

                        # 登記這則訊息，交給 on_message_edit 負責後續補刀
                        pending_suppress_ids.add(message.id)

                        try:
                            await message.edit(suppress=True)
                        except Exception as e:
                            print(f"無法隱藏原始訊息預覽: {e}")

                else:
                    print(f"X API 請求失敗，狀態碼: {response.status_code}")

            except Exception as e:
                print(f"處理 X 網址時發生錯誤: {e}")

    # ================= 處理 PTT 網址 =================
    ptt_pattern = r'https?://(?:www\.)?ptt\.cc/bbs/[^/]+/[A-Za-z0-9\._]+\.html'
    ptt_match = re.search(ptt_pattern, message.content)
    # 原生 PTT 網址命中時
    if ptt_match:
        raw_ptt_url = ptt_match.group(0)
        await process_ptt_embed(
            target_ptt_url=raw_ptt_url,
            display_url=raw_ptt_url,
            message=message,
            source_name="PTT"
        )

    # ================= PTTWeb 網址處理 =================
    pttweb_pattern = r'https?://(?:www\.)?pttweb\.cc/(?:bbs/([^/]+)/([A-Za-z0-9\._]+)|s/([^/]+)/([A-Za-z0-9]+))'
    pttweb_match = re.search(pttweb_pattern, message.content)

    if pttweb_match:
        raw_pttweb_url = pttweb_match.group(0)
        target_ptt_url = None

        # 情況 A：標準網址 /bbs/{看板}/{文章ID}
        if pttweb_match.group(1) and pttweb_match.group(2):
            board = pttweb_match.group(1)
            article_id = pttweb_match.group(2)
            # 若末端已有 .html 則不重複補
            if not article_id.endswith('.html'):
                article_id += '.html'
            target_ptt_url = f"https://www.ptt.cc/bbs/{board}/{article_id}"

        # 情況 B：短網址 /s/{看板}/{短代碼}
        elif pttweb_match.group(3) and pttweb_match.group(4):
            try:
                # 向短網址發送請求，從其頁面內撈出真正的 ptt.cc 文章網址
                s_headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
                s_res = requests.get(raw_pttweb_url, headers=s_headers, timeout=5)
                if s_res.status_code == 200:
                    # 搜尋頁面中的 ptt.cc 文章連結
                    real_ptt_match = re.search(r'https?://www\.ptt\.cc/bbs/[^/]+/[A-Za-z0-9\._]+\.html',
                                               s_res.text)
                    if real_ptt_match:
                        target_ptt_url = real_ptt_match.group(0)
            except Exception as e:
                print(f"解析 PTTWeb 短網址時發生錯誤: {e}")

        # 若成功得到官方 target_ptt_url，交給共用函式解析；display_url 帶入使用者的原始 pttweb 連結
        if target_ptt_url:
            await process_ptt_embed(
                target_ptt_url=target_ptt_url,
                display_url=raw_pttweb_url,
                message=message,
                source_name="PTTWeb"
            )

    # ================= 處理巴哈姆特網址 (GNN / 哈啦版 / 小屋) =================
    if re.search(BAHA_PATTERN, message.content):
        match = re.search(BAHA_PATTERN, message.content)
        url = match.group(0)

        # 動態切換 Header，保護真實帳號安全
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

        if "home.gamer.com.tw" in url:
            # 小屋創作：從環境變數讀取真實 Cookie，若未設定則預設帶入 ckR18=1
            headers['Cookie'] = os.environ.get("BAHA_HOME_COOKIE", "ckR18=1;")
        else:
            # GNN/哈啦版：使用匿名預覽身分，避免頻繁請求導致本尊帳號受影響
            headers['Cookie'] = 'BAHAID=discord_bot_preview; ckR18=1;'

        try:
            res = requests.get(url, headers=headers)
            res.encoding = 'utf-8'
            soup = BeautifulSoup(res.text, 'html.parser')

            # ================= 1. GNN 新聞處理 =================
            if "gnn.gamer.com.tw" in url:
                section_name = "GNN新聞"
                title_tag = soup.find('h1')
                title = title_tag.text.strip() if title_tag else "GNN新聞"

                content_div = soup.find('div', class_='GN-lbox3B')
                target_html_block = str(content_div) if content_div else str(soup)
                raw_content_text = content_div.text if content_div else ""

                date_str = ""
                date_match = re.search(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', soup.text)
                if date_match:
                    try:
                        dt = datetime.strptime(date_match.group(0), "%Y-%m-%d %H:%M:%S")
                        date_str = dt.strftime("%Y/%m/%d %H:%M")
                    except Exception:
                        date_str = date_match.group(0)

                lines = [line.strip() for line in raw_content_text.splitlines() if line.strip()]
                clean_text = "\n".join(lines)
                if len(clean_text) > 150:
                    clean_text = clean_text[:150] + "..."

                block_soup = BeautifulSoup(target_html_block, 'html.parser')
                img_urls = []
                for img in block_soup.find_all('img'):
                    src = img.get('data-src') or img.get('src') or ""
                    if src and src.startswith('http') and 'emoji' not in src and '1x1.gif' not in src:
                        if src not in img_urls:
                            img_urls.append(src)

                embed = discord.Embed(
                    title=title,
                    url=url,
                    description=clean_text if clean_text else "無文字內容",
                    color=0x00B4D8
                )

                footer_text = f"巴哈姆特 • {section_name}"
                if date_str:
                    footer_text += f" • {date_str}"
                embed.set_footer(text=footer_text)

                if img_urls:
                    embed.set_image(url=img_urls[0])  # GNN只取第一張圖

                await message.channel.send(embed=embed)

                # 登記這則訊息，交給 on_message_edit 負責後續補刀
                pending_suppress_ids.add(message.id)

                try:
                    await message.edit(suppress=True)
                except Exception as e:
                    print(f"無法隱藏原始訊息預覽: {e}")

            # ================= 2. 哈啦版處理 =================
            elif "forum.gamer.com.tw" in url or "m.gamer.com.tw/forum" in url:
                # 若為手機版網址，自動轉換為 PC 版標準網址進行請求與解析
                if "m.gamer.com.tw/forum" in url:
                    url = url.replace("m.gamer.com.tw/forum", "forum.gamer.com.tw")

                res = requests.get(url, headers=headers)
                res.encoding = 'utf-8'
                soup = BeautifulSoup(res.text, 'html.parser')

                # 嘗試透過 data-gtm 定位，優先抓取 title 屬性
                board_tag = soup.find('a', attrs={'data-gtm': '選單-看板名稱'})
                if board_tag and board_tag.get('title'):
                    section_name = board_tag.get('title').strip()
                else:
                    # 絕對備用方案：從網頁 <title> 標籤提取 (格式通常為 "文章標題 @看板名稱 哈啦板 - 巴哈姆特")
                    page_title = soup.find('title').text if soup.find('title') else ""
                    board_match = re.search(r'@(.*?)\s+哈啦板', page_title)
                    section_name = board_match.group(1).strip() if board_match else "哈啦板"

                first_post = soup.find('section', class_='c-section')
                if not first_post:
                    return

                title_tag = first_post.find('h1', class_='c-post__header__title')
                title = title_tag.text.strip() if title_tag else "哈啦版文章"

                date_tag = first_post.find('a', class_='edittime')
                date_str = date_tag.text.strip() if date_tag else ""

                article_content = first_post.find('div', class_='c-article__content')
                target_html_block = str(article_content) if article_content else ""

                # 處理內文：利用 separator='\n' 解析排版，並壓縮連續空行
                if article_content:
                    raw_text = article_content.get_text(separator='\n').strip()
                    clean_text = re.sub(r'\n{2,}', '\n', raw_text)
                else:
                    clean_text = ""

                if len(clean_text) > 100:
                    clean_text = clean_text[:100] + "..."

                block_soup = BeautifulSoup(target_html_block, 'html.parser')

                # 過濾圖片與抓取
                img_urls = []
                for img in block_soup.find_all('img'):
                    src = img.get('data-src') or img.get('src') or ""
                    class_name = " ".join(img.get('class', [])).lower()

                    if 'smilie' in class_name or 'emoji' in class_name:
                        continue
                    if 'plugins/smiles' in src or 'forum/smiles' in src or 'editor/emotion' in src:
                        continue

                    if src and src.startswith('http') and '1x1.gif' not in src:
                        if src not in img_urls:
                            img_urls.append(src)

                preview_images = img_urls[:3]

                # 尋找 iframe 內的 YouTube 連結
                first_yt = None
                yt_iframe = block_soup.find('iframe', attrs={'src': re.compile(r'youtube\.com/embed/')})
                if not yt_iframe:
                    yt_iframe = block_soup.find('iframe', attrs={'data-src': re.compile(r'youtube\.com/embed/')})

                if yt_iframe:
                    yt_src = yt_iframe.get('src') or yt_iframe.get('data-src')
                    match = re.search(r'embed/([a-zA-Z0-9_-]+)', yt_src)
                    if match:
                        first_yt = f"https://www.youtube.com/watch?v={match.group(1)}"

                if not first_yt:
                    yt_urls = re.findall(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[a-zA-Z0-9_-]+',
                                         target_html_block)
                    if yt_urls:
                        first_yt = yt_urls[0]

                embeds = []
                main_embed = discord.Embed(
                    title=title,
                    url=url,
                    description=clean_text if clean_text else "無文字內容",
                    color=0x00B4D8
                )

                # 組合 Footer
                footer_text = f"巴哈姆特 • {section_name}"
                if date_str:
                    footer_text += f" • {date_str}"
                main_embed.set_footer(text=footer_text)

                if preview_images:
                    main_embed.set_image(url=preview_images[0])
                embeds.append(main_embed)

                for img_url in preview_images[1:]:
                    sub_embed = discord.Embed(url=url)
                    sub_embed.set_image(url=img_url)
                    embeds.append(sub_embed)

                if first_yt:
                    await message.channel.send(content=first_yt)

                await message.channel.send(embeds=embeds)

                # 登記這則訊息，交給 on_message_edit 負責後續補刀
                pending_suppress_ids.add(message.id)

                try:
                    await message.edit(suppress=True)
                except Exception as e:
                    print(f"無法隱藏原始訊息預覽: {e}")

            # ================= 3. 小屋創作處理 (Session 常駐 + 快取機制) =================
            elif "home.gamer.com.tw" in url:
                section_name = "小屋創作"

                # 1. 檢查是否命中記憶體快取 (命中則秒發，完全不消耗對外網路請求)
                if url in baha_artwork_cache:
                    cached_embeds = baha_artwork_cache[url]
                    await message.channel.send(embeds=cached_embeds)
                    pending_suppress_ids.add(message.id)
                    try:
                        await message.edit(suppress=True)
                    except Exception as e:
                        print(f"無法隱藏原始訊息預覽: {e}")
                    return

                # 2. 未命中快取：透過常駐 Session 發送請求 (自動繼承與輪轉 BAHARUNE)
                res = baha_client.get(url)
                res.encoding = 'utf-8'
                soup = BeautifulSoup(res.text, 'html.parser')

                # 防呆機制：檢查是否被擋在權限牆外 (找不到內文區塊)
                article_content = soup.find('div', id='article_content')

                if not article_content:
                    # 觸發隱私提示，不再往下解析
                    embed = discord.Embed(
                        title="🔒 限制級或私密內容",
                        url=url,
                        description="此小屋創作設有年齡限制、好友限定或已被刪除，請直接點擊標題前往網頁觀看。",
                        color=0x2C2F33
                    )
                    await message.channel.send(embed=embed)
                    pending_suppress_ids.add(message.id)
                    try:
                        await message.edit(suppress=True)
                    except Exception as e:
                        print(f"無法隱藏原始訊息預覽: {e}")
                    return

                # --- 正常解析流程 ---
                target_html_block = str(article_content)

                title_tag = soup.find('h1', class_='article-title')
                title = title_tag.text.strip() if title_tag else "小屋創作"

                # --- 新增：解析作者資訊 (名稱、個人首頁、頭像) ---
                author_name = ""
                author_url = None
                author_avatar = None

                # 1. 抓取作者名稱與小屋網址
                author_a_tag = soup.find('a', class_=re.compile(r'\bcaption-text\b.*\bprimary\b'))
                if not author_a_tag:
                    author_a_tag = soup.find('a', class_='caption-text primary')

                if author_a_tag:
                    author_name = author_a_tag.text.strip()
                    author_href = author_a_tag.get('href', '')
                    if author_href:
                        author_url = author_href if author_href.startswith(
                            'http') else f"https://home.gamer.com.tw/{author_href.lstrip('/')}"

                # 2. 抓取作者頭像圖片網址
                avatar_img_tag = soup.select_one('a.user-avatar-img img')
                if avatar_img_tag:
                    raw_avatar_src = avatar_img_tag.get('src') or avatar_img_tag.get('data-src') or ""
                    if raw_avatar_src.startswith('//'):
                        author_avatar = 'https:' + raw_avatar_src
                    elif raw_avatar_src.startswith('http'):
                        author_avatar = raw_avatar_src

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

                for br in article_content.find_all('br'):
                    br.replace_with('\n')
                for div in article_content.find_all(['div', 'p']):
                    div.append('\n')

                raw_text = article_content.get_text(separator='').strip()
                clean_text = re.sub(r'\n{2,}', '\n', raw_text)

                if len(clean_text) > 150:
                    clean_text = clean_text[:150]
                    clean_text = re.sub(r'\[[^\]]*$|\[[^\]]*\]\([^)]*$', '', clean_text).strip()
                    clean_text = re.sub(r'[-*]+$', '', clean_text).strip()
                    clean_text += "..."

                img_urls = []
                illustration_div = soup.find('div', id='div_illustration')
                if illustration_div:
                    for img in illustration_div.find_all('img'):
                        src = img.get('data-src') or img.get('src') or ""
                        if src and src.startswith('http'):
                            img_urls.append(src)

                block_soup = BeautifulSoup(target_html_block, 'html.parser')
                for img in block_soup.find_all('img'):
                    src = img.get('data-src') or img.get('src') or ""
                    class_name = " ".join(img.get('class', [])).lower()

                    if 'smilie' in class_name or 'emoji' in class_name:
                        continue
                    if 'plugins/smiles' in src or 'forum/smiles' in src or 'editor/emotion' in src:
                        continue

                    if src and src.startswith('http') and '1x1.gif' not in src:
                        if src not in img_urls:
                            img_urls.append(src)

                preview_images = img_urls[:3]

                embeds = []
                main_embed = discord.Embed(
                    title=title,
                    url=url,
                    description=clean_text if clean_text else "無文字內容",
                    color=0x00B4D8
                )

                # --- 新增：設定作者頂部標頭 (顯示頭像與名稱，點擊可前往小屋) ---
                if author_name:
                    main_embed.set_author(
                        name=author_name,
                        url=author_url,
                        icon_url=author_avatar
                    )

                footer_text = f"巴哈姆特 • 小屋創作"
                if date_str:
                    footer_text += f" • {date_str}"
                main_embed.set_footer(text=footer_text)

                if preview_images:
                    main_embed.set_image(url=preview_images[0])
                embeds.append(main_embed)

                for img_url in preview_images[1:]:
                    sub_embed = discord.Embed(url=url)
                    sub_embed.set_image(url=img_url)
                    embeds.append(sub_embed)

                await message.channel.send(embeds=embeds)
                baha_artwork_cache[url] = embeds

                pending_suppress_ids.add(message.id)
                try:
                    await message.edit(suppress=True)
                except Exception as e:
                    print(f"無法隱藏原始訊息預覽: {e}")

        except Exception as e:
            print(f"處理 巴哈姆特 網址時發生錯誤: {e}")


    # ================= 處理 4Gamers 網址 =================
    if re.search(FOURGAMERS_PATTERN, message.content):
        fg_match = re.search(FOURGAMERS_PATTERN, message.content)
        if fg_match:
            raw_fg_url = fg_match.group(0)

            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }

            try:
                res = requests.get(raw_fg_url, headers=headers)
                res.encoding = 'utf-8'
                soup = BeautifulSoup(res.text, 'html.parser')

                # 1. 解析標題
                title_tag = soup.find('h1')
                title = title_tag.text.strip() if title_tag else "4Gamers 新聞"

                # 2. 解析標籤 (鎖定原始 HTML 中的分類超連結，不添加 # 符號)
                tag_text = ""

                # 尋找 href 屬性包含 /news/category/ 的 <a> 標籤
                category_a_tag = soup.find('a', href=re.compile(r'/news/category/'))

                if category_a_tag and category_a_tag.text.strip():
                    # 直接取得純文字，並利用 lstrip('#') 防呆移除開頭可能自帶的井號
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

                # 3. 解析發文時間
                date_str = ""
                time_tag = soup.find('time')
                if time_tag:
                    raw_dt = time_tag.get('datetime', '')
                    if raw_dt:
                        try:
                            clean_iso = re.sub(r'\.\d+Z$', 'Z', raw_dt)
                            dt = datetime.fromisoformat(clean_iso.replace('Z', '+00:00'))
                            date_str = dt.strftime("%Y/%m/%d %H:%M")
                        except Exception:
                            date_str = time_tag.text.strip()
                    else:
                        date_str = time_tag.text.strip()

                # 4. 解析內文 (清除空標籤與壓縮行距)
                clean_text = ""
                content_div = soup.find('div', attrs={'data-news-content': True})
                if not content_div:
                    content_div = soup.find('article')

                if content_div:
                    # 複製一份獨立解析，避免破壞原始結構
                    c_soup = BeautifulSoup(str(content_div), 'html.parser')

                    # 移除所有無實質文字的空段落或純空白 (&nbsp;)
                    for p in c_soup.find_all('p'):
                        if not p.text.replace('\xa0', '').strip():
                            p.decompose()

                    # 逐行清洗空行
                    raw_lines = c_soup.get_text(separator='\n').splitlines()
                    clean_lines = [line.strip() for line in raw_lines if line.strip()]
                    clean_text = "\n".join(clean_lines)

                    # 150 字安全截斷
                    if len(clean_text) > 150:
                        clean_text = clean_text[:150]
                        clean_text = re.sub(r'\[[^\]]*$|\[[^\]]*\]\([^)]*$', '', clean_text).strip()
                        clean_text = re.sub(r'[-*]+$', '', clean_text).strip()
                        clean_text += "..."

                # 5. 解析封面圖片 (只取第一張)
                cover_img = None
                picture_tag = soup.find('picture')
                if picture_tag:
                    img_tag = picture_tag.find('img')
                    if img_tag:
                        cover_img = img_tag.get('src') or img_tag.get('data-src')

                if not cover_img:
                    og_img = soup.find('meta', property='og:image')
                    if og_img and og_img.get('content'):
                        cover_img = og_img.get('content')

                # 6. 組裝 Discord Embed (改為深藍色 0x0E2338)
                embed = discord.Embed(
                    title=title,
                    url=raw_fg_url,
                    description=clean_text if clean_text else "無文字內容",
                    color=0x0E2338
                )

                if cover_img and cover_img.startswith('http'):
                    embed.set_image(url=cover_img)

                # 組裝 Footer
                footer_parts = ["4Gamers"]
                if tag_text:
                    footer_parts.append(tag_text)
                if date_str:
                    footer_parts.append(date_str)
                embed.set_footer(text=" • ".join(footer_parts))

                await message.channel.send(embed=embed)

                try:
                    await message.edit(suppress=True)
                except Exception as e:
                    print(f"無法隱藏原始訊息預覽: {e}")

            except Exception as e:
                print(f"處理 4Gamers 網址時發生錯誤: {e}")

    # ================= 處理《勝利女神：妮姬》台灣官網新聞 =================
    nikke_match = re.search(NIKKE_PATTERN, message.content)
    if nikke_match:
        raw_nikke_url = nikke_match.group(0)
        await process_nikke_embed(raw_nikke_url, message, pending_suppress_ids)

    # ================= 處理 Hanime1 網址 =================
    if re.search(HANIME_PATTERN, message.content):
        hanime_match = re.search(HANIME_PATTERN, message.content)
        if hanime_match:
            target_url = hanime_match.group(0)
            await process_hanime_embed(target_url, message, pending_suppress_ids)

    # ================= 處理 Pixiv 網址 =================
    pixiv_match = re.search(PIXIV_PATTERN, message.content)
    if pixiv_match:
        # 從捕獲群組中提取純數字 illust_id (群組 2 或群組 3)
        illust_id = pixiv_match.group(2) or pixiv_match.group(3)
        original_url = f"https://www.pixiv.net/artworks/{illust_id}"
        await process_pixiv_embed(illust_id, original_url, message, pending_suppress_ids)

    # ================= Gamekee 棕色塵埃2 預覽處理 =================
    gk_match = re.search(GAMEKEE_BD2_PATTERN, message.content)
    if gk_match:
        original_gk_url = gk_match.group(0)
        content_id = gk_match.group(1)
        await process_gamekee_bd2_embed(
            content_id=content_id,
            original_url=original_gk_url,
            message=message,
            pending_suppress_ids=pending_suppress_ids
        )

# 啟動 Bot，請將引號內替換為你的 Token
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
client.run(DISCORD_TOKEN)