import os
import re
import asyncio
import tempfile
import urllib.parse
import discord
from curl_cffi import requests
from yt_dlp import YoutubeDL
from cachetools import TTLCache

# 建立記憶體快取 (最多 100 筆，快取有效時間 1 小時 = 3600 秒)
fb_cache = TTLCache(maxsize=100, ttl=3600)

REQUEST_HEADERS = {
    'User-Agent': 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)',
    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7'
}

# 定義 Cookie 檔案路徑
FB_COOKIE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'fb_cookies.txt')

# 若本機無檔案但 Railway 環境變數有配置，自動在執行期寫入實體檔案供 yt-dlp 讀取
if not os.path.exists(FB_COOKIE_PATH):
    env_cookies = os.environ.get("FB_COOKIES_TEXT")
    if env_cookies:
        try:
            with open(FB_COOKIE_PATH, "w", encoding="utf-8") as f:
                f.write(env_cookies)
        except Exception as e:
            print(f"[Facebook] 無法從環境變數寫入 fb_cookies.txt: {e}")


def _resolve_fb_url_sync(raw_fb_url: str) -> str:
    """
    同步還原 Facebook /share/ 短跳轉至真實的 /reel/ 或 /posts/ 網址
    """
    target_url = raw_fb_url
    if "/share/" not in raw_fb_url:
        return target_url.split('?')[0]

    try:
        # 1. 優先透過 HEAD 請求截取 Location 標頭
        head_res = requests.head(raw_fb_url, headers=REQUEST_HEADERS, allow_redirects=False, timeout=5)
        location = head_res.headers.get('Location', '')

        fbid_match = re.search(r'story_fbid=(\d+)', location)
        if fbid_match:
            return f"https://www.facebook.com/reel/{fbid_match.group(1)}"
        elif location and "/share/" not in location and "/login" not in location:
            return location.split('?')[0]

        # 2. 降級至 GET 深度提取
        res = requests.get(raw_fb_url, headers=REQUEST_HEADERS, allow_redirects=True, timeout=8)
        final_url = res.url

        if "/login" in final_url or "login.php" in final_url:
            fbid_in_login = re.search(r'(?:story_fbid%3D|video_id%3D)(\d+)', final_url)
            if fbid_in_login:
                return f"https://www.facebook.com/reel/{fbid_in_login.group(1)}"
            match_next = re.search(r'[?&](?:next|u)=([^&]+)', final_url)
            if match_next:
                decoded = urllib.parse.unquote(match_next.group(1))
                if "/share/" not in decoded and "/login" not in decoded:
                    return decoded.split('?')[0]
        elif "/share/" not in final_url:
            return final_url.split('?')[0]

    except Exception:
        pass

    return target_url.split('?')[0]


def _probe_facebed_sync(fix_fb_url: str) -> bool:
    """
    探測 facebed 狀態
    """
    try:
        res = requests.get(
            fix_fb_url,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; Discordbot/2.0; +https://discord.app)'},
            allow_redirects=True,
            timeout=8
        )

        # 1. 若被導向至 Facebook 登入頁，代表不可用
        if "/login" in res.url or "login.php" in res.url:
            return False

        # 2. 檢查是否包含有效的 Open Graph 媒體標籤
        if res.status_code == 200:
            if 'property="og:video"' in res.text or 'name="twitter:player"' in res.text or 'property="og:image"' in res.text:
                return True
            if "Log in or sign up to view" not in res.text and "checkpoint" not in res.text:
                return True

        return False
    except Exception:
        return False


