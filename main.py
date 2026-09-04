import discord
import re
import os
import random
import requests
import asyncio
import urllib.parse # <--- 新增此行，用於處理中日文 Hashtag 網址轉碼
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from urllib.parse import urljoin

# 設定 Intents 以便讀取訊息內容
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# 定義一個通用的安全隱藏預覽輔助函式：
async def suppress_embed_safely(message, delay=2.0):
    """
    先嘗試隱藏預覽，若 Discord 尚未生成，則等待一段時間後進行二次檢查與壓抑
    """
    try:
        await message.edit(suppress=True)
    except Exception as e:
        print(f"首次隱藏預覽失敗: {e}")

    # 等待 Discord 後端完成 Embed 渲染
    await asyncio.sleep(delay)

    try:
        # 重新抓取訊息最新狀態
        fresh_msg = await message.channel.fetch_message(message.id)
        # 若仍存在原生 embeds 且尚未被壓抑，執行二次壓抑
        if fresh_msg.embeds and not fresh_msg.flags.suppress_embeds:
            await fresh_msg.edit(suppress=True)
    except Exception as e:
        # 避免訊息已被使用者手動刪除時拋出 NotFound 錯誤
        pass

# 建立 Hashtag 自動超連結轉換函式
def linkify_hashtags(text):
    def replace_hashtag(match):
        tag = match.group(1)
        # 對標籤進行 URL 編碼 (確保中文/日文 Hashtag 連結能正常點擊)
        encoded_tag = urllib.parse.quote(tag)
        return f"[#{tag}](https://x.com/hashtag/{encoded_tag})"

    # 正則表達式：匹配獨立的 #標籤 (排除網址內部的 # 符號與標點符號)
    return re.sub(r'(?<!\S)#([^\s#.,!?:;，。！？]+)', replace_hashtag, text)


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
# PTT 網址正規表達式
PTT_PATTERN = r"(https?://(www\.)?ptt\.cc/bbs/([a-zA-Z0-9_-]+)/[M]\.[0-9A-Za-z._-]+\.html)"


