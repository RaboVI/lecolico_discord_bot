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

# 快取 1 小時內的解析結果
fb_cache = TTLCache(maxsize=150, ttl=3600)

# 特權爬蟲標頭（Meta 官方允許社交平台抓取 Open Graph 的標準標頭）
CRAWLER_HEADERS = {
    'User-Agent': 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)',
    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7'
}

# 一般瀏覽器標頭（yt-dlp 救援與社團影片 ID 探測使用）
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7'
}

FB_COOKIE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'fb_cookies.txt')


def _clean_json_str(text: str) -> str:
    """安全解碼 Facebook JSON 中的 Unicode 轉義字元與換行，避免中文亂碼"""
    if not text:
        return ""
    text = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), text)
    text = text.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
    return text.strip()


def _clean_fb_url(url: str) -> str:
    """
    清理 Facebook 網址多餘參數，同時完整保留相片 (fbid)、貼文 (pfbid/數字) 與專頁關鍵 ID
    """
    if not url:
        return url

    # 1. 檢查是否包含相片 ID (fbid)
    fbid_match = re.search(r'[?&]fbid=(\d+)', url)
    if fbid_match:
        return f"https://www.facebook.com/photo.php?fbid={fbid_match.group(1)}"

    # 2. 檢查是否包含 story_fbid（相容新版 pfbid 英數混編與純數字）
    story_fbid_match = re.search(r'[?&]story_fbid=([^&]+)', url)
    if story_fbid_match:
        story_val = story_fbid_match.group(1)
        id_match = re.search(r'[?&]id=(\d+)', url)
        if id_match:
            return f"https://www.facebook.com/permalink.php?story_fbid={story_val}&id={id_match.group(1)}"
        return f"https://www.facebook.com/permalink.php?story_fbid={story_val}"

    # 3. 一般常規路徑
    clean_base = url.split('?')[0].rstrip('/')

    if clean_base.endswith("/photo"):
        any_id = re.search(r'(\d{10,18})', url)
        if any_id:
            return f"https://www.facebook.com/photo.php?fbid={any_id.group(1)}"

    return clean_base


