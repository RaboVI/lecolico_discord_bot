/**
 * LECO Bot - Facebook 影片專用 Cloudflare Worker (zusakvi.cc)
 * 抓取 FB Reels / 影片真實 MP4 + ActivityPub (Mastodon) 統一卡片偽裝
 *
 * Discord 預覽流程：
 *   1. Discordbot 造訪 zusakvi.cc/reel/xxx ➔ 回傳含 og 標籤與 activity+json 誘餌連結的 HTML
 *   2. Discordbot 再讀取 /users/<帳號>/statuses/<編碼後路徑> ➔ 回傳 Mastodon Status JSON
 *      （頭像 + 用戶名稱、內文、原生播放器、Footer「Facebook • ❤️ 數量 • 時間」）
 *   3. 若第 2 步失敗，Discord 會退回第 1 步的 og:video 陽春播放器（與舊版相同），不會更糟
 *
 * 除錯：網址加上 ?__debug=1 可用瀏覽器直接看 JSON 診斷報告
 *       也可以直接用瀏覽器開報告裡的 activityUrl，看 Discord 拿到的卡片資料
 */

// ================= 可調整設定 =================

// 卡片內文最多顯示幾個字（超過會加上「…」），與 Bot 圖文貼文的 50 字規則一致
const DESC_MAX_LENGTH = 50;

// 找不到含聲音的影片時，是否仍輸出「無聲影片」
//   true  ＝ 輸出無聲影片（至少畫面看得到）
//   false ＝ 跳轉回 Facebook，交給機器人的看門狗用 yt-dlp 下載有聲版本
const ALLOW_SILENT_FALLBACK = true;

// 抓到的貼文資料在 Cloudflare 快取保存幾秒（Discord 第二次來讀卡片時就不用再爬一次 FB）
const CACHE_TTL_SECONDS = 600;

// 卡片左側邊框顏色（FB 藍）
const THEME_COLOR = "#1877F2";

// 抓不到作者頭像時使用的預設頭像（藍底白色 f 的 PNG 圖，直接內嵌在程式裡）
// Discord 的 Mastodon 卡片一定要有頭像，沒有的話會整張退回舊版 og 卡片
const DEFAULT_AVATAR_PNG = "iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAIAAABMXPacAAABTUlEQVR42u3dMQ6CUBBFUTC09rpSXYI7tdcN2FnQmRj+fzPn9hbMYdBA1PVyey0a18kIAAAQAAACAEAAAAgAAAEAIAAABACAAAAQAAACAED/b+twkM/H+deXXO9vAEcP3Qb0Gn1BgKzRV3sTTpx+kQ0IHX2RDYiefjxA+vSzAQpMPxigxvQXtyIAtD79IwEqTd8lCEDv098GjG/rc+5/b/FPtUYtHsjsnq4c9rDFJWi6cbcDmHz6xQHmn75PQQB6n/42AACAqgeWctPCBgAAIAAABACAAAAQAAACAEAAerTO8P8Bw28dD3x8ZgNcggAIAAABACAAAAQAgAAAEIAerSl/ZzvzT0/aAJcgAQAgAAAEAIAAABAAAAIAQAAACAAAAQAgAAAEAIAAABAAAAIAQAAAANDAYr4hYwMEAIAAABAAAAIAQAAACAAAAUjuA/DaMO8JjxtkAAAAAElFTkSuQmCC";

// 一般 Chrome 瀏覽器的請求標頭：FB 會回傳含音軌的完整 MP4 網址（browser_native_sd_url）
const BROWSER_HEADERS = {
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
  "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
  "Sec-Ch-Ua": "\"Not)A;Brand\";v=\"8\", \"Chromium\";v=\"146\"",
  "Sec-Ch-Ua-Mobile": "?0",
  "Sec-Ch-Ua-Platform": "\"Windows\"",
  "Sec-Fetch-Dest": "document",
  "Sec-Fetch-Mode": "navigate",
  "Sec-Fetch-Site": "none",
  "Sec-Fetch-User": "?1",
  "Upgrade-Insecure-Requests": "1"
};

// FB 官方爬蟲的請求標頭：比較不會遇到登入牆，但影片通常只給無聲的 DASH 影像軌
const CRAWLER_HEADERS = {
  "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
  "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
};


// ================= 工具函式 =================

// 反覆還原 JSON / JS 字串的跳脫字元，直到不再變化
function deepUnescape(str) {
  let prev = null;
  let cur = str;
  for (let i = 0; i < 4 && cur !== prev; i++) {
    prev = cur;
    cur = cur
      .replace(/\\+\//g, "/")
      .replace(/\\+"/g, '"')
      .replace(/\\+u0025/gi, "%")
      .replace(/\\+u0026/gi, "&")
      .replace(/\\+u003d/gi, "=")
      .replace(/\\+u002f/gi, "/")
      .replace(/\\+u003c/gi, "<")
      .replace(/\\+u003e/gi, ">");
  }
  return cur;
}

// 還原 JSON 字串裡的 \uXXXX（中文、emoji 名稱會長這樣）與 \n
function decodeJsonText(str) {
  if (!str) return "";
  return str
    .replace(/\\+u([0-9a-fA-F]{4})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)))
    .replace(/\\+n/g, "\n")
    .replace(/\\+r/g, "")
    .replace(/\\+t/g, " ");
}

