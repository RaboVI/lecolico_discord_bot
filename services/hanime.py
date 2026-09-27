import re
import asyncio
import discord
from bs4 import BeautifulSoup
from curl_cffi import requests
from cachetools import TTLCache

# 建立記憶體快取：最多保留 100 筆資料，每筆快取存活 30 分鐘 (1800 秒)
hanime_cache = TTLCache(maxsize=100, ttl=1800)

# 方案 1：補齊高擬真的完整桌面 Chrome 124 Headers (含 Client Hints 與 Sec 標頭)
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://hanime1.me/",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1"
}

class HanimeHighResView(discord.ui.View):
    """Embed 底部的最高畫質跳轉按鈕"""
    def __init__(self, best_quality_label: str, best_video_url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label=f"🖥️ {best_quality_label}",
            url=best_video_url,
            style=discord.ButtonStyle.link
        ))


def _fetch_hanime_data_sync(url: str) -> dict | None:
    """
    同步爬取與解析 Hanime1 頁面資料（優先讀取 TTLCache 快取）
    """
    if url in hanime_cache:
        print(f"[Hanime1] 命中快取，直接讀取: {url}")
        return hanime_cache[url]

    resp = None
    # 嘗試策略：優先 chrome124，若被擋則輪替 safari17_0 嘗試突破
    impersonate_targets = ["chrome124", "safari17_0"]

    for imp in impersonate_targets:
        try:
            print(f"[Hanime1] 正在向目標請求資料 ({imp}): {url}")
            r = requests.get(url, headers=REQUEST_HEADERS, impersonate=imp, timeout=12)
            if r.status_code == 200:
                resp = r
                break
            else:
                print(f"[Hanime1] 指紋 {imp} 請求失敗，狀態碼: {r.status_code}")
        except Exception as e:
            print(f"[Hanime1] 指紋 {imp} 握手例外: {e}")

    if not resp:
        print("[Hanime1] 所有指紋策略皆失敗，無法取得頁面。")
        return None

    try:
        soup = BeautifulSoup(resp.text, "html.parser")

        # 提取影片畫質清單
        video_tag = soup.find("video", id="player") or soup.find("video")
        sources_list = []

        if video_tag:
            for s in video_tag.find_all("source"):
                src = s.get("src")
                size_attr = s.get("size")
                if src:
                    if size_attr and size_attr.isdigit():
                        sources_list.append((int(size_attr), f"{size_attr}p", src))
                    else:
                        res_match = re.search(r"-(\d{3,4})p\.mp4", src)
                        if res_match:
                            sources_list.append((int(res_match.group(1)), f"{res_match.group(1)}p", src))

            if not sources_list and video_tag.get("src"):
                single_src = video_tag.get("src")
                res_match = re.search(r"-(\d{3,4})p\.mp4", single_src)
                label = f"{res_match.group(1)}p" if res_match else "影片"
                q_num = int(res_match.group(1)) if res_match else 720
                sources_list.append((q_num, label, single_src))

        # 排序畫質：最小畫質優先 (用於播放器)，最高畫質用於按鈕
        lowest_video_src = None
        lowest_quality_label = ""
        highest_video_src = None
        highest_quality_label = ""

        if sources_list:
            sources_list.sort(key=lambda x: x[0])
            lowest_quality_label = sources_list[0][1]
            lowest_video_src = sources_list[0][2]

            highest_quality_label = sources_list[-1][1]
            highest_video_src = sources_list[-1][2]

        # 提取標題
        title = "Hanime1 影片"
        title_h3 = soup.find("h3", id="shareBtn-title")
        if title_h3 and title_h3.text.strip():
            title = title_h3.text.strip()
        elif soup.find("title"):
            title = soup.find("title").text.strip().replace(" - Hanime1.me", "")

        # 提取觀看次數與日期 (中文格式)
        views_str = ""
        date_str = ""
        stats_candidates = soup.find_all(lambda t: t.name in ["div", "span", "p"] and "觀看次數" in t.get_text())
        for tag in stats_candidates:
            raw_text = re.sub(r"[\s\xa0]+", " ", tag.get_text()).strip()
            v_match = re.search(r"觀看次數\s*[:：]?\s*([0-9\.]+(?:萬|億)?(?:次)?)", raw_text)
            if v_match:
                raw_v = v_match.group(1)
                if not raw_v.endswith("次"):
                    raw_v += "次"
                views_str = f"觀看: {raw_v}"

            d_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", raw_text)
            if d_match:
                date_str = f"{d_match.group(1)}/{d_match.group(2)}/{d_match.group(3)}"
                break

        # 提取影片簡介 (限制 80 字)
        description = ""
        desc_div = soup.find("div", class_=re.compile(r"video-caption-text"))
        if desc_div:
            raw_desc = desc_div.get_text(separator="\n").strip()
            clean_lines = [line.strip() for line in raw_desc.splitlines() if line.strip()]
            description = "\n".join(clean_lines)

        if len(description) > 80:
            description = description[:80].rstrip() + "..."

        # 提取封面圖
        cover_image = None
        if video_tag:
            cover_image = video_tag.get("poster") or video_tag.get("data-poster")
        if not cover_image:
            og_img = soup.find("meta", property="og:image")
            if og_img and og_img.get("content"):
                cover_image = og_img.get("content")

        result_data = {
            "title": title,
            "url": url,
            "lowest_video_src": lowest_video_src,
            "lowest_quality_label": lowest_quality_label,
            "highest_video_src": highest_video_src,
            "highest_quality_label": highest_quality_label,
            "views": views_str,
            "date": date_str,
            "description": description,
            "cover_image": cover_image
        }

        # 寫入快取 (保存 30 分鐘)
        hanime_cache[url] = result_data
        return result_data

    except Exception as e:
        print(f"[Hanime1] 解析發生錯誤: {e}")
        return None


async def process_hanime_embed(target_url: str, message: discord.Message, pending_suppress_ids: set):
    """
    非同步處理 Hanime1 預覽卡片發送與原訊息壓抑
    """
    data = await asyncio.to_thread(_fetch_hanime_data_sync, target_url)
    if not data:
        return

    # 組裝 Embed 卡片
    embed = discord.Embed(
        title=data["title"],
        url=data["url"],
        description=data["description"] if data["description"] else None,
        color=0xFF1493
    )

    # Footer 格式化
    footer_parts = ["Hanime1"]
    if data["lowest_quality_label"]:
        footer_parts.append(data["lowest_quality_label"])
    if data["views"]:
        footer_parts.append(data["views"])
    if data["date"]:
        footer_parts.append(data["date"])
    embed.set_footer(text=" • ".join(footer_parts))

    # 封面圖
    if data["cover_image"] and data["cover_image"].startswith("http"):
        embed.set_image(url=data["cover_image"])

    # 最高畫質按鈕
    view = None
    if data["highest_video_src"] and data["highest_quality_label"]:
        view = HanimeHighResView(
            best_quality_label=data["highest_quality_label"],
            best_video_url=data["highest_video_src"]
        )

    # 1. 發送卡片
    if view:
        await message.channel.send(embed=embed, view=view)
    else:
        await message.channel.send(embed=embed)

    # 2. 發送最小畫質超連結播放器
    if data["lowest_video_src"]:
        await message.channel.send(content=f"[Hanime1.me]({data['lowest_video_src']})")

    # 3. 壓抑原訊息預覽
    pending_suppress_ids.add(message.id)
    try:
        await message.edit(suppress=True)
    except Exception as e:
        print(f"[Hanime1] 無法隱藏原始預覽: {e}")