def _resolve_fb_url_sync(raw_fb_url: str) -> str:
    """
    同步還原 Facebook /share/ 短跳轉網址至真實路徑：
    以 facebookexternalhit 爬蟲標頭進行單跳探測，精準攔截 Location 中的相片與 pfbid 貼文 ID
    """
    target_url = raw_fb_url
    if "/share/" not in raw_fb_url:
        return _clean_fb_url(target_url)

    print(f"\n[FB Resolve Debug] 🔍 開始解析短網址: {raw_fb_url}")

    # 階段 1：使用特權爬蟲標頭進行單跳探測 (HEAD / GET, allow_redirects=False)
    for method in ['head', 'get']:
        try:
            req_fn = getattr(requests, method)
            hop = req_fn(
                raw_fb_url,
                headers=CRAWLER_HEADERS,
                allow_redirects=False,
                timeout=6
            )
            loc = hop.headers.get('Location') or hop.headers.get('location') or ''
            print(f"[FB Resolve Debug] 單跳 [{method.upper()}] 狀態碼: {hop.status_code}, Location: {loc}")

            if loc:
                decoded_loc = urllib.parse.unquote(urllib.parse.unquote(loc))
                # 檢查相片 fbid
                fbid_m = re.search(r'[?&]fbid=(\d+)', decoded_loc)
                if fbid_m:
                    res_url = f"https://www.facebook.com/photo.php?fbid={fbid_m.group(1)}"
                    print(f"[FB Resolve Debug] ✅ 成功從 Location 命中相片 ID: {res_url}")
                    return res_url

                # 檢查貼文 / 影片 story_fbid（相容 pfbid 英數格式）
                story_m = re.search(r'[?&]story_fbid=([^&]+)', decoded_loc)
                if story_m:
                    story_val = story_m.group(1)
                    id_m = re.search(r'[?&]id=(\d+)', decoded_loc)
                    id_param = f"&id={id_m.group(1)}" if id_m else ""

                    if any(k in raw_fb_url.lower() for k in ["/share/r/", "/share/v/"]):
                        res_url = f"https://www.facebook.com/reel/{story_val}"
                    else:
                        res_url = f"https://www.facebook.com/permalink.php?story_fbid={story_val}{id_param}"
                    print(f"[FB Resolve Debug] ✅ 成功從 Location 命中 story_fbid: {res_url}")
                    return res_url

                # 若跳轉至完整路徑且非登入頁
                if "/share/" not in loc and "/login" not in loc:
                    res_url = _clean_fb_url(decoded_loc)
                    print(f"[FB Resolve Debug] ✅ 成功從 Location 取得真實路徑: {res_url}")
                    return res_url
        except Exception as e:
            print(f"[FB Resolve Debug] ⚠️ 單跳 [{method.upper()}] 異常: {e}")

    # 階段 2：若單跳未截獲，以 GET 跟隨跳轉 (allow_redirects=True)，全面掃描 history 與 HTML
    try:
        res = requests.get(
            raw_fb_url,
            headers=CRAWLER_HEADERS,
            allow_redirects=True,
            timeout=8
        )
        print(f"[FB Resolve Debug] GET 跟隨跳轉完成，最終 URL: {res.url}, 狀態碼: {res.status_code}")
        print(f"[FB Resolve Debug] 跳轉歷史狀態碼: {[h.status_code for h in res.history]}")

        # 掃描跳轉歷史中的 Location
        for h in res.history:
            loc_val = h.headers.get('Location') or h.headers.get('location') or ''
            if loc_val:
                decoded_val = urllib.parse.unquote(urllib.parse.unquote(loc_val))
                fbid_m = re.search(r'[?&]fbid=(\d+)', decoded_val)
                if fbid_m:
                    res_url = f"https://www.facebook.com/photo.php?fbid={fbid_m.group(1)}"
                    print(f"[FB Resolve Debug] ✅ 從 history Location 截獲 fbid: {res_url}")
                    return res_url

                story_m = re.search(r'[?&]story_fbid=([^&]+)', decoded_val)
                if story_m:
                    story_val = story_m.group(1)
                    id_m = re.search(r'[?&]id=(\d+)', decoded_val)
                    id_param = f"&id={id_m.group(1)}" if id_m else ""
                    res_url = f"https://www.facebook.com/permalink.php?story_fbid={story_val}{id_param}"
                    print(f"[FB Resolve Debug] ✅ 從 history Location 截獲 story_fbid: {res_url}")
                    return res_url

        # 檢查最終 URL 是否包含 fbid 或 story_fbid
        decoded_final = urllib.parse.unquote(urllib.parse.unquote(res.url))
        fbid_m = re.search(r'[?&]fbid=(\d+)', decoded_final)
        if fbid_m:
            res_url = f"https://www.facebook.com/photo.php?fbid={fbid_m.group(1)}"
            print(f"[FB Resolve Debug] ✅ 從最終 URL 截獲 fbid: {res_url}")
            return res_url

        story_m = re.search(r'[?&]story_fbid=([^&]+)', decoded_final)
        if story_m:
            story_val = story_m.group(1)
            id_m = re.search(r'[?&]id=(\d+)', decoded_final)
            id_param = f"&id={id_m.group(1)}" if id_m else ""
            res_url = f"https://www.facebook.com/permalink.php?story_fbid={story_val}{id_param}"
            print(f"[FB Resolve Debug] ✅ 從最終 URL 截獲 story_fbid: {res_url}")
            return res_url

        # 從 HTML 的 og:url 或 canonical 標籤萃取
        soup = BeautifulSoup(res.text, 'html.parser')
        og_url_tag = soup.find('meta', property='og:url')
        if og_url_tag and og_url_tag.get('content'):
            og_content = og_url_tag['content']
            if "/share/" not in og_content and "/login" not in og_content:
                return _clean_fb_url(og_content)

        canonical_tag = soup.find('link', rel='canonical')
        if canonical_tag and canonical_tag.get('href'):
            can_content = canonical_tag['href']
            if "/share/" not in can_content and "/login" not in can_content:
                return _clean_fb_url(can_content)

        # 從 HTML 原始碼中正則搜尋相片 ID
        p_match = re.search(r'(?:photo\.php\?fbid=|/photo/?\?fbid=|"fbid":")(\d{12,18})', res.text)
        if p_match:
            res_url = f"https://www.facebook.com/photo.php?fbid={p_match.group(1)}"
            print(f"[FB Resolve Debug] ✅ 從 HTML 原始碼挖出相片 ID: {res_url}")
            return res_url

    except Exception as e:
        print(f"[FB Resolve Debug] ⚠️ 跟隨跳轉階段異常: {e}")

    print(f"[FB Resolve Debug] ❌ 無法還原短網址，退回原始網址清洗結果")
    return _clean_fb_url(target_url)