// HTML 實體解碼（&amp; &#x842c; 之類）
function decodeHtml(str) {
  if (!str) return "";
  return str
    .replace(/&#x([0-9a-fA-F]+);/g, (_, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, dec) => String.fromCodePoint(parseInt(dec, 10)))
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#039;/g, "'");
}

// 輸出到 HTML 前轉義，避免標題或網址含有 " 時把 HTML 弄壞
function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// 把 FB 路徑（含中文也可以）編碼成網址安全的 ID，放進 /statuses/<ID>
function encodePathId(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// 把 /statuses/<ID> 還原成 FB 路徑
function decodePathId(id) {
  const b64 = id.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64 + "===".slice((b64.length + 3) % 4));
  const bytes = Uint8Array.from(bin, c => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

// 讀取 <meta property="og:xxx" content="...">
function getMeta(html, prop) {
  if (!html) return "";
  const re = new RegExp(`<meta\\s+(?:property|name)="${prop}"\\s+content="([^"]*)"`, "i");
  const m = html.match(re);
  return m ? decodeHtml(m[1]) : "";
}

// 找出所有符合的片段，回傳「離目標位置最近」的那一個 { value, index, match }
// 用來從整頁資料中挑出屬於這支影片的作者、讚數、時間，而不是推薦影片的
function nearestMatch(html, regex, positions) {
  let best = null;
  let bestDist = Infinity;
  for (const m of html.matchAll(regex)) {
    const dist = positions.length
      ? Math.min(...positions.map(p => Math.abs(p - m.index)))
      : m.index;
    if (dist < bestDist) {
      bestDist = dist;
      best = { value: m[1], index: m.index, match: m };
    }
  }
  return best;
}

// 第一個成功的 nearestMatch
function nearestOf(html, regexList, positions) {
  for (const re of regexList) {
    const found = nearestMatch(html, re, positions);
    if (found) return found;
  }
  return null;
}

// 找出影片 ID 在頁面中出現的所有位置
function findIdPositions(html, id) {
  const list = [];
  if (!id) return list;
  let pos = html.indexOf(id);
  while (pos !== -1 && list.length < 200) {
    list.push(pos);
    pos = html.indexOf(id, pos + 1);
  }
  return list;
}

// 「1.2K」「3.4萬」「1,234」轉成數字
function parseCount(text) {
  if (!text) return null;
  const m = String(text).replace(/,/g, "").match(/([\d.]+)\s*([KkMmBb萬万億亿]?)/);
  if (!m) return null;
  let n = parseFloat(m[1]);
  if (isNaN(n)) return null;
  const unit = m[2];
  if (/[Kk]/.test(unit)) n *= 1e3;
  else if (/[Mm]/.test(unit)) n *= 1e6;
  else if (/[Bb]/.test(unit)) n *= 1e9;
  else if (/[萬万]/.test(unit)) n *= 1e4;
  else if (/[億亿]/.test(unit)) n *= 1e8;
  return Math.round(n);
}

// 判斷一段文字是不是「觀看次數 / 心情數」之類的統計
function isStatsText(s) {
  return /觀看|心情|views|reactions|likes|comments|則留言|次分享|shares/i.test(s);
}

// 整理內文：拆掉統計數字與「Facebook」字樣、逐行 strip 並移除空行、限制字數
function formatCaption(raw, fromOgTag) {
  if (!raw) return "";
  let text = raw;
  // og:description 有時長這樣：「12萬次觀看 · 3,456 個心情 | 真正的內文 | 粉專名稱」
  if (fromOgTag && text.includes(" | ")) {
    const parts = text.split(" | ").map(p => p.trim())
      .filter(p => p && p.toLowerCase() !== "facebook" && !isStatsText(p));
    text = parts.sort((a, b) => b.length - a.length)[0] || "";
  }
  const lines = text.split(/\r?\n/).map(l => l.trim()).filter(l => l.length > 0);
  let clean = lines.join("\n");
  const chars = Array.from(clean);
  if (chars.length > DESC_MAX_LENGTH) {
    clean = chars.slice(0, DESC_MAX_LENGTH).join("").trimEnd() + "…";
  }
  return clean;
}

// 從 FB 影片網址的 efg 參數讀出編碼標籤，例如 dash_h264-basic-gen2_360p
function getEncodeTag(videoUrl) {
  try {
    const efg = new URL(decodeHtml(videoUrl)).searchParams.get("efg");
    if (!efg) return null;
    const b64 = efg.replace(/-/g, "+").replace(/_/g, "/");
    const json = JSON.parse(atob(b64 + "===".slice((b64.length + 3) % 4)));
    return json.vencode_tag || null;
  } catch (e) {
    return null;
  }
}

// 判斷影片是否有聲音：dash_ 開頭的是純影像軌（聲音在另一條軌），其他視為完整影片
function hasAudioTrack(videoUrl) {
  const tag = getEncodeTag(videoUrl);
  if (!tag) return true;               // 沒有標籤時（例如 browser_native 網址）多半是完整 MP4
  if (/audio/i.test(tag)) return false; // 純音軌
  return !/^dash_/i.test(tag);
}

// 找出所有候選影片網址，回傳：與網址 ID 相關且有聲音的 > 有聲音的 > 與 ID 相關的 > 第一個
function findVideoUrl(html, requestedId) {
  const patterns = [
    /"browser_native_sd_url"\s*:\s*"(https:[^"]+?)"/g,
    /"browser_native_hd_url"\s*:\s*"(https:[^"]+?)"/g,
    /"playable_url"\s*:\s*"(https:[^"]+?)"/g,
    /"playable_url_quality_hd"\s*:\s*"(https:[^"]+?)"/g,
    /"progressive_url"\s*:\s*"(https:[^"]+?)"/g,
    /\bsd_src\s*:\s*"(https:[^"]+?)"/g,
    /\bhd_src\s*:\s*"(https:[^"]+?)"/g,
    /(https:\/\/[^"'\s<>\\]+?\.fbcdn\.net\/[^"'\s<>\\]+?\.mp4[^"'\s<>\\]*)/g,
    /<video[^>]+src=["'](https:[^"']+)["']/gi
  ];

  const seen = new Set();
  const candidates = [];
  for (const re of patterns) {
    for (const m of html.matchAll(re)) {
      const u = m[1];
      if (u.includes("lookaside.fbsbx.com") || seen.has(u)) continue;
      seen.add(u);
      candidates.push({ url: u, index: m.index, audio: hasAudioTrack(u), nearId: false });
    }
  }
  if (candidates.length === 0) return null;

  if (requestedId) {
    const idPositions = findIdPositions(html, requestedId);
    for (const c of candidates) {
      c.nearId = idPositions.some(p => Math.abs(p - c.index) < 20000);
    }
  }

  return candidates.find(c => c.nearId && c.audio) ||
         candidates.find(c => c.audio && !requestedId) ||
         candidates.find(c => c.nearId) ||
         candidates[0];
}

