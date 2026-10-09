import os
import re
import asyncio
import tempfile
import urllib.parse
import discord
from curl_cffi import requests
from bs4 import BeautifulSoup
from yt_dlp import YoutubeDL
from cachetools import TTLCache

# ================= 參數與伺服器設定 =================
WATCHDOG_CHECK_DELAY = 8.0
CUSTOM_WORKER_DOMAIN = "zusakvi.cc"

fb_cache = TTLCache(maxsize=150, ttl=3600)

REQUEST_HEADERS = {
    'User-Agent': 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)',
    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7'
}

FB_COOKIE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'fb_cookies.txt')


def _resolve_fb_url_sync(raw_fb_url: str) -> str:
    """同步還原 Facebook /share/ 短跳轉網址至真實路徑"""
    target_url = raw_fb_url
    if "/share/" not in raw_fb_url:
        return target_url.split('?')[0]

    try:
        head_res = requests.head(raw_fb_url, headers=REQUEST_HEADERS, allow_redirects=False, timeout=5)
        location = head_res.headers.get('Location', '')

        fbid_match = re.search(r'story_fbid=(\d+)', location)
        if fbid_match:
            return f"https://www.facebook.com/reel/{fbid_match.group(1)}"
        elif location and "/share/" not in location and "/login" not in location:
            return location.split('?')[0]

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


def _extract_fb_post_sync(target_fb_url: str) -> dict | None:
    """
    爬取普通貼文（個人貼文、社團貼文、轉發貼文）：
    1. 智能識別社團名 / 作者
    2. 穿透轉發提取被轉發原文案 (50 字限制)
    3. 解除斜線隱蔽，精準狙擊 __isMedia: Photo 的真實相片 ID (最多 3 張)
    """
    try:
        clean_url = target_fb_url.split('?')[0]
        mobile_url = re.sub(r'https?://(?:www\.)?facebook\.com', 'https://m.facebook.com', clean_url)

        res = requests.get(
            mobile_url,
            headers=REQUEST_HEADERS,
            impersonate="chrome120",
            allow_redirects=True,
            timeout=10
        )

        if res.status_code != 200 or "/login" in res.url:
            return None

        html_text = res.text
        # 解除 Facebook 對網址與 JSON 斜線的跳脫偽裝
        unescaped_html = html_text.replace(r'\/', '/').replace('\\/', '/')
        soup = BeautifulSoup(html_text, 'html.parser')

        # 1. 標題與社團/作者名稱萃取
        raw_og_title = ""
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            raw_og_title = og_title['content'].strip()

        raw_page_title = soup.title.get_text().strip() if soup.title else ""
        is_group_post = "/groups/" in target_fb_url or "groups" in mobile_url

        clean_title = ""
        if is_group_post:
            if raw_page_title:
                t_parts = raw_page_title.split("|")
                candidate_group = t_parts[0].strip()
                if candidate_group.lower() != "facebook" and len(candidate_group) > 0:
                    clean_title = candidate_group

            if (not clean_title or clean_title == "Facebook") and raw_og_title and "|" in raw_og_title:
                clean_title = raw_og_title.split("|")[0].strip()

            if not clean_title or clean_title == "Facebook":
                group_link = soup.find('a', href=re.compile(r'/groups/\d+/'))
                if group_link and group_link.get_text().strip():
                    clean_title = group_link.get_text().strip()

        if not clean_title:
            clean_title = raw_og_title or raw_page_title

        clean_title = re.sub(r'\s*[|\-]\s*Facebook$', '', clean_title, flags=re.I).strip()
        clean_title = re.sub(r'\s*[|\-]\s*(?:貼文|Post)$', '', clean_title, flags=re.I).strip()
        if not clean_title:
            clean_title = "Facebook 社團" if is_group_post else "Facebook 貼文"

        # 2. 內文萃取
        main_story = soup.find('div', id='m_story_permalink_view') or soup.find('div', role='main') or soup

        # 移除留言區
        for comment_elem in main_story.find_all(['div', 'footer', 'section'],
                                                class_=re.compile(r'ufi|comment|feedback', re.I)):
            comment_elem.decompose()

        raw_desc = ""
        attached_story = main_story.find('div', class_=re.compile(r'attached_story|story_body_container', re.I))
        if attached_story:
            raw_desc = attached_story.get_text(separator='\n').strip()

        if not raw_desc:
            og_desc = soup.find('meta', property='og:description')
            if og_desc and og_desc.get('content'):
                raw_desc = og_desc['content'].strip()

        clean_lines = [line.strip() for line in raw_desc.splitlines() if line.strip()]
        clean_lines = [l for l in clean_lines if not re.match(r'^(?:加入|已加入|公開社團|私人社團|\d+位成員)$', l)]
        compact_desc = "\n".join(clean_lines)

        if len(compact_desc) > 50:
            compact_desc = compact_desc[:50] + "..."

        # 3. 圖片提取（核心狙擊：鎖定 __isMedia: Photo 真實相片）
        image_urls = []
        found_photo_ids = []

        # (A) 優先在解除斜線後的內容中，抓取所有明確被標記為 Photo 的 ID
        # 格式範例："id":"27978767405134914","__isMedia":"Photo"
        media_photo_matches = re.findall(r'"id":"(\d{14,18})","__isMedia":"Photo"', unescaped_html)
        for pid in media_photo_matches:
            if pid not in found_photo_ids:
                found_photo_ids.append(pid)

        # (B) 備用：比對帶有 /photo/?fbid= 或 photo.php?fbid= 的超連結
        if len(found_photo_ids) < 3:
            link_matches = re.findall(r'(?:/photo/?\?fbid=|photo\.php\?fbid=)(\d{14,18})', unescaped_html)
            for pid in link_matches:
                if pid not in found_photo_ids:
                    found_photo_ids.append(pid)

        # 將找到的相片 ID 轉換為 lookaside 官方跳轉網址
        if found_photo_ids:
            for pid in found_photo_ids:
                lookaside_url = f"https://lookaside.fbsbx.com/lookaside/crawler/media/?media_id={pid}"
                if lookaside_url not in image_urls:
                    image_urls.append(lookaside_url)
                if len(image_urls) >= 3:
                    break

        # (C) 備用防線：若完全無 ID，抓取包含 -6/ 的貼文真實 CDN
        if len(image_urls) < 3:
            raw_cdn_matches = re.findall(r'(https:[^"\'\s<>\\]+?fbcdn\.net/[^"\'\s<>\\]+?-[68]/[^"\'\s<>\\]+)',
                                         unescaped_html)
            for raw_img in raw_cdn_matches:
                clean_img = raw_img.replace('&amp;', '&')
                if any(k in clean_img for k in
                       ["s32x32", "s100x100", "p50x50", "p100x100", "emoji", "static", "rsrc.php"]):
                    continue
                if clean_img not in image_urls:
                    image_urls.append(clean_img)
                if len(image_urls) >= 3:
                    break

        # (D) 兜底：非社團貼文且完全無圖時，才允許退回使用 og:image
        if not image_urls and not is_group_post:
            og_img = soup.find('meta', property='og:image')
            if og_img and og_img.get('content'):
                img_src = og_img['content'].replace('&amp;', '&')
                if "fb_icon" not in img_src and "facebook.com/images" not in img_src:
                    image_urls.append(img_src)

        return {
            "title": clean_title,
            "description": compact_desc,
            "images": image_urls
        }

    except Exception:
        return None