def _extract_fb_post_sync(target_fb_url: str) -> dict | None:
    """
    爬取普通貼文（支援個人貼文、社團貼文、單張相片貼文、多圖貼文與 permalink.php 貼文）：
    1. 支援單張相片貼文模式，嚴格只抓指定相片
    2. 從 Relay/GraphQL 快照精準補充作者標題與貼文文案
    """
    try:
        clean_url = _clean_fb_url(target_fb_url)
        is_single_photo = bool(re.search(r'[?&]fbid=\d+', clean_url) or "/photo" in clean_url.lower())

        if "/share/" in clean_url:
            request_url = clean_url
        else:
            request_url = re.sub(r'https?://(?:www\.)?facebook\.com', 'https://m.facebook.com', clean_url)

        print(f"\n[FB Extract Debug] 📥 開始抓取貼文，請求網址: {request_url}")

        res = requests.get(
            request_url,
            headers=CRAWLER_HEADERS,
            impersonate="chrome120",
            allow_redirects=True,
            timeout=10
        )

        print(f"[FB Extract Debug] 回應狀態碼: {res.status_code}, 最終 URL: {res.url}")

        if res.status_code != 200 or "/login" in res.url:
            print("[FB Extract Debug] ❌ 請求失敗或遭遇登入擋板")
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
        is_group_post = "/groups/" in target_fb_url or "groups" in request_url

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

        # 針對 photo、permalink 或未命名頁面從 JSON 快照補強作者名稱
        if not clean_title or clean_title.lower() == "facebook":
            owner_match = re.search(r'"(?:owner|actor|author)":\{[^}]*?"name":"([^"]+)"', unescaped_html)
            if not owner_match:
                owner_match = re.search(r'"actors":\[\{[^}]*?"name":"([^"]+)"', unescaped_html)
            if owner_match:
                candidate_name = _clean_json_str(owner_match.group(1))
                if candidate_name and candidate_name.lower() != "facebook":
                    clean_title = candidate_name

        if not clean_title or clean_title.lower() == "facebook":
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

        # 從 Relay/GraphQL 快照中萃取真實文案
        if not raw_desc:
            msg_match = re.search(r'"message":\{"text":"([^"]+)"\}', unescaped_html)
            if not msg_match:
                msg_match = re.search(r'"caption":\{"text":"([^"]+)"\}', unescaped_html)
            if not msg_match:
                msg_match = re.search(r'"savable_description":\{"text":"([^"]+)"\}', unescaped_html)
            if msg_match:
                raw_desc = _clean_json_str(msg_match.group(1))

        clean_lines = [line.strip() for line in raw_desc.splitlines() if line.strip()]
        clean_lines = [l for l in clean_lines if not re.match(r'^(?:加入|已加入|公開社團|私人社團|\d+位成員)$', l)]
        compact_desc = "\n".join(clean_lines)

        if len(compact_desc) > 80:
            compact_desc = compact_desc[:80] + "..."

        # 3. 圖片提取
        image_urls = []
        fbid_in_url = re.search(r'[?&]fbid=(\d+)', clean_url)

        if is_single_photo and fbid_in_url:
            target_pid = fbid_in_url.group(1)
            image_urls.append(f"https://lookaside.fbsbx.com/lookaside/crawler/media/?media_id={target_pid}")
        else:
            found_photo_ids = []
            if fbid_in_url:
                found_photo_ids.append(fbid_in_url.group(1))

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

        print(f"[FB Extract Debug] ✅ 抓取完成 - 標題: {clean_title}, 內文: {compact_desc[:20]}..., 圖片數: {len(image_urls)}")

        return {
            "title": clean_title,
            "description": compact_desc,
            "images": image_urls
        }

    except Exception as e:
        print(f"[FB Extract Debug] ❌ 解析貼文異常: {e}")
        return None