// 從頁面挖出作者、頭像、內文、讚數、發文時間、縮圖、影片尺寸
function extractPostInfo(dataHtml, metaHtml, requestedId, rawPath, videoIndex) {
  // 以影片 ID 出現的位置（找不到就用影片網址的位置）當作「這支影片」的基準點
  let positions = findIdPositions(dataHtml, requestedId);
  if (!positions.length && typeof videoIndex === "number") positions = [videoIndex];

  // --- 作者名稱 ---
  const ownerMatch = nearestOf(dataHtml, [
    /"video_owner":\{"__typename":"(?:User|Page)"[^{}]*?"name":"([^"]{1,100})"/g,
    /"owner":\{"__typename":"(?:User|Page)"[^{}]*?"name":"([^"]{1,100})"/g,
    /"owning_profile":\{"__typename":"(?:User|Page)"[^{}]*?"name":"([^"]{1,100})"/g,
    /"actors":\[\{"__typename":"(?:User|Page)"[^{}]*?"name":"([^"]{1,100})"/g
  ], positions);
  let authorName = ownerMatch ? decodeJsonText(ownerMatch.value) : "";

  if (!authorName) {
    // 備案：og:title 常見格式「影片說明 | 作者名稱」或「... | By 作者名稱」
    const ogTitle = getMeta(metaHtml, "og:title");
    const parts = ogTitle.split("|").map(p => p.trim())
      .filter(p => p && p.toLowerCase() !== "facebook" && !isStatsText(p));
    const byPart = parts.find(p => /^(?:By|作者[:：]?)\s+/i.test(p));
    if (byPart) authorName = byPart.replace(/^(?:By|作者[:：]?)\s+/i, "");
    else if (parts.length >= 2) authorName = parts[parts.length - 1];
  }
  if (!authorName) authorName = "Facebook";

  // --- 帳號（@xxx）：網址裡的粉專名稱 > 作者資料裡的主頁網址 ---
  const badHandles = /^(?:reel|reels|watch|videos|share|profile\.php|people|story\.php|permalink\.php|groups|pages)$/i;
  let handle = "";
  const pathHandle = rawPath.match(/^\/([A-Za-z0-9.]{3,})\/(?:videos|posts|reels?)\//);
  if (pathHandle && !badHandles.test(pathHandle[1])) handle = pathHandle[1];
  if (!handle) {
    const ownerUrl = nearestOf(dataHtml, [
      /"(?:video_owner|owner|owning_profile)":\{"__typename":"(?:User|Page)"[^{}]*?"url":"https:\/\/www\.facebook\.com\/([A-Za-z0-9.]{3,})"/g
    ], ownerMatch ? [ownerMatch.index] : positions);
    if (ownerUrl && !badHandles.test(ownerUrl.value)) handle = ownerUrl.value;
  }

  // --- 作者主頁網址（沒有自訂帳號的人會是 facebook.com/people/名稱/數字ID/，一樣可以點）---
  const profileMatch = nearestOf(dataHtml, [
    /"(?:video_owner|owner|owning_profile)":\{"__typename":"(?:User|Page)"[^{}]*?"url":"(https:\/\/www\.facebook\.com\/[^"]+)"/g
  ], ownerMatch ? [ownerMatch.index] : positions);
  let profileUrl = profileMatch ? profileMatch.value : "";
  if (!profileUrl && handle) profileUrl = `https://www.facebook.com/${handle}`;

  // --- 頭像：離作者資料最近的大頭貼 ---
  const avatarMatch = nearestOf(dataHtml, [
    /"displayPicture":\{[^{}]{0,80}?"uri":"(https:[^"]+)"/g,
    /"profile_picture":\{"uri":"(https:[^"]+)"/g,
    /"profilePicLarge":\{"uri":"(https:[^"]+)"/g,
    /"profilePic\w*":\{"uri":"(https:[^"]+)"/g,
    /"profilePicture":\{"uri":"(https:[^"]+)"/g,
    /"profile_pic_uri":"(https:[^"]+)"/g
  ], ownerMatch ? [ownerMatch.index] : positions);
  let avatar = avatarMatch ? decodeHtml(avatarMatch.value) : "";
  // 備案：用 FB 公開的大頭貼網址（粉專都適用，會自動跳轉到真正的圖片）
  if (!avatar && info_handleOk(handle)) avatar = `https://graph.facebook.com/${handle}/picture?type=large`;

  // --- 內文：貼文本身的 message.text > og:description ---
  const msgMatch = nearestOf(dataHtml, [
    /"message":\{"text":"((?:[^"\\]|\\.){1,3000})"/g,
    /"savable_description":\{"text":"((?:[^"\\]|\\.){1,3000})"/g
  ], positions);
  const caption = msgMatch
    ? formatCaption(decodeJsonText(msgMatch.value), false)
    : formatCaption(getMeta(metaHtml, "og:description"), true);

  // --- 讚數（心情數）---
  let likes = null;
  const likeMatch = nearestOf(dataHtml, [
    /"reaction_count":\{"count":(\d+)/g,
    /"unified_reactors":\{"count":(\d+)/g,
    /"reactors":\{"count":(\d+)/g,
    /"i18n_reaction_count":"([^"]+)"/g
  ], positions);
  if (likeMatch) likes = parseCount(likeMatch.value);
  if (likes === null) {
    // 備案：og:title / og:description 裡的「3,456 個心情」「1.2K reactions」
    const statsText = getMeta(metaHtml, "og:title") + " " + getMeta(metaHtml, "og:description");
    const m = statsText.match(/([\d.,]+\s*[KkMm萬万]?)\s*(?:個心情|心情|reactions)/i);
    if (m) likes = parseCount(m[1]);
  }

  // --- 發文時間 ---
  const timeMatch = nearestOf(dataHtml, [
    /"(?:creation_time|publish_time|created_time)":(\d{10})\b/g
  ], positions);
  const createdAt = timeMatch
    ? new Date(parseInt(timeMatch.value, 10) * 1000).toISOString()
    : new Date().toISOString();

  // --- 縮圖 ---
  let preview = getMeta(metaHtml, "og:image");
  if (!preview) {
    const thumbMatch = nearestOf(dataHtml, [
      /"preferred_thumbnail":\{"image":\{"uri":"(https:[^"]+)"/g,
      /"thumbnailImage":\{"uri":"(https:[^"]+)"/g
    ], positions);
    if (thumbMatch) preview = decodeHtml(thumbMatch.value);
  }

  // --- 影片尺寸（找不到時：Reels 用直式 720x1280，其他用橫式 1280x720）---
  const isReel = /\/reels?\//.test(rawPath);
  let width = isReel ? 720 : 1280;
  let height = isReel ? 1280 : 720;
  const sizeMatch = nearestMatch(dataHtml, /"(?:original_)?width":(\d{3,4}),"(?:original_)?height":(\d{3,4})/g, positions);
  if (sizeMatch) {
    const w = parseInt(sizeMatch.match[1], 10);
    const h = parseInt(sizeMatch.match[2], 10);
    if (w > 0 && h > 0) { width = w; height = h; }
  }

  return { authorName, handle, profileUrl, avatar, caption, likes, createdAt, preview, width, height };
}


