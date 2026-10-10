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
    """爬取普通貼文（個人貼文、社團貼文、轉發貼文）"""
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

        for comment_elem in main_story.find_all(['div', 'footer', 'section'], class_=re.compile(r'ufi|comment|feedback', re.I)):
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

        # 3. 圖片提取（鎖定 __isMedia: Photo）
        image_urls = []
        found_photo_ids = []

        media_photo_matches = re.findall(r'"id":"(\d{14,18})","__isMedia":"Photo"', unescaped_html)
        for pid in media_photo_matches:
            if pid not in found_photo_ids:
                found_photo_ids.append(pid)

        if len(found_photo_ids) < 3:
            link_matches = re.findall(r'(?:/photo/?\?fbid=|photo\.php\?fbid=)(\d{14,18})', unescaped_html)
            for pid in link_matches:
                if pid not in found_photo_ids:
                    found_photo_ids.append(pid)

        if found_photo_ids:
            for pid in found_photo_ids:
                lookaside_url = f"https://lookaside.fbsbx.com/lookaside/crawler/media/?media_id={pid}"
                if lookaside_url not in image_urls:
                    image_urls.append(lookaside_url)
                if len(image_urls) >= 3:
                    break

        if len(image_urls) < 3:
            raw_cdn_matches = re.findall(r'(https:[^"\'\s<>\\]+?fbcdn\.net/[^"\'\s<>\\]+?-[68]/[^"\'\s<>\\]+)', unescaped_html)
            for raw_img in raw_cdn_matches:
                clean_img = raw_img.replace('&amp;', '&')
                if any(k in clean_img for k in ["s32x32", "s100x100", "p50x50", "p100x100", "emoji", "static", "rsrc.php"]):
                    continue
                if clean_img not in image_urls:
                    image_urls.append(clean_img)
                if len(image_urls) >= 3:
                    break

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
    """yt-dlp 下載模組 (含社團 permalink 自動探測)"""
    temp_dir = tempfile.gettempdir()
    out_template = os.path.join(temp_dir, 'fb_%(id)s.%(ext)s')

    target_urls = []

    id_match = re.search(r'/(?:reel|videos)/(\d+)', fb_url)
    if id_match:
        target_urls.append(f"https://www.facebook.com/watch/?v={id_match.group(1)}")
        target_urls.append(f"https://www.facebook.com/reel/{id_match.group(1)}")

    if "/groups/" in fb_url:
        try:
            m_url = re.sub(r'https?://(?:www\.)?facebook\.com', 'https://m.facebook.com', fb_url.split('?')[0])
            res = requests.get(m_url, headers=REQUEST_HEADERS, impersonate="chrome120", timeout=8)
            if res.status_code == 200:
                unescaped = res.text.replace(r'\/', '/')
                v_ids = re.findall(r'(?:video_id["\':=]|"video":\{"id":")(\d{12,18})', unescaped)
                if not v_ids:
                    v_ids = re.findall(r'"playable_url".*?"id":"(\d{12,18})"', unescaped)
                for vid in v_ids:
                    watch_candidate = f"https://www.facebook.com/watch/?v={vid}"
                    if watch_candidate not in target_urls:
                        target_urls.append(watch_candidate)
        except Exception as e:
            print(f"[Rescue yt-dlp] 探測社團影片 ID 失敗: {e}")

    if fb_url not in target_urls:
        target_urls.append(fb_url)

    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'outtmpl': out_template,
        'quiet': True,
        'no_warnings': True,
        'max_filesize': 50 * 1024 * 1024,
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
                        'title': info.get('title') or "Facebook 影片"
                    }
        except Exception:
            continue

    return None