async def _trigger_fallback_card(channel: discord.abc.Messageable, original_url: str):
    """發布防呆卡片"""
    embed = discord.Embed(
        title="🔒 此 Facebook 內容受限或無法取得公開媒體",
        description=f"請直接點擊原連結前往觀看：\n[前往 Facebook 觀看]({original_url})",
        color=0x1877F2
    )
    await channel.send(embed=embed)


# 看門狗等待設定：第一次產生預覽時 Discord 要讀兩次 Worker，常常超過 8 秒
WATCHDOG_POLL_INTERVAL = 2.0   # 每隔幾秒檢查一次
WATCHDOG_FIRST_WAIT = 20.0     # 初審最多等幾秒
WATCHDOG_SECOND_WAIT = 8.0     # 初審通過後再等幾秒做複審


def _find_valid_video(msg: discord.Message | None, require_proxy: bool = False) -> str:
    """檢查訊息的 Embed 影片狀態：回傳 "ok"、"fake"（假影片）或 "none"（還沒有影片）"""
    if not msg or not msg.embeds:
        return "none"
    for emb in msg.embeds:
        v_url = emb.video.url if emb.video else ""
        if emb.video and v_url:
            if "lookaside.fbsbx.com" in v_url or "media/?media_id=" in v_url:
                return "fake"
            if require_proxy and not emb.video.proxy_url:
                continue
            return "ok"
    return "none"


async def _watchdog_worker_embed(fix_msg: discord.Message, target_fb_url: str, raw_fb_url: str, fix_fb_url: str):
    """非同步看門狗：專門監控 Worker 原生影片播放器生成狀況"""
    print(f"\n[Watchdog] 🐕 看門狗啟動，監聽訊息 ID: {fix_msg.id}")
    channel = fix_msg.channel

    # 階段一：每 2 秒檢查一次，最多等 20 秒，影片一出現就提早通過
    status = "none"
    waited = 0.0
    while waited < WATCHDOG_FIRST_WAIT:
        await asyncio.sleep(WATCHDOG_POLL_INTERVAL)
        waited += WATCHDOG_POLL_INTERVAL
        try:
            refreshed_msg = await channel.fetch_message(fix_msg.id)
        except discord.NotFound:
            return
        except Exception:
            refreshed_msg = None

        status = _find_valid_video(refreshed_msg)
        if status != "none":
            break

    if status != "ok":
        print(f"[Watchdog] ⚠️ 初審未通過（等待 {waited:.0f} 秒，狀態: {status}），啟動 yt-dlp 救援！")
        await _trigger_ytdlp_rescue(fix_msg, target_fb_url, raw_fb_url)
        return

    print(f"[Watchdog] 🔎 初審通過（等待 {waited:.0f} 秒），進入複審")
    await asyncio.sleep(WATCHDOG_SECOND_WAIT)

    try:
        refreshed_msg = await channel.fetch_message(fix_msg.id)
        if not refreshed_msg:
            return

        if _find_valid_video(refreshed_msg, require_proxy=True) == "ok":
            print("[Watchdog] ✅ 複審確認影片正常播放！")
            fb_cache[raw_fb_url] = {"type": "worker"}
            return

        print("[Watchdog] ⚠️ 複審判定播放器破圖，啟動 yt-dlp 救援！")
        await _trigger_ytdlp_rescue(fix_msg, target_fb_url, raw_fb_url)

    except discord.NotFound:
        pass
    except Exception as e:
        print(f"[Watchdog] ❌ 階段二例外: {e}")