// ================= 抓取 Facebook =================

// 解析網址、抓取 FB 頁面，回傳影片與貼文資訊
async function scrapeFacebook(rawPath, rawSearch, debugInfo) {
  // 步驟 A：若路徑是 /share/ 短跳轉，先解析出真實 reel/video 路徑
  if (rawPath.includes("/share/")) {
    try {
      const probeRes = await fetch(`https://www.facebook.com${rawPath}${rawSearch}`, {
        headers: CRAWLER_HEADERS,
        redirect: "manual"
      });
      const loc = probeRes.headers.get("Location") || "";
      debugInfo.shareProbe = { status: probeRes.status, location: loc || null };

      if (loc) {
        const locUrl = new URL(loc, "https://www.facebook.com");
        if (!locUrl.pathname.includes("/login") && !locUrl.pathname.includes("/share/")) {
          rawPath = locUrl.pathname;
          rawSearch = locUrl.search;
        } else if (loc.includes("next=") || loc.includes("u=")) {
          const matchNext = loc.match(/[?&](?:next|u)=([^&]+)/);
          if (matchNext) {
            const decoded = decodeURIComponent(matchNext[1]);
            const nextUrl = new URL(decoded, "https://www.facebook.com");
            if (!nextUrl.pathname.includes("/login")) {
              rawPath = nextUrl.pathname;
              rawSearch = nextUrl.search;
            }
          }
        }
      }
    } catch (e) {
      debugInfo.shareProbe = { error: String(e) };
    }
  }
  debugInfo.resolvedPath = rawPath + rawSearch;

  // 從網址取出影片 ID，用來避免抓到頁面裡的「推薦影片」
  const idMatch = rawPath.match(/\/(?:reels?|videos)\/(\d+)/) || rawSearch.match(/[?&]v=(\d+)/);
  const requestedId = idMatch ? idMatch[1] : null;
  debugInfo.requestedId = requestedId;

  // 步驟 B：先用瀏覽器身分抓（有機會拿到有聲 MP4），再用 FB 爬蟲身分抓（較穩定）
  const attemptsPlan = [
    { label: "browser-www", url: `https://www.facebook.com${rawPath}${rawSearch}`, headers: BROWSER_HEADERS },
    { label: "crawler-m",   url: `https://m.facebook.com${rawPath}${rawSearch}`,   headers: CRAWLER_HEADERS },
    { label: "crawler-www", url: `https://www.facebook.com${rawPath}${rawSearch}`, headers: CRAWLER_HEADERS }
  ];

  let fbStatus = 200;
  let isBlocked = false;
  let bestVideo = null;   // 目前找到最好的影片
  let bestHtml = "";      // 找到最好影片的那一頁（已還原跳脫字元），拿來挖作者、讚數等資料
  let metaHtml = "";      // 拿來抓 og 標籤的頁面（第一個有 og:title 的）

  for (const plan of attemptsPlan) {
    let fbResponse, rawHtml;
    try {
      fbResponse = await fetch(plan.url, { headers: plan.headers, redirect: "follow" });
      rawHtml = await fbResponse.text();
    } catch (e) {
      debugInfo.attempts.push({ label: plan.label, targetUrl: plan.url, error: String(e) });
      continue;
    }

    fbStatus = fbResponse.status;
    const finalUrl = fbResponse.url || "";
    const hasLoginForm = /id="login_form"|id="loginbutton"/.test(rawHtml);
    const currentBlocked = fbStatus !== 200 || finalUrl.includes("/login") || hasLoginForm;

    const ogTitle = getMeta(rawHtml, "og:title");
    if (!metaHtml && ogTitle) metaHtml = rawHtml;

    const unescapedHtml = deepUnescape(rawHtml);
    const found = findVideoUrl(unescapedHtml, requestedId);

    debugInfo.attempts.push({
      label: plan.label,
      targetUrl: plan.url,
      status: fbResponse.status,
      finalUrl,
      htmlLength: rawHtml.length,
      ogTitle: ogTitle || null,
      hasLoginForm,
      hasRequestedId: requestedId ? unescapedHtml.includes(requestedId) : null,
      videoFound: !!found,
      videoHasAudio: found ? found.audio : null,
      videoEncodeTag: found ? getEncodeTag(found.url) : null
    });

    if (found) {
      // 有聲音的優先；沒聲音的先記著當備案
      if (!bestVideo || (found.audio && !bestVideo.audio)) {
        bestVideo = found;
        bestHtml = unescapedHtml;
      }
      if (found.audio) { isBlocked = false; break; }
    } else {
      isBlocked = currentBlocked;
    }
  }

  // 沒有聲音又不允許無聲備案時，當作沒找到影片
  let videoUrl = null;
  if (bestVideo && (bestVideo.audio || ALLOW_SILENT_FALLBACK)) {
    videoUrl = decodeHtml(bestVideo.url);
  }

  const info = videoUrl
    ? extractPostInfo(bestHtml, metaHtml, requestedId, rawPath, bestVideo.index)
    : null;

  // 除錯模式：列出頁面裡可能是頭像 / 作者主頁的欄位，方便之後調整抓取規則
  if (debugInfo.wantCandidates && bestHtml) {
    debugInfo.avatarCandidates = listCandidates(bestHtml,
      /"(\w*(?:[Pp]rofile|[Pp]ic|[Aa]vatar)\w*)":\{[^{}]{0,40}?"uri":"(https:[^"]{0,120})/g, requestedId);
    debugInfo.ownerCandidates = listCandidates(bestHtml,
      /"(\w+)":\{"__typename":"(?:User|Page)"[^{}]{0,300}?"name":"([^"]{1,60})"/g, requestedId);
    debugInfo.ownerSnippets = listSnippets(bestHtml, '"video_owner":', 2, 500);
    debugInfo.profileUrlCandidates = listCandidates(bestHtml,
      /"(\w*url\w*)":"(https:\/\/www\.facebook\.com\/(?!reel|watch|share|photo|story|permalink|groups|hashtag)[A-Za-z0-9.]{3,}\/?)"/g, requestedId);
  }

  return {
    rawPath,
    rawSearch,
    requestedId,
    fbStatus,
    isBlocked,
    videoUrl,
    videoHasAudio: bestVideo ? bestVideo.audio : null,
    videoEncodeTag: bestVideo ? getEncodeTag(bestVideo.url) : null,
    info
  };
}

