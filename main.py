import os
import re
import discord
from dotenv import load_dotenv

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
load_dotenv()

# 設定 Intents 以便讀取訊息內容
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# 追蹤待壓抑原始預覽的訊息 ID (由 on_message_edit 補刀處理延遲生成的 Embed)
pending_suppress_ids = set()

# ================= 網址匹配正規表達式 =================
WNACG_PATTERN = r"(https?://(www\.)?wnacg\.com/photos-index-aid-\d+\.html)"
BILIBILI_PATTERN = r"(https?://(www\.|m\.)?(bilibili\.com|b23\.tv)/[^\s]+)"
FACEBOOK_PATTERN = r"(https?://(www\.|m\.|web\.)?(facebook\.com|fb\.watch)/[^\s]+)"
THREADS_PATTERN = r"(https?://(www\.)?threads\.(net|com)/[^\s]+)"
INSTAGRAM_PATTERN = r"(https?://(www\.)?instagram\.com/[^\s]+)"
X_PATTERN = r"(https?://(www\.)?(x|twitter)\.com/[^\s]+/status/\d+)"
PTT_PATTERN = r"https?://(?:www\.)?ptt\.cc/bbs/[^/]+/[A-Za-z0-9\._]+\.html"
PTTWEB_PATTERN = r"https?://(?:www\.)?pttweb\.cc/(?:bbs/([^/]+)/([A-Za-z0-9\._]+)|s/([^/]+)/([A-Za-z0-9]+))"
BAHA_PATTERN = r"(https?://(?:(gnn|forum|home)\.gamer\.com\.tw|m\.gamer\.com\.tw/forum)/[^\s]+)"
FOURGAMERS_PATTERN = r"(https?://(www\.)?4gamers\.com\.tw/news/detail/\d+/[^\s]+)"
NIKKE_PATTERN = r"(https?://nikke\.hotcool\.tw/(?:m/)?News_detail-\d+)"
HANIME_PATTERN = r"(https?://hanime1\.me/watch\?v=\d+)"
PIXIV_PATTERN = r"(https?://(?:www\.)?pixiv\.net/(?:(?:en/)?artworks/|member_illust\.php\?illust_id=)(\d+)|https?://pixiv\.net/i/(\d+))"
GAMEKEE_BD2_PATTERN = r"https?://(?:www\.)?gamekee\.com/zsca2/(\d+)\.html"


@client.event
async def on_ready():
    print(f'Bot 已成功登入為 {client.user}')


@client.event
async def on_message_edit(before, after):
    # 忽略 Bot 自己的編輯事件
    if after.author.bot:
        return

    # 處理待補刀名單中的訊息
    if after.id in pending_suppress_ids:
        if after.embeds and not after.flags.suppress_embeds:
            try:
                await after.edit(suppress=True)
            except Exception as e:
                print(f"on_message_edit 隱藏預覽失敗: {e}")
            finally:
                pending_suppress_ids.discard(after.id)


@client.event
async def on_message(message):
    # 避免 Bot 回應自己的訊息
    if message.author == client.user:
        return

    # ================= 處理 WNACG 網址 =================
    if wnacg_match := re.search(WNACG_PATTERN, message.content):
        await process_wnacg_embed(wnacg_match.group(0), message, pending_suppress_ids)

    # ================= 處理 Bilibili 網址 =================
    if not re.search(r"vx(bilibili|b23)", message.content):
        if bili_match := re.search(BILIBILI_PATTERN, message.content):
            await process_bilibili_embed(bili_match.group(0), message, pending_suppress_ids)

    # ================= 處理 Facebook 網址 =================
    if "facebed.com" not in message.content:
        if fb_match := re.search(FACEBOOK_PATTERN, message.content):
            await process_facebook_embed(fb_match.group(0), message, pending_suppress_ids)

    # ================= 處理 Threads 網址 =================
    if not re.search(r"(fzthreads\.com|fixthreads\.seria\.moe)", message.content):
        if threads_match := re.search(THREADS_PATTERN, message.content):
            await process_threads_embed(threads_match.group(0), message, pending_suppress_ids)

    # ================= 處理 Instagram 網址 =================
    if not re.search(r"(og|hh|kk)instagram\.com", message.content):
        if ig_match := re.search(INSTAGRAM_PATTERN, message.content):
            await process_instagram_embed(ig_match.group(0), message, pending_suppress_ids)

    # ================= 處理 X / Twitter 網址 =================
    if not re.search(r"(vx|fx|fixupx|fixvx)(x|twitter)\.com", message.content):
        if x_match := re.search(X_PATTERN, message.content):
            await process_x_embed(x_match.group(0), message, pending_suppress_ids)

    # ================= 處理 PTT 網址 =================
    if ptt_match := re.search(PTT_PATTERN, message.content):
        raw_ptt_url = ptt_match.group(0)
        await process_ptt_embed(
            target_ptt_url=raw_ptt_url,
            display_url=raw_ptt_url,
            message=message,
            source_name="PTT",
            pending_suppress_ids=pending_suppress_ids
        )

    # ================= 處理 PTTWeb 網址 =================
    if pttweb_match := re.search(PTTWEB_PATTERN, message.content):
        await handle_pttweb_url(pttweb_match.group(0), pttweb_match, message, pending_suppress_ids)

    # ================= 處理巴哈姆特網址 (GNN / 哈啦板 / 小屋) =================
    if baha_match := re.search(BAHA_PATTERN, message.content):
        await process_bahamut_embed(baha_match.group(0), message, pending_suppress_ids)

    # ================= 處理 4Gamers 新聞 =================
    if fg_match := re.search(FOURGAMERS_PATTERN, message.content):
        await process_4gamers_embed(fg_match.group(0), message, pending_suppress_ids)

    # ================= 處理《勝利女神：妮姬》台灣官網新聞 =================
    if nikke_match := re.search(NIKKE_PATTERN, message.content):
        await process_nikke_embed(nikke_match.group(0), message, pending_suppress_ids)

    # ================= 處理 Hanime1 網址 =================
    if hanime_match := re.search(HANIME_PATTERN, message.content):
        await process_hanime_embed(hanime_match.group(0), message, pending_suppress_ids)

    # ================= 處理 Pixiv 網址 =================
    if pixiv_match := re.search(PIXIV_PATTERN, message.content):
        illust_id = pixiv_match.group(2) or pixiv_match.group(3)
        original_url = f"https://www.pixiv.net/artworks/{illust_id}"
        await process_pixiv_embed(illust_id, original_url, message, pending_suppress_ids)

    # ================= Gamekee 棕色塵埃2 預覽處理 =================
    if gk_match := re.search(GAMEKEE_BD2_PATTERN, message.content):
        await process_gamekee_bd2_embed(
            content_id=gk_match.group(1),
            original_url=gk_match.group(0),
            message=message,
            pending_suppress_ids=pending_suppress_ids
        )


# 啟動 Bot
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
client.run(DISCORD_TOKEN)