def _download_fb_video_ytdlp(fb_url: str) -> dict | None:
    """yt-dlp 備援下載（年齡限制/私密影片兜底）"""
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
                        'title': info.get('title') or "Facebook 限制級內容"
                    }
        except Exception:
            continue

    return None


async def _watchdog_worker_embed(fix_msg: discord.Message, target_fb_url: str, raw_fb_url: str):
    """背景看門狗：若 Worker 逾時未讓 Discord 生成卡片，自動啟動 yt-dlp 救援"""
    await asyncio.sleep(WATCHDOG_CHECK_DELAY)

    try:
        channel = fix_msg.channel
        refreshed_msg = await channel.fetch_message(fix_msg.id)

        if refreshed_msg.embeds:
            fb_cache[raw_fb_url] = {"type": "worker"}
            return

        try:
            await refreshed_msg.delete()
        except Exception:
            pass

        status_msg = await channel.send("⏳ **偵測到預覽生成逾時或受限**，正在為您直接擷取檔案，請稍候...")

        ytdl_result = await asyncio.to_thread(_download_fb_video_ytdlp, target_fb_url)

        if ytdl_result and ytdl_result.get('file_path') and os.path.exists(ytdl_result['file_path']):
            file_path = ytdl_result['file_path']
            try:
                discord_file = discord.File(file_path, filename="facebook_reel.mp4")
                await channel.send(
                    content="已為您直接擷取影片檔案：",
                    file=discord_file
                )
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                fb_cache[raw_fb_url] = {"type": "downloaded"}
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)
            return

        embed = discord.Embed(
            title="🔒 此 Facebook 內容含有年齡限制或私密設定",
            description=f"無法獲取公開媒體，請直接點擊連結查看：\n[前往 Facebook 觀看]({target_fb_url})",
            color=0x1877F2
        )
        await status_msg.edit(content=None, embed=embed)

    except discord.NotFound:
        pass
    except Exception:
        pass


async def process_facebook_embed(raw_fb_url: str, message: discord.Message, pending_suppress_ids: set):
    """
    Facebook 預覽主入口
    """
    pending_suppress_ids.add(message.id)
    try:
        await message.edit(suppress=True)
    except Exception:
        pass

    target_fb_url = await asyncio.to_thread(_resolve_fb_url_sync, raw_fb_url)

    is_video_content = any(k in target_fb_url for k in ["/reel/", "/videos/", "/watch"]) or \
                       any(k in raw_fb_url for k in ["/share/r/", "/share/v/"])

    if not is_video_content:
        post_data = await asyncio.to_thread(_extract_fb_post_sync, target_fb_url)

        if post_data:
            embeds = []
            main_embed = discord.Embed(
                title=post_data["title"],
                url=target_fb_url,
                description=post_data["description"] if post_data["description"] else None,
                color=0x1877F2
            )
            if post_data.get("images"):
                main_embed.set_image(url=post_data["images"][0])
            embeds.append(main_embed)

            if post_data.get("images") and len(post_data["images"]) > 1:
                for extra_img in post_data["images"][1:3]:
                    extra_embed = discord.Embed(url=target_fb_url)
                    extra_embed.set_image(url=extra_img)
                    embeds.append(extra_embed)

            await message.channel.send(embeds=embeds)
        return

    fix_fb_url = re.sub(r"(https?://)(?:www\.)?(?:facebook\.com|fb\.watch)", rf"\1{CUSTOM_WORKER_DOMAIN}",
                        target_fb_url)

    try:
        fix_msg = await message.channel.send(f"[⠀]({fix_fb_url})")
    except Exception:
        return

    asyncio.create_task(_watchdog_worker_embed(fix_msg, target_fb_url, raw_fb_url))