// 除錯用：列出符合規則的「欄位名稱 + 值」，依照離影片 ID 的距離排序，最多 8 筆
function listCandidates(html, regex, requestedId) {
  const positions = findIdPositions(html, requestedId);
  const seen = new Set();
  const list = [];
  for (const m of html.matchAll(regex)) {
    const key = m[1] + "|" + m[2];
    if (seen.has(key)) continue;
    seen.add(key);
    const dist = positions.length ? Math.min(...positions.map(p => Math.abs(p - m.index))) : m.index;
    list.push({ field: m[1], value: decodeJsonText(m[2]), distance: dist });
  }
  return list.sort((a, b) => a.distance - b.distance).slice(0, 8);
}

// 除錯用：擷取關鍵字後面的原始片段
function listSnippets(html, keyword, max, length) {
  const list = [];
  let pos = html.indexOf(keyword);
  while (pos !== -1 && list.length < max) {
    list.push(html.slice(pos, pos + length));
    pos = html.indexOf(keyword, pos + keyword.length);
  }
  return list;
}

// Cloudflare 快取：存放 / 讀取抓到的結果
function cacheKey(host, statusId) {
  return new Request(`https://${host}/__cache/fb/${statusId}`);
}

async function readCache(host, statusId) {
  try {
    const res = await caches.default.match(cacheKey(host, statusId));
    return res ? await res.json() : null;
  } catch (e) {
    return null;
  }
}