def _download_fb_video_ytdlp(fb_url: str) -> dict | None:
    """
    使用 yt-dlp 掛載成年小號 Cookie 下載影片 (含雙路徑備援)
    """
    temp_dir = tempfile.gettempdir()
    out_template = os.path.join(temp_dir, 'fb_%(id)s.%(ext)s')

    id_match = re.search(r'/(?:reel|videos)/(\d+)', fb_url)
    reel_id = id_match.group(1) if id_match else None

    target_urls = [fb_url]
    if reel_id:
        target_urls.append(f"https://www.facebook.com/watch/?v={reel_id}")

    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'outtmpl': out_template,
        'quiet': True,
        'no_warnings': True,
        'max_filesize': 24 * 1024 * 1024,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
        }
    }

    if os.path.exists(FB_COOKIE_PATH):
        ydl_opts['cookiefile'] = FB_COOKIE_PATH

    for try_url in target_urls:
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(try_url, download=True)
                filename = ydl.prepare_filename(info)
                if not os.path.exists(filename):
                    base_name, _ = os.path.splitext(filename)
                    if os.path.exists(f"{base_name}.mp4"):
                        filename = f"{base_name}.mp4"

                if os.path.exists(filename):
                    return {
                        'file_path': filename,
                        'title': info.get('title') or "Facebook 限制級影片",
                        'direct_url': info.get('url')
                    }
        except Exception:
            continue

    return None


async def process_facebook_embed(raw_fb_url: str, message: discord.Message, pending_suppress_ids: set):
    """
    Facebook 雙軌預覽處理流程 (掛載 TTL 快取與即時狀態反饋)
    """
    # 檢查是否已在快取中
    if raw_fb_url in fb_cache:
        cached_result = fb_cache[raw_fb_url]
        if cached_result.get("type") == "facebed":
            await message.channel.send(f"[⠀]({cached_result['url']})")
            pending_suppress_ids.add(message.id)
            try:
                await message.edit(suppress=True)
            except Exception:
                pass
            return

    # 步驟 1：還原真實網址
    target_fb_url = await asyncio.to_thread(_resolve_fb_url_sync, raw_fb_url)
    fix_fb_url = re.sub(r"(facebook\.com|fb\.watch)", "facebed.com", target_fb_url)

    # 步驟 2：探測 facebed
    is_facebed_ok = await asyncio.to_thread(_probe_facebed_sync, fix_fb_url)

    if is_facebed_ok:
        fb_cache[raw_fb_url] = {"type": "facebed", "url": fix_fb_url}
        await message.channel.send(f"[⠀]({fix_fb_url})")
        pending_suppress_ids.add(message.id)
        try:
            await message.edit(suppress=True)
        except Exception:
            pass
        return

    # 步驟 3：facebed 失敗，轉交 yt-dlp 救援
    # (A) 立即隱藏原訊息預覽
    pending_suppress_ids.add(message.id)
    try:
        await message.edit(suppress=True)
    except Exception:
        pass

    # (B) 搶先發送「處理中」占位訊息
    status_msg = await message.channel.send("⏳ **此內容設有年齡或登入限制**，正在為您擷取影片檔案，請稍候...")

    # (C) 背景執行 yt-dlp 下載
    ytdl_result = await asyncio.to_thread(_download_fb_video_ytdlp, target_fb_url)

    if ytdl_result and ytdl_result.get('file_path') and os.path.exists(ytdl_result['file_path']):
        file_path = ytdl_result['file_path']
        try:
            discord_file = discord.File(file_path, filename="facebook_reel.mp4")

            # 先將檔案送至頻道
            await message.channel.send(
                content=f"已為您直接擷取影片檔案：",
                file=discord_file
            )

            # 確定新訊息成功出現後，再刪除占位訊息
            try:
                await status_msg.delete()
            except Exception:
                pass

            # 寫入快取
            fb_cache[raw_fb_url] = {"type": "downloaded"}
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
        return

    # 步驟 4：兜底防呆卡片
    embed = discord.Embed(
        title="🔒 此 Facebook 內容含有年齡限制或私密設定",
        description=f"代理服務於未登入狀態下無法載入，請直接點擊連結登入查看：\n[前往 Facebook 觀看]({target_fb_url})",
        color=0x1877F2
    )
    await status_msg.edit(content=None, embed=embed)