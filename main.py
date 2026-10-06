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
from services.threads import process_threads_embed
from services.facebook import process_facebook_embed
from services.bilibili import process_bilibili_embed
from services.wnacg import process_wnacg_embed

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


# 用正規表達式比對 wnacg 的網址
WNACG_PATTERN = r"(https?://(www\.)?wnacg\.com/photos-index-aid-\d+\.html)"
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

    # ================= 處理 WNACG 網址 =================
    wnacg_match = re.search(WNACG_PATTERN, message.content)
    if wnacg_match:
        target_url = wnacg_match.group(0)
        await process_wnacg_embed(target_url, message, pending_suppress_ids)

    # ================= 處理 Bilibili 網址 =================
    if re.search(BILIBILI_PATTERN, message.content) and not re.search(r"vx(bilibili|b23)", message.content):
        bili_match = re.search(BILIBILI_PATTERN, message.content)
        if bili_match:
            raw_bili_url = bili_match.group(0)
            await process_bilibili_embed(raw_bili_url, message, pending_suppress_ids)

    # ================= 處理 Facebook 網址 =================
    if re.search(FACEBOOK_PATTERN, message.content) and "facebed.com" not in message.content:
        fb_match = re.search(FACEBOOK_PATTERN, message.content)
        if fb_match:
            raw_fb_url = fb_match.group(0)
            await process_facebook_embed(raw_fb_url, message, pending_suppress_ids)

    # ================= 處理 Threads 網址 =================
    if re.search(THREADS_PATTERN, message.content) and not re.search(r"(fzthreads\.com|fixthreads\.seria\.moe)",
                                                                     message.content):
        threads_match = re.search(THREADS_PATTERN, message.content)
        if threads_match:
            raw_threads_url = threads_match.group(0)
            await process_threads_embed(raw_threads_url, message, pending_suppress_ids)

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