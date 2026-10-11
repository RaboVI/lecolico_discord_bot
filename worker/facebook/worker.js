// ================= 可調整設定 =================

// Embed 標題最多顯示幾個字（超過會加上「…」）
const TITLE_MAX_LENGTH = 30;

// 找不到含聲音的影片時，是否仍輸出「無聲影片」
//   true  ＝ 輸出無聲影片（至少畫面看得到）
//   false ＝ 跳轉回 Facebook，交給機器人的看門狗用 yt-dlp 下載有聲版本
const ALLOW_SILENT_FALLBACK = true;

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

// 輸出到 meta 標籤前轉義，避免標題或網址含有 " 時把 HTML 弄壞
function escapeAttr(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
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
    const idPositions = [];
    let pos = html.indexOf(requestedId);
    while (pos !== -1 && idPositions.length < 200) {
      idPositions.push(pos);
      pos = html.indexOf(requestedId, pos + 1);
    }
    for (const c of candidates) {
      c.nearId = idPositions.some(p => Math.abs(p - c.index) < 20000);
    }
  }

  return candidates.find(c => c.nearId && c.audio) ||
         candidates.find(c => c.audio && !requestedId) ||
         candidates.find(c => c.nearId) ||
         candidates[0];
}

// 整理標題：去掉觀看數、粉專名稱、hashtag，只留第一行，並限制字數
function buildTitle(rawTitle, rawDesc) {
  const isStats = s => /觀看|心情|views|reactions|likes/i.test(s);
  let title = "";

  if (rawTitle) {
    const parts = rawTitle.split("|").map(p => p.trim())
      .filter(p => p && p.toLowerCase() !== "facebook" && !isStats(p));
    // 挑最長的一段（通常是影片說明，而不是粉專名稱）
    title = parts.sort((a, b) => b.length - a.length)[0] || "";
  }
  if (!title && rawDesc) title = rawDesc;

  // 只取第一個有意義的行
  const firstLine = title.split("\n").map(l => l.trim()).find(l => l.length > 1) || "";
  title = firstLine
    .replace(/^(?:Facebook\s*(?:Reel)?[:\s\-]*)/i, "")
    .replace(/(?:\s*#[^\s#]+)+\s*$/, "")
    .replace(/[\s&|\-:;]+$/g, "")
    .trim();

  if (!title) title = "Facebook 影片";

  // 依「字元」計算長度（emoji、中文都算 1 個字）
  const chars = Array.from(title);
  if (chars.length > TITLE_MAX_LENGTH) {
    title = chars.slice(0, TITLE_MAX_LENGTH).join("").trim() + "…";
  }
  return title;
}

// 除錯用：擷取關鍵字前後的原始片段
function collectSnippets(rawHtml) {
  const keys = ["browser_native_sd_url", "browser_native_hd_url", "playable_url", "progressive_url", ".mp4"];
  const out = {};
  for (const k of keys) {
    const list = [];
    let pos = rawHtml.indexOf(k);
    while (pos !== -1 && list.length < 2) {
      list.push(rawHtml.slice(Math.max(0, pos - 60), pos + 240));
      pos = rawHtml.indexOf(k, pos + k.length);
    }
    if (list.length) out[k] = list;
  }
  return out;
}


// ================= 主程式 =================

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const userAgent = (request.headers.get("user-agent") || "").toLowerCase();

    // 【除錯模式】網址加上 ?__debug=1 就會回傳 JSON 診斷報告（不檢查 UA，可直接用瀏覽器開）
    const debug = url.searchParams.has("__debug");
    url.searchParams.delete("__debug");

    let rawPath = url.pathname;
    let rawSearch = url.search;

    const debugInfo = {
      requestPath: rawPath + rawSearch,
      colo: (request.cf && request.cf.colo) || null,
      shareProbe: null,
      resolvedPath: null,
      requestedId: null,
      attempts: [],
      videoMatch: null,
      videoHasAudio: null,
      videoEncodeTag: null,
      title: null,
      error: null
    };

    // 1. 一般使用者點擊：直接跳轉回 Facebook（除錯模式時略過）
    if (!debug && !userAgent.includes("discordbot")) {
      return Response.redirect(`https://www.facebook.com${rawPath}${rawSearch}`, 302);
    }

    // 2. Discord 官方爬蟲造訪
    try {
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
      const idMatch = rawPath.match(/\/(?:reel|videos)\/(\d+)/) || rawSearch.match(/[?&]v=(\d+)/);
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
      let titleHtml = "";     // 拿來抓標題的頁面（第一個有 og:title 的）

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

        const ogTitleMatch = rawHtml.match(/<meta\s+property="og:title"\s+content="([^"]*)"/i);
        if (!titleHtml && ogTitleMatch) titleHtml = rawHtml;

        const unescapedHtml = deepUnescape(rawHtml);
        const found = findVideoUrl(unescapedHtml, requestedId);

        debugInfo.attempts.push({
          label: plan.label,
          targetUrl: plan.url,
          status: fbResponse.status,
          finalUrl,
          htmlLength: rawHtml.length,
          ogTitle: ogTitleMatch ? ogTitleMatch[1] : null,
          hasLoginForm,
          hasRequestedId: requestedId ? unescapedHtml.includes(requestedId) : null,
          videoFound: !!found,
          videoHasAudio: found ? found.audio : null,
          videoEncodeTag: found ? getEncodeTag(found.url) : null,
          rawSnippets: debug ? collectSnippets(rawHtml) : undefined
        });

        if (found) {
          // 有聲音的優先；沒聲音的先記著當備案
          if (!bestVideo || (found.audio && !bestVideo.audio)) bestVideo = found;
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

      // 標題
      const html = deepUnescape(titleHtml);
      const rawTitleMatch = html.match(/<meta\s+property="og:title"\s+content="([^"]*)"/i);
      const rawDescMatch = html.match(/<meta\s+property="og:description"\s+content="([^"]*)"/i);
      const title = buildTitle(
        decodeHtml(rawTitleMatch ? rawTitleMatch[1] : ""),
        decodeHtml(rawDescMatch ? rawDescMatch[1] : "")
      );

      // 除錯模式：回傳 JSON 診斷報告
      if (debug) {
        debugInfo.videoMatch = videoUrl;
        debugInfo.videoHasAudio = bestVideo ? bestVideo.audio : null;
        debugInfo.videoEncodeTag = bestVideo ? getEncodeTag(bestVideo.url) : null;
        debugInfo.title = title;
        return new Response(JSON.stringify(debugInfo, null, 2), {
          headers: { "Content-Type": "application/json; charset=utf-8" }
        });
      }

      // 命中影片：以原生播放器卡片輸出
      if (videoUrl) {
        const safeVideo = escapeAttr(videoUrl);
        const safeTitle = escapeAttr(title);

        const ogHtml = `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta property="og:title" content="${safeTitle}" />
  <meta property="og:type" content="video.other" />
  <meta property="og:video" content="${safeVideo}" />
  <meta property="og:video:secure_url" content="${safeVideo}" />
  <meta property="og:video:type" content="video/mp4" />
  <meta property="og:video:width" content="720" />
  <meta property="og:video:height" content="1280" />
  <meta name="twitter:card" content="player" />
  <meta name="twitter:title" content="${safeTitle}" />
  <meta name="twitter:player:stream" content="${safeVideo}" />
  <meta name="twitter:player:stream:content_type" content="video/mp4" />
  <meta name="twitter:player:width" content="720" />
  <meta name="twitter:player:height" content="1280" />
</head>
<body></body>
</html>`;

        return new Response(ogHtml, {
          headers: {
            "Content-Type": "text/html; charset=utf-8",
            "X-FB-Status": `${fbStatus}`,
            "X-FB-Blocked": `${isBlocked}`,
            "X-Video-Found": "true",
            "X-Video-Audio": `${bestVideo.audio}`
          }
        });
      }

      // 未命中影片時跳轉回 Facebook
      return new Response(null, {
        status: 302,
        headers: {
          "Location": `https://www.facebook.com${rawPath}${rawSearch}`,
          "X-FB-Status": `${fbStatus}`,
          "X-FB-Blocked": `${isBlocked}`,
          "X-Video-Found": "false"
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