async function writeCache(host, statusId, data) {
  try {
    await caches.default.put(cacheKey(host, statusId), new Response(JSON.stringify(data), {
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": `public, max-age=${CACHE_TTL_SECONDS}`
      }
    }));
  } catch (e) {
    // 快取失敗不影響主流程
  }
}

// 卡片底部 Footer 前半段：「Facebook • ❤️ 1,234」
function buildFooterSiteName(likes) {
  return likes !== null && likes !== undefined
    ? `Facebook • ❤️ ${Number(likes).toLocaleString("en-US")}`
    : "Facebook";
}

// 帳號是否可用來組大頭貼網址
function info_handleOk(handle) {
  return !!handle && /^[A-Za-z0-9.]+$/.test(handle);
}

// 卡片網址 /users/<名稱>/ 裡的名稱：有自訂帳號時用真正的帳號，沒有時用作者名稱
// （Discord 會在作者名稱後面顯示「(@名稱@www.facebook.com)」，這是 Mastodon 卡片固定格式，無法拿掉）
function mastodonUsername(info) {
  const name = (info && (info.handle || info.authorName)) || "facebook";
  return encodeURIComponent(name);
}

// 「作者名稱 (@帳號)」，沒有帳號時只顯示名稱
function buildDisplayName(info) {
  return info.handle ? `${info.authorName} (@${info.handle})` : info.authorName;
}

function defaultAvatarResponse() {
  const bytes = Uint8Array.from(atob(DEFAULT_AVATAR_PNG), c => c.charCodeAt(0));
  return new Response(bytes, {
    headers: {
      "Content-Type": "image/png",
      "Access-Control-Allow-Origin": "*",
      "Cross-Origin-Resource-Policy": "cross-origin",
      "Cache-Control": "public, max-age=86400"
    }
  });
}

function proxied(host, imgUrl) {
  return imgUrl ? `https://${host}/proxy?url=${encodeURIComponent(imgUrl)}` : "";
}

