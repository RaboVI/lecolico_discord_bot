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
from services.four_gamers import process_4gamers_embed
from services.bahamut import process_bahamut_embed
from services.ptt import process_ptt_embed, handle_pttweb_url
from services.twitter_x import process_x_embed
from services.instagram import process_instagram_embed

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
# 匹配 PTT & PTTweb 網址
PTT_PATTERN = r"https?://(?:www\.)?ptt\.cc/bbs/[^/]+/[A-Za-z0-9\._]+\.html"
PTTWEB_PATTERN = r"https?://(?:www\.)?pttweb\.cc/(?:bbs/([^/]+)/([A-Za-z0-9\._]+)|s/([^/]+)/([A-Za-z0-9]+))"


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

    # ================= 處理 Instagram 網址 =================
    if re.search(INSTAGRAM_PATTERN, message.content) and not re.search(r"(og|hh|kk)instagram\.com",
                                                                       message.content):
        ig_match = re.search(INSTAGRAM_PATTERN, message.content)
        if ig_match:
            raw_ig_url = ig_match.group(0)
            await process_instagram_embed(raw_ig_url, message, pending_suppress_ids)

    # ================= 處理 X / Twitter 網址 =================
    if re.search(X_PATTERN, message.content) and not re.search(r"(vx|fx|fixupx|fixvx)(x|twitter)\.com",
                                                               message.content):
        x_match = re.search(X_PATTERN, message.content)
        if x_match:
            raw_x_url = x_match.group(0)
            await process_x_embed(raw_x_url, message, pending_suppress_ids)

    # ================= 處理 PTT 網址 =================
    ptt_match = re.search(PTT_PATTERN, message.content)
    if ptt_match:
        raw_ptt_url = ptt_match.group(0)
        await process_ptt_embed(
            target_ptt_url=raw_ptt_url,
            display_url=raw_ptt_url,
            message=message,
            source_name="PTT",
            pending_suppress_ids=pending_suppress_ids
        )

    # ================= 處理 PTTWeb 網址 =================
    pttweb_match = re.search(PTTWEB_PATTERN, message.content)
    if pttweb_match:
        raw_pttweb_url = pttweb_match.group(0)
        await handle_pttweb_url(raw_pttweb_url, pttweb_match, message, pending_suppress_ids)

    # ================= 處理巴哈姆特網址 (GNN / 哈啦版 / 小屋) =================
    baha_match = re.search(BAHA_PATTERN, message.content)
    if baha_match:
        raw_baha_url = baha_match.group(0)
        await process_bahamut_embed(raw_baha_url, message, pending_suppress_ids)

    # ================= 處理 4Gamers 新聞 =================
    fg_match = re.search(FOURGAMERS_PATTERN, message.content)
    if fg_match:
        raw_fg_url = fg_match.group(0)
        await process_4gamers_embed(raw_fg_url, message, pending_suppress_ids)

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