async def _watchdog_worker_embed(fix_msg: discord.Message, target_fb_url: str, raw_fb_url: str, fix_fb_url: str):
    """
    非同步看門狗：
    結合 Worker 診斷標頭與動態檔案上限檢測
    """
    print(f"\n[Watchdog] 🐕 看門狗啟動，監聽訊息 ID: {fix_msg.id}")
    channel = fix_msg.channel

    # 1. 探測 Worker 診斷狀態（辨識是 FB 擋 IP 還是抓取未命中）
    try:
        diag_res = await asyncio.to_thread(
            requests.get,
            fix_fb_url,
            headers={'User-Agent': 'Discordbot/2.0'},
            allow_redirects=False,
            timeout=5
        )
        fb_status = diag_res.headers.get("X-FB-Status", "未知")
        fb_blocked = diag_res.headers.get("X-FB-Blocked", "未知")
        video_found = diag_res.headers.get("X-Video-Found", "未知")
        print(f"[Watchdog Diag] 🌐 Worker 診斷: FB狀態={fb_status}, 是否被擋={fb_blocked}, 命中影片={video_found}")
    except Exception as diag_err:
        print(f"[Watchdog Diag] ⚠️ 探測 Worker 診斷失敗: {diag_err}")

    # 初審等待
    await asyncio.sleep(9.0)

    try:
        refreshed_msg = await channel.fetch_message(fix_msg.id)
    except discord.NotFound:
        return
    except Exception:
        refreshed_msg = None

    has_video_tag = False
    is_fake_video = False

    if refreshed_msg and refreshed_msg.embeds:
        for idx, emb in enumerate(refreshed_msg.embeds):
            v_url = emb.video.url if emb.video else ""
            if emb.video and v_url:
                if "lookaside.fbsbx.com" in v_url or "media/?media_id=" in v_url:
                    is_fake_video = True
                    break
                has_video_tag = True
                break

    if is_fake_video or not has_video_tag:
        print("[Watchdog] ⚠️ 初審未通過，進入救援流程！")
        await _trigger_ytdlp_rescue(fix_msg, target_fb_url, raw_fb_url)
        return

    # 複審等待
    await asyncio.sleep(9.0)

    try:
        refreshed_msg = await channel.fetch_message(fix_msg.id)
        if not refreshed_msg:
            return

        is_still_valid = False
        for emb in refreshed_msg.embeds:
            v_url = emb.video.url if emb.video else ""
            if emb.video and v_url and emb.video.proxy_url:
                if "lookaside.fbsbx.com" not in v_url:
                    is_still_valid = True
                    break

        if is_still_valid:
            print("[Watchdog] ✅ 複審確認影片正常播放，看門狗完成任務！")
            fb_cache[raw_fb_url] = {"type": "worker"}
            return

        print("[Watchdog] ⚠️ 複審判定播放器破圖失效，啟動救援機制！")
        await _trigger_ytdlp_rescue(fix_msg, target_fb_url, raw_fb_url)

    except discord.NotFound:
        pass
    except Exception as e:
        print(f"[Watchdog] ❌ 階段二例外: {e}")


