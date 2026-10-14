import re
import json
import asyncio
from datetime import datetime, timezone, timedelta
from curl_cffi import requests
import discord

# ================= 參數與設定 =================
THREADS_WORKER_DOMAIN = "threads.zusakvi.cc"
FALLBACK_PROXY_DOMAIN = "www.fzthreads.com"
THREADS_EMBED_COLOR = 0xE0E2E6  # 柔和銀灰白
WATCHDOG_CHECK_DELAY = 6.0      # 影片 Worker 看門狗檢查秒數

# 台灣時區 (UTC+8)
TZ_TW = timezone(timedelta(hours=8))

# Base64 字元表與 Threads 專屬 App ID
BASE64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
IG_APP_ID = "238260118697367"

RESOLVE_HEADERS = {
    "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}

SSR_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _code_to_post_id(code: str) -> str:
    """將 Threads 貼文短碼轉換為純數字 Post ID (pk)"""
    clean_code = code.split("?")[0].rstrip("/")
    post_id = 0
    for ch in clean_code:
        idx = BASE64_CHARS.find(ch)
        if idx != -1:
            post_id = (post_id << 6) + idx
    return str(post_id)


def _find_post_in_json(obj, target_code: str, target_pk: str):
    """遞迴在 JSON 樹中尋找 code == target_code 或 pk == target_pk 的目標貼文節點"""
    if isinstance(obj, dict):
        obj_code = obj.get("code")
        obj_pk = str(obj.get("pk") or obj.get("id") or "").split("_")[0]
        if (obj_code == target_code or (target_pk and obj_pk == target_pk)) and isinstance(obj.get("user"), dict):
            return obj

        if "thread_items" in obj and isinstance(obj["thread_items"], list):
            for item in obj["thread_items"]:
                if isinstance(item, dict) and isinstance(item.get("post"), dict):
                    p = item["post"]
                    p_code = p.get("code")
                    p_pk = str(p.get("pk") or p.get("id") or "").split("_")[0]
                    if p_code == target_code or (target_pk and p_pk == target_pk):
                        return p

        for val in obj.values():
            found = _find_post_in_json(val, target_code, target_pk)
            if found:
                return found

    elif isinstance(obj, list):
        for elem in obj:
            found = _find_post_in_json(elem, target_code, target_pk)
            if found:
                return found

    return None


def _extract_post_from_html(html_text: str, target_code: str, target_pk: str) -> dict | None:
    """從 HTML 的 <script type="application/json"> 標籤中尋找目標貼文節點"""
    scripts = re.findall(
        r'<script\s+type="application/json"[^>]*>([\s\S]*?)</script>',
        html_text,
        re.IGNORECASE
    )
    for script_content in scripts:
        if '"thread_items"' in script_content or '"text_post_app_info"' in script_content:
            if target_code in script_content or (target_pk and target_pk in script_content):
                try:
                    data = json.loads(script_content)
                    post_node = _find_post_in_json(data, target_code, target_pk)
                    if post_node:
                        return post_node
                except Exception:
                    continue
    return None


def _extract_media_from_node(node: dict) -> tuple[bool, list[str]]:
    """從單一貼文節點提取 (是否含影片, 圖片清單)"""
    has_video = False
    images = []
    if not isinstance(node, dict):
        return False, []

    carousel = node.get("carousel_media")
    if isinstance(carousel, list) and carousel:
        for item in carousel:
            if not isinstance(item, dict):
                continue
            if item.get("video_versions"):
                has_video = True
            candidates = (item.get("image_versions2") or {}).get("candidates") or []
            if candidates and isinstance(candidates[0], dict) and candidates[0].get("url"):
                images.append(candidates[0]["url"])
    else:
        if node.get("video_versions"):
            has_video = True
        candidates = (node.get("image_versions2") or {}).get("candidates") or []
        if candidates and isinstance(candidates[0], dict) and candidates[0].get("url"):
            images.append(candidates[0]["url"])

    return has_video, images


def _deep_find_video_or_images(obj) -> tuple[bool, list[str]]:
    """
    深層探測：當主貼文無媒體時（例如轉發 IG Reels 或引用他篇貼文），
    遞迴搜尋附屬節點（排除大頭貼 profile_pic_url）是否有 video_versions 或 image_versions2
    """
    has_video = False
    images = []

    if isinstance(obj, dict):
        if isinstance(obj.get("video_versions"), list) and len(obj["video_versions"]) > 0:
            has_video = True
        if isinstance(obj.get("image_versions2"), dict):
            cands = obj["image_versions2"].get("candidates") or []
            if cands and isinstance(cands[0], dict) and cands[0].get("url"):
                images.append(cands[0]["url"])

        for k, v in obj.items():
            if k == "user":  # 跳過作者資訊，避免誤抓頭像
                continue
            sub_vid, sub_imgs = _deep_find_video_or_images(v)
            if sub_vid:
                has_video = True
            for img in sub_imgs:
                if img not in images:
                    images.append(img)

    elif isinstance(obj, list):
        for item in obj:
            sub_vid, sub_imgs = _deep_find_video_or_images(item)
            if sub_vid:
                has_video = True
            for img in sub_imgs:
                if img not in images:
                    images.append(img)

    return has_video, images


def _resolve_and_fetch_threads_sync(raw_url: str) -> dict | None:
    """
    同步執行：
    1. 統一網域為 www.threads.com 並透過單跳 Location 解開 /share/ 短網址
    2. 透過 fzthreads 同款 SSR_HEADERS 與 GraphQL 取得貼文資料
    """
    work_url = re.sub(r"https?://(?:www\.)?threads\.(?:net|com)", "https://www.threads.com", raw_url)

    try:
        if "/share/" in work_url:
            hop = requests.get(
                work_url,
                headers=RESOLVE_HEADERS,
                allow_redirects=False,
                timeout=8
            )
            loc = hop.headers.get("Location") or hop.headers.get("location") or ""
            if loc and "/share/" not in loc:
                work_url = loc.split("?")[0]
            else:
                res_share = requests.get(
                    work_url,
                    headers=SSR_HEADERS,
                    allow_redirects=True,
                    timeout=8
                )
                if res_share.url and "/share/" not in res_share.url:
                    work_url = res_share.url.split("?")[0]

        user_match = re.search(r"/@([^/?#]+)/post/([A-Za-z0-9_-]+)", work_url)
        if user_match:
            target_user = user_match.group(1)
            target_code = user_match.group(2)
            clean_post_url = f"https://www.threads.com/@{target_user}/post/{target_code}"
        else:
            code_match = re.search(r"/(?:post|t)/([A-Za-z0-9_-]+)", work_url)
            if not code_match:
                print(f"[Threads] 無法從網址提取貼文短碼: {work_url}")
                return None
            target_user = ""
            target_code = code_match.group(1)
            clean_post_url = f"https://www.threads.com/post/{target_code}"

        target_pk = _code_to_post_id(target_code)
        print(f"[Threads] 1. 原始網址: {raw_url} ➔ 目標網址: {clean_post_url} (短碼: {target_code}, PK: {target_pk})")

        post_node = None
        res_html = requests.get(
            clean_post_url,
            headers=SSR_HEADERS,
            timeout=10
        )
        if res_html.status_code == 200:
            has_items_kw = '"thread_items"' in res_html.text
            has_code_kw = target_code in res_html.text
            print(f"[Threads Debug] SSR 狀態: {res_html.status_code}, 含 thread_items: {has_items_kw}, 含短碼: {has_code_kw}")
            post_node = _extract_post_from_html(res_html.text, target_code, target_pk)

        if not post_node:
            print(f"[Threads] SSR 未命中 {target_code}，啟動 GraphQL 備援查詢...")
            token = "LECOBotToken123"
            gql_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://www.threads.com",
                "Referer": f"{clean_post_url}/",
                "X-FB-Friendly-Name": "BarcelonaPostPageQuery",
                "X-FB-LSD": token,
                "X-IG-App-ID": IG_APP_ID,
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "Cookie": f"lsd={token};",
            }
            payload = {
                "variables": json.dumps({
                    "postID": target_pk,
                    "__relay_internal__pv__BarcelonaIsLoggedInrelayprovider": True,
                    "__relay_internal__pv__BarcelonaIsThreadContextHeaderEnabledrelayprovider": False,
                    "__relay_internal__pv__BarcelonaIsInlineReelsEnabledrelayprovider": True,
                    "__relay_internal__pv__BarcelonaOptionalCookiesEnabledrelayprovider": True,
                    "__relay_internal__pv__BarcelonaShowReshareCountrelayprovider": True,
                }),
                "doc_id": "7448805248491092",
                "lsd": token,
            }
            res_gql = requests.post(
                "https://www.threads.com/api/graphql",
                headers=gql_headers,
                data=payload,
                timeout=10
            )
            if res_gql.status_code == 200:
                try:
                    gql_json = res_gql.json()
                    post_node = _find_post_in_json(gql_json, target_code, target_pk)
                except Exception as e:
                    print(f"[Threads Debug] GraphQL JSON 解析失敗: {e}")

        if not post_node:
            print(f"[Threads] ⚠️ 無法取得短碼 {target_code} 的貼文節點，將交由 fzthreads 保底")
            return {"resolved_url": clean_post_url, "code": target_code, "post": None}

        return {"resolved_url": clean_post_url, "code": target_code, "post": post_node}

    except Exception as e:
        print(f"[Threads] 抓取貼文發生異常: {e}")
        return None


