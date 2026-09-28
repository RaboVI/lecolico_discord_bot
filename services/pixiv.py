import re
import requests
from datetime import datetime, timezone
import discord
from cachetools import TTLCache

# 建立 Pixiv Embed 快取：最多 100 筆，保存 2 小時 (7200 秒)
pixiv_cache = TTLCache(maxsize=100, ttl=7200)


def convert_pximg_url(original_url: str, is_avatar: bool = False) -> str:
    """
    1. 將 Pixiv 官方圖床 i.pximg.net 替換為免 Referer 的公開代理 i.pixiv.re
    2. 若非頭像，自動將正方形頭部縮圖 (_square1200, _custom1200) 還原為等比例完整大圖 (_master1200)
    """
    if not original_url:
        return ""

    clean_url = re.sub(r'https?://[is]\.pximg\.net', 'https://i.pixiv.re', original_url)

    if not is_avatar:
        clean_url = re.sub(r'_(?:square|custom)\d+', '_master1200', clean_url)

    return clean_url


def get_user_avatar(user_id: str, headers: dict) -> str:
    """從繪師個人公開端點提取頭像"""
    if not user_id:
        return ""
    try:
        user_api = f"https://www.pixiv.net/ajax/user/{user_id}?full=0"
        res = requests.get(user_api, headers=headers, timeout=5)
        if res.status_code == 200:
            u_data = res.json()
            raw_avatar = u_data.get("body", {}).get("image", "")
            return convert_pximg_url(raw_avatar, is_avatar=True)
    except Exception:
        pass
    return ""


async def process_pixiv_embed(illust_id: str, original_url: str, message: discord.Message, pending_suppress_ids: set):
    """Pixiv 核心解析處理函式"""
    # 1. 檢查快取
    if illust_id in pixiv_cache:
        cached_embeds = pixiv_cache[illust_id]
        await message.channel.send(embeds=cached_embeds)
        pending_suppress_ids.add(message.id)
        try:
            await message.edit(suppress=True)
        except Exception:
            pass
        return

    # 2. 請求 Pixiv 官方公開 AJAX 端點
    ajax_url = f"https://www.pixiv.net/ajax/illust/{illust_id}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Referer': f'https://www.pixiv.net/artworks/{illust_id}',
        'Accept-Language': 'zh-TW,zh;q=0.9,ja;q=0.8,en;q=0.7'
    }

    try:
        res = requests.get(ajax_url, headers=headers, timeout=10)
        if res.status_code != 200:
            return

        data = res.json()
        if data.get("error"):
            return

        body = data.get("body", {})

        # --- 解析標題與簡介 ---
        title = body.get("illustTitle") or body.get("title") or "Pixiv 插畫"
        description_raw = body.get("description") or ""

        clean_desc = re.sub(r'<br\s*/?>', '\n', description_raw)
        clean_desc = re.sub(r'<[^>]+>', '', clean_desc).strip()
        clean_desc = re.sub(r'\n{2,}', '\n', clean_desc)

        if len(clean_desc) > 80:
            clean_desc = clean_desc[:80].rstrip() + "..."

        # --- 解析作者資訊 ---
        author_name = body.get("userName", "")
        user_id = body.get("userId", "")
        author_url = f"https://www.pixiv.net/users/{user_id}" if user_id else None

        raw_avatar = body.get("profileImageUrl") or body.get("userIllusts", {}).get("profileImg", "")
        if raw_avatar:
            author_avatar = convert_pximg_url(raw_avatar, is_avatar=True)
        else:
            author_avatar = get_user_avatar(user_id, headers)

        # --- 統計數據與時間 ---
        like_count = body.get("likeCount", 0)
        bookmark_count = body.get("bookmarkCount", 0)
        view_count = body.get("viewCount", 0)
        create_date_str = body.get("createDate", "")

        tags_data = body.get("tags", {}).get("tags", [])
        tag_list = [f"`#{t.get('tag')}`" for t in tags_data[:8] if t.get("tag")]
        tags_str = " ".join(tag_list) if tag_list else ""

        # --- 圖片網址提取與判斷 ---
        page_count = body.get("pageCount", 1)
        image_urls = []

        urls_obj = body.get("urls", {})
        first_img = urls_obj.get("regular") or urls_obj.get("original") or ""

        if first_img:
            # 一般未受限作品：使用標準代理轉換
            converted_first = convert_pximg_url(first_img, is_avatar=False)
            image_urls.append(converted_first)

            if page_count > 1 and "_p0" in converted_first:
                for p in range(1, min(page_count, 4)):
                    extra_img = converted_first.replace("_p0", f"_p{p}")
                    image_urls.append(extra_img)
        else:
            # R-18 / 未登入受限作品：使用 pixiv.re 官方路由
            if page_count == 1:
                target_img = f"https://pixiv.re/{illust_id}.jpg"
                image_urls.append(target_img)
            else:
                for p in range(1, min(page_count + 1, 5)):
                    target_img = f"https://pixiv.re/{illust_id}-{p}.jpg"
                    image_urls.append(target_img)

        # --- 組裝主 Embed ---
        embed_color = 0x0096FA
        main_embed = discord.Embed(
            title=title,
            url=original_url,
            description=clean_desc if clean_desc else None,
            color=embed_color
        )

        if author_name:
            main_embed.set_author(
                name=author_name,
                url=author_url,
                icon_url=author_avatar if author_avatar else None
            )

        if tags_str:
            main_embed.add_field(name="🏷️ 標籤", value=tags_str, inline=False)

        if image_urls:
            main_embed.set_image(url=image_urls[0])

        footer_parts = ["Pixiv"]
        if like_count:
            footer_parts.append(f"❤️ {like_count:,}")
        if bookmark_count:
            footer_parts.append(f"🔖 {bookmark_count:,}")
        if view_count:
            footer_parts.append(f"🖥️ {view_count:,}")
        if page_count > 1:
            footer_parts.append(f"📄 共 {page_count} 張")
        if create_date_str:
            try:
                dt = datetime.fromisoformat(create_date_str.replace('Z', '+00:00'))
                footer_parts.append(dt.strftime("%Y/%m/%d %H:%M"))
            except Exception:
                pass

        main_embed.set_footer(text="  •  ".join(footer_parts))

        embeds_to_send = [main_embed]
        for extra_url in image_urls[1:4]:
            sub_embed = discord.Embed(url=original_url)
            sub_embed.set_image(url=extra_url)
            embeds_to_send.append(sub_embed)

        await message.channel.send(embeds=embeds_to_send)
        pixiv_cache[illust_id] = embeds_to_send

        pending_suppress_ids.add(message.id)
        try:
            await message.edit(suppress=True)
        except Exception:
            pass

    except Exception as e:
        print(f"[Pixiv] 解析插畫 ID {illust_id} 時發生錯誤: {e}")