async def _trigger_ytdlp_rescue(fix_msg: discord.Message, target_fb_url: str, raw_fb_url: str):
    """
    執行 yt-dlp 救援並嚴格防護 413 錯誤（動態伺服器上限偵測）
    """
    print(f"\n[Rescue] 🚨 進入救援流程！目標: {target_fb_url}")
    channel = fix_msg.channel
    guild = fix_msg.guild

    # 動態計算該伺服器的精準上傳上限（預留 512KB 安全緩衝）
    server_limit = getattr(guild, "filesize_limit", 25 * 1024 * 1024) if guild else 25 * 1024 * 1024
    safe_upload_limit = min(server_limit - (512 * 1024), 24 * 1024 * 1024)  # 保守限制在 24MB 內避免 413

    try:
        await fix_msg.delete()
    except Exception:
        pass

    try:
        status_msg = await channel.send("⏳ **影片容量較大或解析受限**，正在為您直接擷取原始檔案，請稍候...")
    except Exception:
        status_msg = None

    ytdl_result = await asyncio.to_thread(_download_fb_video_ytdlp, target_fb_url)

    if ytdl_result and ytdl_result.get('file_path') and os.path.exists(ytdl_result['file_path']):
        file_path = ytdl_result['file_path']
        file_size = os.path.getsize(file_path)
        print(f"[Rescue] 下載完成，體積: {file_size} bytes (伺服器安全上限: {safe_upload_limit} bytes)")

        # 關鍵防線：體積超出時直接攔截，絕不硬傳引發 413
        if file_size > safe_upload_limit:
            print(f"[Rescue] ⚠️ 檔案體積超出伺服器可上傳上限 ({file_size} > {safe_upload_limit})，轉為引導卡片")
            if os.path.exists(file_path):
                os.remove(file_path)

            embed = discord.Embed(
                title="📦 影片檔案超出 Discord 上傳上限",
                description=f"此影片檔案大小為 **{round(file_size / (1024*1024), 2)} MB**，已超出此伺服器的檔案限制。\n請直接前往觀看：\n[點此開啟 Facebook 原片]({target_fb_url})",
                color=0x1877F2
            )
            if status_msg:
                await status_msg.edit(content=None, embed=embed)
            else:
                await channel.send(embed=embed)
            return

        try:
            video_title = ytdl_result.get('title', 'Facebook 影片')
            discord_file = discord.File(file_path, filename="facebook_video.mp4")
            await channel.send(
                content=f"**{video_title}**",
                file=discord_file
            )
            print("[Rescue] ✅ 影片檔案上傳成功！")

            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            fb_cache[raw_fb_url] = {"type": "downloaded"}
            return
        except discord.HTTPException as http_err:
            print(f"[Rescue] ❌ 上傳檔案遇到 HTTP 例外 (包含 413): {http_err}")
            # 遭遇 413 時立即編輯提示訊息，消除使用者無限乾等
            embed = discord.Embed(
                title="📦 影片超出伺服器負載上限",
                description=f"Discord 拒絕接收此檔案 (HTTP 413)，請直接至原網址觀看：\n[點此開啟 Facebook 原片]({target_fb_url})",
                color=0x1877F2
            )
            if status_msg:
                await status_msg.edit(content=None, embed=embed)
            else:
                await channel.send(embed=embed)
            return
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

    # 下載失敗時的保底卡片
    embed = discord.Embed(
        title="🔒 此 Facebook 內容受限或無法取得公開媒體",
        description=f"請直接點擊原連結前往觀看：\n[前往 Facebook 觀看]({target_fb_url})",
        color=0x1877F2
    )
    if status_msg:
        await status_msg.edit(content=None, embed=embed)
    else:
        await channel.send(embed=embed)


async def process_facebook_embed(raw_fb_url: str, message: discord.Message, pending_suppress_ids: set):
    """Facebook 預覽主入口"""
    pending_suppress_ids.add(message.id)
    try:
        await message.edit(suppress=True)
    except Exception:
        pass

    target_fb_url = await asyncio.to_thread(_resolve_fb_url_sync, raw_fb_url)

    is_video_content = any(k in target_fb_url.lower() for k in ["/reel/", "/videos/", "/watch"]) or \
                       any(k in raw_fb_url.lower() for k in ["/share/r/", "/share/v/"])

    if not is_video_content:
        post_data = await asyncio.to_thread(_extract_fb_post_sync, target_fb_url)
        if post_data and len(post_data.get("images", [])) > 0:
            embeds = []
            main_embed = discord.Embed(
                title=post_data["title"],
                url=target_fb_url,
                description=post_data["description"] if post_data["description"] else None,
                color=0x1877F2
            )
            main_embed.set_image(url=post_data["images"][0])
            embeds.append(main_embed)

            if len(post_data["images"]) > 1:
                for extra_img in post_data["images"][1:3]:
                    extra_embed = discord.Embed(url=target_fb_url)
                    extra_embed.set_image(url=extra_img)
                    embeds.append(extra_embed)

            await message.channel.send(embeds=embeds)
            return

    fix_fb_url = re.sub(r"(https?://)(?:www\.)?(?:facebook\.com|fb\.watch)", rf"\1{CUSTOM_WORKER_DOMAIN}", target_fb_url)

    try:
        fix_msg = await message.channel.send(f"[\u2800]({fix_fb_url})")
    except Exception as e:
        print(f"❌ 發送代理訊息失敗: {e}")
        return

    asyncio.create_task(_watchdog_worker_embed(fix_msg, target_fb_url, raw_fb_url, fix_fb_url))