// 組出 Mastodon Status JSON（Discord 用這份資料畫出頭像、內文、播放器、Footer）
function buildMastodonStatus(host, statusId, result) {
  const info = result.info;
  const postUrl = `https://www.facebook.com${result.rawPath}${result.rawSearch}`;
  const accountId = mastodonUsername(info);
  const accountUrl = info.profileUrl || postUrl;
  // Discord 會在名稱後面自動加上「(@帳號@網域)」，但名稱裡已經含有帳號時就不會加
  // 沒有真正帳號的人，帳號欄位直接填作者名稱，讓 Discord 只顯示名稱，不出現無意義的 @facebook
  const acct = info.handle || info.authorName;
  const avatar = info.avatar ? proxied(host, info.avatar) : `https://${host}/default-avatar.png`;
  const htmlContent = info.caption
    ? `<p>${escapeHtml(info.caption).replace(/\n/g, "<br/>")}</p>`
    : "";

  return {
    id: statusId,
    url: postUrl,
    uri: postUrl,
    created_at: info.createdAt,
    content: htmlContent,
    spoiler_text: "",
    language: null,
    visibility: "public",
    application: { name: "Facebook", website: "https://www.facebook.com" },
    media_attachments: [
      {
        id: result.requestedId || statusId,
        type: "video",
        url: result.videoUrl,
        preview_url: info.preview ? proxied(host, info.preview) : null,
        remote_url: null,
        meta: {
          original: { width: info.width, height: info.height }
        }
      }
    ],
    account: {
      id: accountId,
      display_name: buildDisplayName(info),
      username: acct,
      acct: acct,
      url: accountUrl,
      uri: accountUrl,
      created_at: info.createdAt,
      locked: false,
      bot: false,
      discoverable: true,
      indexable: false,
      group: false,
      avatar: avatar,
      avatar_static: avatar,
      header: "",
      header_static: "",
      followers_count: 0,
      following_count: 0,
      statuses_count: 0,
      hide_collections: false,
      noindex: false,
      emojis: [],
      roles: [],
      fields: []
    },
    mentions: [],
    tags: [],
    emojis: [],
    card: null,
    poll: null
  };
}

function newDebugInfo(request, rawPath, rawSearch) {
  return {
    requestPath: rawPath + rawSearch,
    colo: (request.cf && request.cf.colo) || null,
    shareProbe: null,
    resolvedPath: null,
    requestedId: null,
    attempts: [],
    videoMatch: null,
    videoHasAudio: null,
    videoEncodeTag: null,
    postInfo: null,
    wantCandidates: false,
    activityUrl: null,
    error: null
  };
}


