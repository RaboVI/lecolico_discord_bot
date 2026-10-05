import re
import random
import asyncio
import urllib.parse
from datetime import datetime, timezone
import discord
from curl_cffi import requests  # 使用具備瀏覽器指紋模擬的 requests

# 完整鏡像桌面 Chrome 瀏覽器的 Client Hints 與安全標頭
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
    'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'cross-site',
}

def linkify_hashtags(text: str) -> str:
    """將推文內文中的獨立 Hashtag 自動轉為可點擊的 Markdown 超連結"""
    def replace_hashtag(match):
        tag = match.group(1)
        encoded_tag = urllib.parse.quote(tag)
        return f"[#{tag}](https://x.com/hashtag/{encoded_tag})"

    return re.sub(r'(?<!\S)#([^\s#.,!?:;，。！？]+)', replace_hashtag, text)


def _fetch_tweet_data_sync(author_screen_name: str, tweet_id: str) -> dict | None:
    """
    在背景執行緒依序請求 FxTwitter 與 VxTwitter API
    - 第一順位：穩定度與通過率極高的 FxTwitter
    - 第二順位：VxTwitter 作為備援
    """
    api_endpoints = [
        f"https://api.fxtwitter.com/{author_screen_name}/status/{tweet_id}",
        f"https://api.vxtwitter.com/{author_screen_name}/status/{tweet_id}"
    ]

    for api_url in api_endpoints:
        try:
            res = requests.get(
                api_url,
                headers=REQUEST_HEADERS,
                impersonate="chrome124",
                timeout=6
            )
            if res.status_code == 200:
                data = res.json()
                # 統一 FxTwitter 的回傳結構，映射為共通欄位
                if "tweet" in data:
                    t = data["tweet"]
                    media = t.get("media", {})
                    all_media = media.get("all", []) or media.get("photos", []) or media.get("videos", [])
                    return {
                        "hasMedia": bool(all_media),
                        "media_extended": all_media,
                        "text": t.get("text", ""),
                        "likes": t.get("likes", 0),
                        "views": t.get("views"),
                        "user_name": t.get("author", {}).get("name", author_screen_name),
                        "user_screen_name": t.get("author", {}).get("screen_name", author_screen_name),
                        "user_profile_image_url": t.get("author", {}).get("avatar_url", ""),
                        "date_epoch": t.get("created_timestamp", 0)
                    }
                return data
            else:
                print(f"[Twitter] 端點 {api_url} 回應 {res.status_code}，嘗試切換備援 API...")
        except Exception as e:
            print(f"[Twitter] 請求 {api_url} 異常: {e}")

    return None

async def process_x_embed(raw_x_url: str, message: discord.Message, pending_suppress_ids: set):
    """
    非同步處理 X (Twitter) 貼文預覽
    - 純文字推文：保留原生預覽
    - 影片 / GIF 推文：隨機派發 fixvx / fixupx 代理
    - 純圖片推文：一律自組 Embed (杜絕 18+ 擋板並支援多圖排版)
    """
    # 精確提取作者名稱與推文 ID
    id_match = re.search(r"(?:twitter\.com|x\.com)/([a-zA-Z0-9_]+)/status/(\d+)", raw_x_url)
    if not id_match:
        return

    author_screen_name = id_match.group(1)
    tweet_id = id_match.group(2)

    tweet_data = await asyncio.to_thread(_fetch_tweet_data_sync, author_screen_name, tweet_id)
    if not tweet_data:
        return

    has_media = tweet_data.get("hasMedia", False)
    media_extended = tweet_data.get("media_extended", [])

    # 判斷是否包含影片或動圖
    has_video_or_gif = any(m.get("type") in ["video", "gif"] for m in media_extended)

    # 1. 純文字推文：保留原生預覽 (不做事)
    if not has_media:
        return

    # 2. 含有影片 / GIF：轉發修復代理連結
    if has_video_or_gif:
        x_proxies = ["fixvx.com", "fixupx.com"]
        chosen_proxy = random.choice(x_proxies)

        domain_match = re.search(r"(x|twitter)\.com", raw_x_url).group(0)
        fix_x_url = raw_x_url.replace(domain_match, chosen_proxy)

        await message.channel.send(f"[Xfix]({fix_x_url})")

        # 登記訊息交由 on_message_edit 補刀壓抑
        pending_suppress_ids.add(message.id)
        try:
            await message.edit(suppress=True)
        except Exception as e:
            print(f"[Twitter] 隱藏原訊息預覽失敗: {e}")
        return

    # 3. 圖片推文：統一自組 Embed
    raw_text = tweet_data.get("text", "")
    likes = tweet_data.get("likes", 0)
    views = tweet_data.get("views")
    author_name = tweet_data.get("user_name", author_screen_name)
    author_screen_name = tweet_data.get("user_screen_name", author_screen_name)
    author_avatar = tweet_data.get("user_profile_image_url", "")
    date_epoch = tweet_data.get("date_epoch", 0)

    formatted_text = linkify_hashtags(raw_text)

    # 主卡片設定
    primary_embed = discord.Embed(
        description=formatted_text if formatted_text else None,
        url=raw_x_url,
        color=0x1DA1F2,
        timestamp=datetime.fromtimestamp(date_epoch, timezone.utc) if date_epoch else None
    )

    primary_embed.set_author(
        name=f"{author_name} (@{author_screen_name})",
        url=raw_x_url,
        icon_url=author_avatar if author_avatar else None
    )

    image_urls = [m.get("url") for m in media_extended if m.get("url")]
    if image_urls:
        primary_embed.set_image(url=image_urls[0])

    # Footer 格式：X • ❤️ 讚數 • 📷 觀看數
    footer_parts = ["X", f"❤️ {likes:,}"]
    if views is not None:
        footer_parts.append(f"📷 {views:,}")
    primary_embed.set_footer(text="  •  ".join(footer_parts))

    embeds_to_send = [primary_embed]

    # 支援第 2 至 4 張圖片的原生並排拼貼
    for extra_url in image_urls[1:4]:
        extra_embed = discord.Embed(url=raw_x_url)
        extra_embed.set_image(url=extra_url)
        embeds_to_send.append(extra_embed)

    await message.channel.send(embeds=embeds_to_send)

    # 登記訊息交由 on_message_edit 補刀壓抑
    pending_suppress_ids.add(message.id)
    try:
        await message.edit(suppress=True)
    except Exception as e:
        print(f"[Twitter] 隱藏原訊息預覽失敗: {e}")