def _parse_threads_post(result_data: dict) -> dict:
    """從原始 post 節點整理出 Embed 與分流所需的乾淨欄位 (含轉發/引用貼文與深層媒體探測)"""
    post = result_data["post"]
    code = post.get("code") or result_data["code"]

    user = post.get("user") or {}
    username = user.get("username") or "threads"
    full_name = user.get("full_name") or username
    avatar_url = user.get("profile_pic_url") or ""

    # 內文整理：緊湊無空行，限制 100 字以內
    raw_caption = (post.get("caption") or {}).get("text") or ""
    lines = [line.strip() for line in raw_caption.splitlines() if line.strip()]
    clean_text = "\n".join(lines)
    if len(clean_text) > 100:
        clean_text = clean_text[:100].rstrip() + "..."

    like_count = post.get("like_count") or 0
    taken_at = post.get("taken_at")

    date_str = ""
    if taken_at:
        try:
            dt = datetime.fromtimestamp(int(taken_at), tz=TZ_TW)
            date_str = dt.strftime("%Y/%m/%d %H:%M")
        except Exception:
            pass

    # 1. 先檢查主貼文本身的媒體
    has_video, images = _extract_media_from_node(post)

    # 2. 若主貼文無媒體，檢查是否為引用/轉發貼文或內嵌 IG Reels (share_info / 深層節點)
    if not has_video and not images:
        share_info = (post.get("text_post_app_info") or {}).get("share_info") or {}
        quoted = share_info.get("quoted_post") or share_info.get("reposted_post")
        if isinstance(quoted, dict):
            has_video, images = _extract_media_from_node(quoted)

        # 若仍未找到，對整個 post 節點做深層搜尋 (例如跨平台 IG Reels 鑲嵌節點)
        if not has_video and not images:
            has_video, images = _deep_find_video_or_images(post)
            if has_video or images:
                print(f"[Threads Debug] 🔍 從深層/引用節點成功挖出媒體! (含影片: {has_video}, 圖片數: {len(images)})")
            else:
                # 印出 text_post_app_info 的鍵值，方便觀察特殊貼文結構
                t_info = post.get("text_post_app_info") or {}
                print(f"[Threads Debug] 此貼文無直接媒體，post keys: {list(post.keys())}, app_info keys: {list(t_info.keys())}")

    canonical_url = f"https://www.threads.com/@{username}/post/{code}"
    print(f"[Threads] 2. ✅ 成功命中貼文! 作者: @{username}, 短碼: {code}, 含影片: {has_video}, 圖片數: {len(images)}")

    return {
        "code": code,
        "username": username,
        "full_name": full_name,
        "avatar_url": avatar_url,
        "clean_text": clean_text,
        "like_count": like_count,
        "date_str": date_str,
        "has_video": has_video,
        "images": images[:3],
        "canonical_url": canonical_url,
    }