def _download_fb_video_ytdlp(fb_url: str, max_filesize: int) -> dict | None:
    """yt-dlp 下載模組（含社團 permalink 自動探測）；有 fb_cookies.txt 時會帶入以下載需登入 / 成人限制影片"""
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
            res = requests.get(m_url, headers=BROWSER_HEADERS, impersonate="chrome120", timeout=8)
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
        'noplaylist': True,
        'noprogress': True,
        'max_filesize': max_filesize,
        'http_headers': BROWSER_HEADERS,
    }

    has_cookie = os.path.exists(FB_COOKIE_PATH)
    if has_cookie:
        ydl_opts['cookiefile'] = FB_COOKIE_PATH
    print(f"[Rescue yt-dlp] 候選網址 {len(target_urls)} 個，使用 cookies: {has_cookie}")

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
                # yt-dlp 因 max_filesize 跳過下載時不會產生檔案
                print(f"[Rescue yt-dlp] ⚠️ {try_url} 未產生檔案（可能超過 {max_filesize} bytes 上限）")
        except Exception as e:
            print(f"[Rescue yt-dlp] ❌ {try_url} 下載失敗: {str(e)[:200]}")
            continue

    return None


async def _trigger_ytdlp_rescue(fix_msg: discord.Message, target_fb_url: str, raw_fb_url: str):
    """看門狗判定 Worker 播放器失效時：刪除代理訊息，改用 yt-dlp 下載影片並以 discord.File 上傳，失敗才輸出防呆卡片"""
    print(f"\n[Rescue] 🚨 進入 yt-dlp 救援流程！目標: {target_fb_url}")
    channel = fix_msg.channel
    guild = fix_msg.guild

    # 動態計算該伺服器的上傳上限（預留 512KB 安全緩衝，並保守限制在 24MB 內避免 413）
    server_limit = getattr(guild, "filesize_limit", 10 * 1024 * 1024) if guild else 10 * 1024 * 1024
    safe_upload_limit = min(server_limit - (512 * 1024), 24 * 1024 * 1024)

    try:
        await fix_msg.delete()
    except Exception:
        pass

    try:
        status_msg = await channel.send("⏳ **此影片無法直接預覽**，正在為您擷取原始檔案，請稍候...")
    except Exception:
        status_msg = None

    async def _finish_with_embed(embed: discord.Embed):
        if status_msg:
            try:
                await status_msg.edit(content=None, embed=embed)
                return
            except Exception:
                pass
        await channel.send(embed=embed)

    ytdl_result = await asyncio.to_thread(_download_fb_video_ytdlp, target_fb_url, safe_upload_limit)

    if ytdl_result and ytdl_result.get('file_path') and os.path.exists(ytdl_result['file_path']):
        file_path = ytdl_result['file_path']
        try:
            file_size = os.path.getsize(file_path)
            print(f"[Rescue] 下載完成，體積: {file_size} bytes (伺服器安全上限: {safe_upload_limit} bytes)")

            # 關鍵防線：體積超出時直接攔截，絕不硬傳引發 413
            if file_size > safe_upload_limit:
                print("[Rescue] ⚠️ 檔案體積超出伺服器可上傳上限，轉為引導卡片")
                await _finish_with_embed(discord.Embed(
                    title="📦 影片檔案超出 Discord 上傳上限",
                    description=f"此影片檔案大小為 **{round(file_size / (1024 * 1024), 2)} MB**，已超出此伺服器的檔案限制。\n請直接前往觀看：\n[點此開啟 Facebook 原片]({target_fb_url})",
                    color=0x1877F2
                ))
                return

            video_title = ytdl_result.get('title', 'Facebook 影片')
            await channel.send(
                content=f"**{video_title}**",
                file=discord.File(file_path, filename="facebook_video.mp4")
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
            await _finish_with_embed(discord.Embed(
                title="📦 影片超出伺服器負載上限",
                description=f"Discord 拒絕接收此檔案，請直接至原網址觀看：\n[點此開啟 Facebook 原片]({target_fb_url})",
                color=0x1877F2
            ))
            return
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

    # 下載失敗時的保底卡片
    print("[Rescue] ❌ yt-dlp 救援失敗，輸出防呆卡片")
    await _finish_with_embed(discord.Embed(
        title="🔒 此 Facebook 內容受限或無法取得公開媒體",
        description=f"請直接點擊原連結前往觀看：\n[前往 Facebook 觀看]({target_fb_url})",
        color=0x1877F2
    ))


async def process_facebook_embed(raw_fb_url: str, message: discord.Message, pending_suppress_ids: set):
    """
    Facebook 預覽主入口：
    1. 壓抑原訊息預覽
    2. 還原真實路徑並保留相片 ID / 貼文 pfbid ID
    3. 圖文與相片貼文由 Bot 自組 Embed 發送，Reels 走 Worker 代理
    """
    pending_suppress_ids.add(message.id)
    try:
        await message.edit(suppress=True)
    except Exception:
        pass

    target_fb_url = await asyncio.to_thread(_resolve_fb_url_sync, raw_fb_url)

    is_video_content = any(k in target_fb_url.lower() for k in ["/reel/", "/videos/", "/watch"]) or \
                       any(k in raw_fb_url.lower() for k in ["/share/r/", "/share/v/"])

    # 分支 1：【圖文貼文 / 相片貼文】
    if not is_video_content:
        post_data = await asyncio.to_thread(_extract_fb_post_sync, target_fb_url)
        if post_data and len(post_data.get("images", [])) > 0:
            embeds = []
            click_url = target_fb_url if "/share/" not in target_fb_url else raw_fb_url
            main_embed = discord.Embed(
                title=post_data["title"],
                url=click_url,
                description=post_data["description"] if post_data["description"] else None,
                color=0x1877F2
            )
            main_embed.set_image(url=post_data["images"][0])
            embeds.append(main_embed)

            # 僅在多圖貼文時附加副圖
            if len(post_data["images"]) > 1:
                for extra_img in post_data["images"][1:3]:
                    extra_embed = discord.Embed(url=click_url)
                    extra_embed.set_image(url=extra_img)
                    embeds.append(extra_embed)

            await message.channel.send(embeds=embeds)
            return

        print("[FB] ❌ 圖文貼文無法解析，發布防呆卡片")
        await _trigger_fallback_card(message.channel, target_fb_url if "/share/" not in target_fb_url else raw_fb_url)
        return

    # 分支 2：【影片類別 (Reels / Watch)】走 Cloudflare Worker 直出原生播放器
    fix_fb_url = re.sub(r"(https?://)(?:www\.)?(?:facebook\.com|fb\.watch)", rf"\1{CUSTOM_WORKER_DOMAIN}", target_fb_url)

    try:
        fix_msg = await message.channel.send(f"[\u2800]({fix_fb_url})")
    except Exception as e:
        print(f"❌ 發送代理訊息失敗: {e}")
        return

    asyncio.create_task(_watchdog_worker_embed(fix_msg, target_fb_url, raw_fb_url, fix_fb_url))