@client.event
async def on_ready():
    print(f'Bot 已成功登入為 {client.user}')


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
            # 使用 asyncio.create_task 在背景執行二次壓抑，不卡住 Bot 主流程
            asyncio.create_task(suppress_embed_safely(message, delay=2.5))

            # 隱藏使用者發送的原始訊息預覽
            try:
                await message.edit(suppress=True)
            except Exception as e:
                print(f"無法隱藏原始訊息預覽: {e}")

    # ================= 新增：處理 Facebook 網址 =================
    if re.search(FACEBOOK_PATTERN, message.content) and "facebed.com" not in message.content:
        fb_match = re.search(FACEBOOK_PATTERN, message.content)
        if fb_match:
            raw_fb_url = fb_match.group(0)

            # 將 facebook.com / fb.watch 替換為 facebed.com
            fix_fb_url = re.sub(r"(facebook\.com|fb\.watch)", "facebed.com", raw_fb_url)

            # 由 Bot 發送修復後的連結 (Discord 會自動抓取 facebed 的完整文章預覽)
            await message.channel.send(f"[FBfix]({fix_fb_url})")

            # 隱藏使用者發送的原始「Log in or sign up to view」無效預覽
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
            # 使用 asyncio.create_task 在背景執行二次壓抑，不卡住 Bot 主流程
            asyncio.create_task(suppress_embed_safely(message, delay=2.5))

            # 隱藏使用者發送的原始訊息預覽
            try:
                await message.edit(suppress=True)
            except Exception as e:
                print(f"無法隱藏原始訊息預覽: {e}")

    # ================= 新增：處理 Instagram 網址 =================
    # 確保訊息包含 IG 連結，且沒有被代理過 (排除 og/hh/kk)
    if re.search(INSTAGRAM_PATTERN, message.content) and not re.search(r"(og|hh|kk)instagram\.com", message.content):
        ig_match = re.search(INSTAGRAM_PATTERN, message.content)
        if ig_match:
            raw_ig_url = ig_match.group(0)

            # 建立代理伺服器清單
            ig_proxies = ["oginstagram.com", "hhinstagram.com", "kkinstagram.com"]

            # 隨機選擇一個代理
            chosen_proxy = random.choice(ig_proxies)

            # 將 instagram.com 替換為隨機選中的代理網域
            fix_ig_url = re.sub(r"instagram\.com", chosen_proxy, raw_ig_url)

            # 發送隱藏網址文字的超連結
            await message.channel.send(f"[IGfix]({fix_ig_url})")
            # 使用 asyncio.create_task 在背景執行二次壓抑，不卡住 Bot 主流程
            asyncio.create_task(suppress_embed_safely(message, delay=2.5))

            # 隱藏使用者發送的原始訊息預覽
            try:
                await message.edit(suppress=True)
            except Exception as e:
                print(f"無法隱藏原始訊息預覽: {e}")

    # ================= 處理 X / Twitter 網址 =================
    # 確保訊息包含 X 連結，且沒有被代理過 (排除 vx, fx, fixup, fixvx 等前綴)
    if re.search(X_PATTERN, message.content) and not re.search(r"(vx|fx|fixupx|fixvx)(x|twitter)\.com",
                                                               message.content):
        x_match = re.search(X_PATTERN, message.content)
        if x_match:
            raw_x_url = x_match.group(0)

            # 邏輯 4: 將網址轉換為 API 查詢網址 (將 x.com 或 twitter.com 替換為 api.vxtwitter.com)
            api_url = re.sub(r"(x|twitter)\.com", "api.vxtwitter.com", raw_x_url)

            try:
                # 請求 API 獲取推文資料
                response = requests.get(api_url)

                if response.status_code == 200:
                    tweet_data = response.json()

                    has_media = tweet_data.get("hasMedia", False)
                    is_sensitive = tweet_data.get("possibly_sensitive", False)
                    media_extended = tweet_data.get("media_extended", [])

                    # 判斷是否有影片或 GIF
                    has_video_or_gif = any(m.get("type") in ["video", "gif"] for m in media_extended)

                    # 邏輯 7: 沒有媒體 (純文字推文)
                    if not has_media:
                        print("此為純文字推文，保留原生預覽 (不做事)。")

                    # 邏輯 9: 有影片，則隨機呼叫代理服務
                    elif has_video_or_gif:
                        # 建立代理伺服器清單
                        x_proxies = ["fixvx.com", "fixupx.com"]
                        chosen_proxy = random.choice(x_proxies)

                        # 替換原網址中的網域
                        domain_match = re.search(r"(x|twitter)\.com", raw_x_url).group(0)
                        fix_x_url = raw_x_url.replace(domain_match, chosen_proxy)

                        await message.channel.send(f"[Xfix]({fix_x_url})")
                        # 使用 asyncio.create_task 在背景執行二次壓抑，不卡住 Bot 主流程
                        asyncio.create_task(suppress_embed_safely(message, delay=2.5))

                        # 隱藏原始預覽
                        try:
                            await message.edit(suppress=True)
                        except Exception as e:
                            print(f"無法隱藏原始訊息預覽: {e}")

                    # 邏輯 5 & 8: 有媒體且非影片 (即純圖片推文)
                    else:
                        if is_sensitive:
                            # 邏輯 5: 是圖片且為敏感內容 -> 自製 Embed
                            # 從 JSON 中提取所需資料 (內文通常已包含 Hashtag)
                            raw_text = tweet_data.get("text", "")
                            likes = tweet_data.get("likes", 0)
                            views = tweet_data.get("views")  # <--- 正確名稱為複數 views
                            author_name = tweet_data.get("user_name", "")
                            author_screen_name = tweet_data.get("user_screen_name", "")
                            date_epoch = tweet_data.get("date_epoch", 0)

                            # 1. 將內文中的 Hashtag 轉為超連結
                            formatted_text = linkify_hashtags(raw_text)

                            # 2. 建立卡片框架
                            embed = discord.Embed(
                                description=formatted_text,
                                url=raw_x_url,
                                color=0x1DA1F2,  # Twitter 藍色
                                timestamp=datetime.fromtimestamp(date_epoch, timezone.utc)
                            )

                            # 設定作者
                            embed.set_author(name=f"{author_name} (@{author_screen_name})", url=raw_x_url)

                            # 設定圖片 (抓取 media_extended 中的第一張圖片)
                            if media_extended:
                                embed.set_image(url=media_extended[0].get("url"))

                            # 3. 組合 Footer 文字 (愛心數 + 觀看數，支援千分位格式化)
                            footer_parts = [f"❤️ {likes:,}"]
                            if views is not None:
                                footer_parts.append(f"📷 {views:,}")  # 例如: 📷 12,345

                            embed.set_footer(text="  •  ".join(footer_parts))

                            # 發送自製 Embed 並隱藏原連結預覽
                            await message.channel.send(embed=embed)
                            try:
                                await message.edit(suppress=True)
                            except Exception as e:
                                print(f"無法隱藏原始訊息預覽: {e}")

                        else:
                            # 邏輯 8: 是圖片但非敏感內容
                            print("此為一般圖片推文，保留原生預覽 (不做事)。")

                else:
                    print(f"X API 請求失敗，狀態碼: {response.status_code}")

            except Exception as e:
                print(f"處理 X 網址時發生錯誤: {e}")

    # ================= 處理 PTT 網址 =================
    if re.search(PTT_PATTERN, message.content):
        ptt_match = re.search(PTT_PATTERN, message.content)
        if ptt_match:
            raw_ptt_url = ptt_match.group(0)
            board_name = ptt_match.group(3)  # 提取看板名稱

            try:
                # 必須帶入 over18=1 才能繞過八卦版等 18 禁驗證頁面
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                cookies = {'over18': '1'}

                response = requests.get(raw_ptt_url, headers=headers, cookies=cookies)

                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    main_content = soup.find('div', id='main-content')

                    if main_content:
                        # 1. 提取標題與時間 (從 article-metaline 提取)
                        meta_lines = main_content.find_all('div', class_='article-metaline')
                        title = "無標題"
                        post_time = ""
                        for meta in meta_lines:
                            tag = meta.find('span', class_='article-meta-tag').text
                            value = meta.find('span', class_='article-meta-value').text
                            if tag == '標題':
                                title = value
                            elif tag == '時間':
                                raw_time = value.strip()
                                try:
                                    # PTT 時間格式通常為: Fri Sep  4 13:33:05 2026
                                    # 日期個位數時可能會有連續兩個空格，strptime 的 %a %b %d 會自動容錯處理多個空格
                                    dt = datetime.strptime(raw_time, "%a %b %d %H:%M:%S %Y")
                                    post_time = dt.strftime("%Y/%m/%d %H:%M")
                                except Exception as e:
                                    # 若時間格式解析異常，退回原始文字
                                    post_time = raw_time

                        # 2. 備份原始 HTML 字串來尋找多媒體網址
                        raw_html = str(main_content)

                        # 尋找第一張圖片 (支援 jpg, jpeg, png, gif, webp)
                        image_urls = re.findall(r'https?://[^\s"\'<>]+?\.(?:jpg|jpeg|png|gif|webp)', raw_html,
                                                re.IGNORECASE)
                        first_image = image_urls[0] if image_urls else None

                        # 如果沒找到標準附檔名的圖片，嘗試尋找純 imgur 連結並補上 .jpg
                        if not first_image:
                            imgur_links = re.findall(r'https?://(?:i\.)?imgur\.com/([a-zA-Z0-9]{5,7})(?!\.\w+)',
                                                     raw_html)
                            if imgur_links:
                                first_image = f"https://i.imgur.com/{imgur_links[0]}.jpg"

                        # 尋找第一個 YouTube 影片網址
                        yt_urls = re.findall(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[a-zA-Z0-9_-]+',
                                             raw_html)
                        first_yt = yt_urls[0] if yt_urls else None

                        # 3. 淨化內文：移除 Meta 標頭、推文區塊、發信站浮水印等雜訊
                        for tag in main_content.find_all(['div', 'span']):
                            class_name = tag.get('class', [])
                            if any(c in class_name for c in
                                   ['article-metaline', 'article-metaline-right', 'push', 'f2']):
                                tag.extract()

                        # 獲取純文字並切除簽名檔 (PTT 簽名檔通常以 -- 開頭)
                        clean_text = main_content.text.split('--\n')[0]

                        # --- 關鍵優化 1：移除所有圖片與 YouTube 連結 ---
                        clean_text = re.sub(r'https?://\S+?\.(?:jpg|jpeg|png|gif|webp)', '', clean_text,
                                            flags=re.IGNORECASE)
                        clean_text = re.sub(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)\S+', '',
                                            clean_text)
                        clean_text = re.sub(r'https?://(?:i\.)?imgur\.com/[a-zA-Z0-9]{5,7}', '', clean_text)

                        # 逐行去除前後空白（PTT 常有隱藏的空格行），並過濾掉純空行
                        lines = [line.strip() for line in clean_text.splitlines()]

                        # 方案 A（最緊湊，推薦）：完全不保留多餘空行，網址與文字緊密排列
                        clean_text = "\n".join(line for line in lines if line)

                        # 擷取前 250 字做為預覽，避免文章過長洗版
                        if len(clean_text) > 250:
                            clean_text = clean_text[:250] + "...\n\n(點擊標題閱讀全文)"

                        # --- 關鍵優化 2：提取前 3 張不重複的圖片 ---
                        # 收集標準副檔名圖片與純 imgur 圖片
                        all_img_urls = re.findall(r'https?://[^\s"\'<>]+?\.(?:jpg|jpeg|png|gif|webp)', raw_html,
                                                  re.IGNORECASE)
                        imgur_links = re.findall(r'https?://(?:i\.)?imgur\.com/([a-zA-Z0-9]{5,7})(?!\.\w+)', raw_html)
                        for img_id in imgur_links:
                            all_img_urls.append(f"https://i.imgur.com/{img_id}.jpg")

                        # 保持順序去重
                        seen_images = set()
                        unique_images = []
                        for img in all_img_urls:
                            if img not in seen_images:
                                seen_images.add(img)
                                unique_images.append(img)

                        # 只取前 3 張
                        preview_images = unique_images[:3]

                        # --- 關鍵優化 3：組裝多圖 Embed 清單 ---
                        embeds = []

                        # 主 Embed (包含標題、內文摘要、Footer)
                        main_embed = discord.Embed(
                            title=title,
                            url=raw_ptt_url,
                            description=clean_text if clean_text else "無文字內容",
                            color=0x2C2F33
                        )
                        main_embed.set_footer(text=f"Ptt 批踢踢實業坊  •  {board_name}  •  {post_time}")

                        if preview_images:
                            main_embed.set_image(url=preview_images[0])
                        embeds.append(main_embed)

                        # 第 2、3 張圖片建立為附屬 Embed (必須使用完全相同的 url)
                        for img_url in preview_images[1:]:
                            sub_embed = discord.Embed(url=raw_ptt_url)
                            sub_embed.set_image(url=img_url)
                            embeds.append(sub_embed)

                        # 5. 發送處理 (有 YT 先發純網址，接著一次送出所有 embeds)
                        if first_yt:
                            await message.channel.send(content=first_yt)
                            await message.channel.send(embeds=embeds)
                        else:
                            await message.channel.send(embeds=embeds)

                        # 隱藏使用者發送的原始預覽
                        try:
                            await message.edit(suppress=True)
                        except Exception as e:
                            print(f"無法隱藏原始訊息預覽: {e}")

                else:
                    print(f"PTT 請求失敗，狀態碼: {response.status_code}")

            except Exception as e:
                print(f"處理 PTT 網址時發生錯誤: {e}")

    # ================= 處理巴哈姆特網址 (GNN / 哈啦版 / 小屋) =================
    BAHA_PATTERN = r"(https?://(gnn|forum|home)\.gamer\.com\.tw/[^\s]+)"

    if re.search(BAHA_PATTERN, message.content):
        match = re.search(BAHA_PATTERN, message.content)
        url = match.group(0)

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Cookie': 'BAHAID=discord_bot_preview;'  # 降低被阻擋機率
        }

        try:
            res = requests.get(url, headers=headers)
            res.encoding = 'utf-8'
            soup = BeautifulSoup(res.text, 'html.parser')

            # 初始化通用變數
            title = "無標題"
            raw_content_text = ""
            tags_list = []
            gp_count = "-"
            date_str = ""
            section_name = ""
            target_html_block = ""  # 用於限制只在主文中尋找圖片與YT

            # 1. 根據網址特徵進行路由解析
            if "gnn.gamer.com.tw" in url:
                section_name = "GNN新聞"
                title_tag = soup.find('h1')
                title = title_tag.text.strip() if title_tag else "GNN新聞"

                content_div = soup.find('div', class_='GN-lbox3B')
                target_html_block = str(content_div) if content_div else str(soup)
                raw_content_text = content_div.text if content_div else ""

                date_tag = soup.find('div', class_='GN-lbox3A')
                if date_tag:
                    date_match = re.search(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', date_tag.text)
                    date_str = date_match.group(0) if date_match else ""

                # GNN 標籤通常在文末
                tag_div = soup.find('div', class_='GN-lbox3C')
                if tag_div:
                    tags_list = [f"#{a.text.strip()}" for a in tag_div.find_all('a')]

            elif "forum.gamer.com.tw" in url:
                # 哈啦版需特別抓取第一樓
                first_post = soup.find('section', class_='c-section')
                target_html_block = str(first_post) if first_post else str(soup)

                title_tag = soup.find('h1', class_='c-post__header__title')
                title = title_tag.text.strip() if title_tag else "哈啦版文章"

                # 嘗試抓取看板名稱做為 section
                board_tag = soup.find('a', class_='b-logo')
                section_name = f"哈啦板 - {board_tag.text.strip()}" if board_tag else "哈啦板"

                if first_post:
                    content_div = first_post.find('div', class_='c-post__body')
                    raw_content_text = content_div.text if content_div else ""
                    date_tag = first_post.find('a', class_='edittime')
                    date_str = date_tag.text.strip() if date_tag else ""
                    gp_tag = first_post.find('span', class_='postgp')
                    gp_count = gp_tag.text.strip() if gp_tag else "-"

            elif "home.gamer.com.tw" in url:
                section_name = "小屋創作"
                title_tag = soup.find('h1', class_='TS1')
                title = title_tag.text.strip() if title_tag else "小屋創作"

                content_div = soup.find('div', class_='MSG-list8C')
                target_html_block = str(content_div) if content_div else str(soup)
                raw_content_text = content_div.text if content_div else ""

                date_tag = soup.find('div', class_='ST1')
                if date_tag:
                    date_match = re.search(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', date_tag.text)
                    date_str = date_match.group(0) if date_match else ""

                # 嘗試尋找小屋標籤
                tag_links = soup.find_all('a', href=re.compile(r'tag\.php'))
                tags_list = [f"#{a.text.strip()}" for a in tag_links]

            # 2. 內文淨化與截斷
            # 移除常見連續空白與空行
            lines = [line.strip() for line in raw_content_text.splitlines() if line.strip()]
            clean_text = "\n".join(lines)

            if len(clean_text) > 200:
                clean_text = clean_text[:200] + "...\n\n(點擊標題閱讀全文)"

            # 將標籤串接在內文底部
            if tags_list:
                clean_text += "\n\n" + " ".join(tags_list[:5])  # 最多顯示 5 個標籤

            # 3. 提取圖片 (支援懶加載 data-src) 與 YouTube 連結
            # 利用 BeautifulSoup 再次解析目標區塊找圖，避免正則抓到奇怪的東西
            block_soup = BeautifulSoup(target_html_block, 'html.parser')
            img_urls = []
            for img in block_soup.find_all('img'):
                # 優先抓 data-src，沒有才抓 src
                src = img.get('data-src') or img.get('src') or ""
                # 排除巴哈常見的表情符號與 1x1 佔位圖
                if src and src.startswith('http') and 'emoji' not in src and '1x1.gif' not in src:
                    if src not in img_urls:
                        img_urls.append(src)

            preview_images = img_urls[:3]  # 最多 3 張

            # 尋找 YouTube 連結
            yt_urls = re.findall(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[a-zA-Z0-9_-]+',
                                 target_html_block)
            first_yt = yt_urls[0] if yt_urls else None

            # 4. 組裝 Embed 陣列 (多圖拼貼)
            embeds = []
            main_embed = discord.Embed(
                title=title,
                url=url,
                description=clean_text if clean_text else "無文字內容",
                color=0x00B4D8  # 巴哈姆特藍綠色
            )

            # Footer 格式化
            footer_text = f"巴哈姆特 • {section_name} • 推薦: {gp_count}"
            if date_str:
                footer_text += f" • {date_str}"
            main_embed.set_footer(text=footer_text)

            if preview_images:
                main_embed.set_image(url=preview_images[0])
            embeds.append(main_embed)

            # 附屬圖片 Embed (達成 2~3 張圖並排)
            for img_url in preview_images[1:]:
                sub_embed = discord.Embed(url=url)
                sub_embed.set_image(url=img_url)
                embeds.append(sub_embed)

            # 5. 發送訊息與隱藏原生預覽
            if first_yt:
                await message.channel.send(content=first_yt)

            await message.channel.send(embeds=embeds)

            # 呼叫你之前建立的非同步壓抑函式
            import asyncio
            asyncio.create_task(suppress_embed_safely(message, delay=2.0))

        except Exception as e:
            print(f"解析巴哈姆特網址時發生錯誤: {e}")


# 啟動 Bot，請將引號內替換為你的 Token
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
client.run(DISCORD_TOKEN)