async def _threads_video_watchdog(sent_msg: discord.Message, fallback_url: str):
    """
    看門狗機制：若 Worker 在 6 秒後未能讓 Discord 成功展開影片預覽卡片，
    自動將訊息編輯替換為 fzthreads.com 進行保底救援。
    """
    await asyncio.sleep(WATCHDOG_CHECK_DELAY)
    try:
        refreshed_msg = await sent_msg.channel.fetch_message(sent_msg.id)
        if not refreshed_msg.embeds:
            print(f"[Threads Watchdog] Worker 預覽未展開，切換至 fzthreads 救援: {fallback_url}")
            await sent_msg.edit(content=f"[⠀]({fallback_url})")
    except Exception as e:
        print(f"[Threads Watchdog] 檢查失敗: {e}")


async def process_threads_embed(raw_threads_url: str, message: discord.Message, pending_suppress_ids: set):
    result = await asyncio.to_thread(_resolve_and_fetch_threads_sync, raw_threads_url)

    if not result or not result.get("post"):
        base_url = result["resolved_url"] if (result and result.get("resolved_url")) else raw_threads_url
        fallback_url = re.sub(
            r"https?://(?:www\.)?threads\.(?:net|com)",
            f"https://{FALLBACK_PROXY_DOMAIN}",
            base_url
        )
        await message.channel.send(f"[⠀]({fallback_url})")
        pending_suppress_ids.add(message.id)
        try:
            await message.edit(suppress=True)
        except Exception:
            pass
        return

    info = _parse_threads_post(result)

    if info["has_video"]:
        worker_url = f"https://{THREADS_WORKER_DOMAIN}/@{info['username']}/post/{info['code']}"
        fallback_url = f"https://{FALLBACK_PROXY_DOMAIN}/@{info['username']}/post/{info['code']}"

        sent_msg = await message.channel.send(f"[⠀]({worker_url})")

        pending_suppress_ids.add(message.id)
        try:
            await message.edit(suppress=True)
        except Exception:
            pass

        asyncio.create_task(_threads_video_watchdog(sent_msg, fallback_url))
        return

    embeds = []

    main_embed = discord.Embed(
        url=info["canonical_url"],
        description=info["clean_text"] if info["clean_text"] else None,
        color=THREADS_EMBED_COLOR
    )

    author_display = f"{info['full_name']} (@{info['username']})"
    main_embed.set_author(
        name=author_display,
        url=info["canonical_url"],
        icon_url=info["avatar_url"] if info["avatar_url"].startswith("http") else None
    )

    footer_parts = ["Threads", f"❤️ {info['like_count']:,}"]
    if info["date_str"]:
        footer_parts.append(info["date_str"])
    main_embed.set_footer(text=" • ".join(footer_parts))

    if info["images"]:
        main_embed.set_image(url=info["images"][0])
    embeds.append(main_embed)

    for extra_img in info["images"][1:3]:
        sub_embed = discord.Embed(url=info["canonical_url"])
        sub_embed.set_image(url=extra_img)
        embeds.append(sub_embed)

    await message.channel.send(embeds=embeds)

    pending_suppress_ids.add(message.id)
    try:
        await message.edit(suppress=True)
    except Exception as e:
        print(f"[Threads] 無法隱藏原始訊息預覽: {e}")