// ================= 主程式 =================

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const host = url.host;
    const userAgent = (request.headers.get("user-agent") || "").toLowerCase();
    const acceptHeader = (request.headers.get("accept") || "").toLowerCase();

    // 記錄每個請求，方便在 Cloudflare 後台 Logs 或 `npx wrangler tail` 看 Discord 來讀了哪些網址
    console.log(JSON.stringify({ path: url.pathname + url.search, ua: userAgent.slice(0, 80), accept: acceptHeader.slice(0, 80) }));

    // 【除錯模式】網址加上 ?__debug=1 就會回傳 JSON 診斷報告（不檢查 UA，可直接用瀏覽器開）
    const debug = url.searchParams.has("__debug");
    url.searchParams.delete("__debug");

    let rawPath = url.pathname;
    let rawSearch = url.search;

    // 【路由 0】預設頭像
    if (rawPath === "/default-avatar.png") {
      return defaultAvatarResponse();
    }

    // 【路由 1】圖片代理：加上跨來源標頭，解決 FB 頭像 / 縮圖防盜鏈
    if (rawPath === "/proxy") {
      const targetImg = url.searchParams.get("url");
      if (!targetImg || !/^https:\/\/[^/]*(?:fbcdn\.net|facebook\.com|fbsbx\.com)\//.test(targetImg)) {
        return new Response("Bad url", { status: 400 });
      }
      try {
        const imgRes = await fetch(targetImg, {
          headers: { "User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)" }
        });
        // FB 有時回傳的不是圖片（例如個人帳號的大頭貼需要登入），這時改用預設頭像，避免卡片出現破圖
        const contentType = imgRes.headers.get("content-type") || "";
        if (!imgRes.ok || !contentType.startsWith("image/")) {
          return defaultAvatarResponse();
        }
        const newHeaders = new Headers(imgRes.headers);
        newHeaders.set("Access-Control-Allow-Origin", "*");
        newHeaders.set("Cross-Origin-Resource-Policy", "cross-origin");
        newHeaders.set("Cache-Control", "public, max-age=86400");
        return new Response(imgRes.body, { status: imgRes.status, headers: newHeaders });
      } catch (e) {
        return defaultAvatarResponse();
      }
    }

    // 【路由 2】oEmbed：提供卡片的 provider 名稱
    if (rawPath === "/oembed") {
      const site = url.searchParams.get("site") || "Facebook";
      return new Response(JSON.stringify({
        provider_name: site,
        provider_url: "https://www.facebook.com",
        type: "video",
        version: "1.0"
      }), { headers: { "Content-Type": "application/json; charset=utf-8" } });
    }

    // 【路由 3】Mastodon Status JSON：Discord 第二次來讀卡片資料
    //   支援三種讀法：/users/<帳號>/statuses/<ID>、/api/v1/statuses/<ID>、或直接用影片網址 + Accept: activity+json
    const userStatusMatch = rawPath.match(/^\/users\/[^/]+\/statuses\/([A-Za-z0-9_-]+)/);
    const apiStatusMatch = rawPath.match(/^\/api\/v1\/statuses\/([A-Za-z0-9_-]+)/);
    const wantsActivity = !debug && acceptHeader.includes("activity+json");
    if (userStatusMatch || apiStatusMatch || wantsActivity) {
      const statusId = userStatusMatch || apiStatusMatch
        ? (userStatusMatch || apiStatusMatch)[1]
        : encodePathId(rawPath + rawSearch);
      try {
        let result = await readCache(host, statusId);
        if (!result) {
          const decoded = new URL(decodePathId(statusId), "https://www.facebook.com");
          const debugInfo = newDebugInfo(request, decoded.pathname, decoded.search);
          result = await scrapeFacebook(decoded.pathname, decoded.search, debugInfo);
          if (result.videoUrl) ctx.waitUntil(writeCache(host, statusId, result));
        }
        if (!result.videoUrl || !result.info) return new Response("Not Found", { status: 404 });

        return new Response(JSON.stringify(buildMastodonStatus(host, statusId, result)), {
          headers: {
            "Content-Type": "application/activity+json; charset=utf-8",
            "Cache-Control": "public, max-age=3600"
          }
        });
      } catch (e) {
        return new Response("Not Found", { status: 404 });
      }
    }

    // 【路由 4】一般使用者點擊：直接跳轉回 Facebook（除錯模式時略過）
    if (!debug && !userAgent.includes("discordbot")) {
      return Response.redirect(`https://www.facebook.com${rawPath}${rawSearch}`, 302);
    }

    // 【路由 5】Discord 官方爬蟲造訪影片網址
    const debugInfo = newDebugInfo(request, rawPath, rawSearch);
    debugInfo.wantCandidates = debug;
    try {
      const result = await scrapeFacebook(rawPath, rawSearch, debugInfo);
      rawPath = result.rawPath;
      rawSearch = result.rawSearch;

      const statusId = encodePathId(rawPath + rawSearch);
      const info = result.info;
      const activityUrl = `https://${host}/users/${mastodonUsername(info)}/statuses/${statusId}`;

      // 除錯模式：回傳 JSON 診斷報告
      if (debug) {
        debugInfo.videoMatch = result.videoUrl;
        debugInfo.videoHasAudio = result.videoHasAudio;
        debugInfo.videoEncodeTag = result.videoEncodeTag;
        debugInfo.postInfo = info;
        debugInfo.activityUrl = result.videoUrl ? activityUrl : null;
        return new Response(JSON.stringify(debugInfo, null, 2), {
          headers: { "Content-Type": "application/json; charset=utf-8" }
        });
      }

      // 未命中影片時跳轉回 Facebook（交給機器人的看門狗救援）
      if (!result.videoUrl || !info) {
        return new Response(null, {
          status: 302,
          headers: {
            "Location": `https://www.facebook.com${rawPath}${rawSearch}`,
            "X-FB-Status": `${result.fbStatus}`,
            "X-FB-Blocked": `${result.isBlocked}`,
            "X-Video-Found": "false"
          }
        });
      }

      // 先存進快取，Discord 第二次來讀 Mastodon JSON 時直接拿
      ctx.waitUntil(writeCache(host, statusId, result));

      const footerSiteName = buildFooterSiteName(info.likes);
      const oembedUrl = `https://${host}/oembed?site=${encodeURIComponent(footerSiteName)}`;
      const canonicalUrl = `https://www.facebook.com${rawPath}${rawSearch}`;
      const imageTag = info.preview
        ? `<meta property="og:image" content="${escapeHtml(proxied(host, info.preview))}" />`
        : "";

      const html = `<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="utf-8" />
  <meta name="theme-color" content="${THEME_COLOR}" />
  <meta property="og:site_name" content="${escapeHtml(footerSiteName)}" />
  <meta property="og:title" content="${escapeHtml(buildDisplayName(info))}" />
  <meta property="og:description" content="${escapeHtml(info.caption)}" />
  <meta property="og:url" content="${escapeHtml(canonicalUrl)}" />
  <meta property="og:type" content="video.other" />
  ${imageTag}
  <meta property="og:video" content="${escapeHtml(result.videoUrl)}" />
  <meta property="og:video:secure_url" content="${escapeHtml(result.videoUrl)}" />
  <meta property="og:video:type" content="video/mp4" />
  <meta property="og:video:width" content="${info.width}" />
  <meta property="og:video:height" content="${info.height}" />
  <meta name="twitter:card" content="player" />
  <link rel="alternate" type="application/activity+json" href="${escapeHtml(activityUrl)}" />
  <link rel="alternate" type="application/json+oembed" href="${escapeHtml(oembedUrl)}" />
</head>
<body></body>
</html>`;

      return new Response(html, {
        headers: {
          "Content-Type": "text/html; charset=utf-8",
          "X-FB-Status": `${result.fbStatus}`,
          "X-FB-Blocked": `${result.isBlocked}`,
          "X-Video-Found": "true",
          "X-Video-Audio": `${result.videoHasAudio}`
        }
      });

    } catch (e) {
      if (debug) {
        debugInfo.error = String(e && e.stack ? e.stack : e);
        return new Response(JSON.stringify(debugInfo, null, 2), {
          status: 500,
          headers: { "Content-Type": "application/json; charset=utf-8" }
        });
      }
      return Response.redirect(`https://www.facebook.com${rawPath}${rawSearch}`, 302);
    }
  }
};
