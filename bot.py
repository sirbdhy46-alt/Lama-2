import discord
from discord.ext import commands
import os
import sys
import asyncio
import yt_dlp
import aiohttp
import ctypes
import ctypes.util
from collections import deque
from dotenv import load_dotenv

load_dotenv()

# Support --token-env <VAR> so a clone instance can use a different token
_token_env_name = "DISCORD_BOT_TOKEN"
if "--token-env" in sys.argv:
    idx = sys.argv.index("--token-env")
    if idx + 1 < len(sys.argv):
        _token_env_name = sys.argv[idx + 1]

# Support --prefix <PREFIX> so each instance can have its own prefix
_prefix = "?"
if "--prefix" in sys.argv:
    idx = sys.argv.index("--prefix")
    if idx + 1 < len(sys.argv):
        _prefix = sys.argv[idx + 1]

# Load Opus — required for voice audio
def load_opus():
    if discord.opus.is_loaded():
        return

    import glob as _glob

    # 1. Known fixed paths (apt libopus0 on Ubuntu/Railway)
    opus_names = [
        "libopus.so.0",
        "/usr/lib/x86_64-linux-gnu/libopus.so.0",
        "/usr/lib/aarch64-linux-gnu/libopus.so.0",
        "/usr/lib/arm-linux-gnueabihf/libopus.so.0",
        "/usr/local/lib/libopus.so.0",
        "/usr/lib/libopus.so.0",
        "libopus.so",
        "libopus",
        "opus",
    ]
    for name in opus_names:
        try:
            discord.opus.load_opus(name)
            print(f"[Opus] Loaded: {name}")
            return
        except OSError:
            continue

    # 2. ctypes discovery (works if library is in ldconfig cache)
    lib = ctypes.util.find_library("opus")
    if lib:
        try:
            discord.opus.load_opus(lib)
            print(f"[Opus] Loaded via ctypes: {lib}")
            return
        except OSError:
            pass

    # 3. Glob search — finds opus anywhere on the system, including Nix store
    patterns = [
        "/usr/lib/**/libopus.so*",
        "/usr/local/lib/**/libopus.so*",
        "/nix/store/**/libopus.so*",
        "/lib/**/libopus.so*",
    ]
    for pattern in patterns:
        for match in sorted(_glob.glob(pattern, recursive=True)):
            try:
                discord.opus.load_opus(match)
                print(f"[Opus] Loaded via glob: {match}")
                return
            except OSError:
                continue

    print("[Opus] WARNING: Could not load Opus — voice will not work!")

load_opus()

TOKEN = os.getenv(_token_env_name)
PREFIX = _prefix

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.AutoShardedBot(command_prefix=PREFIX, intents=intents, help_command=None)

# ─── Per-guild state ────────────────────────────────────────────────────────

class GuildMusicState:
    def __init__(self):
        self.queue: deque = deque()
        self.current = None
        self.loop_mode = "off"          # off | track | queue
        self.volume = 0.5
        self.voice_client: discord.VoiceClient | None = None
        self.text_channel: discord.TextChannel | None = None
        self.skip_votes = set()
        self.audio_filter = "off"
        self.history: deque = deque(maxlen=20)
        self.stay_247 = False
        self.autoplay = False
        self.track_start_time: float = 0.0
        self.lonely_task: asyncio.Task | None = None

guild_states: dict[int, GuildMusicState] = {}

def get_state(guild_id: int) -> GuildMusicState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildMusicState()
    return guild_states[guild_id]

# ─── YouTube cookies support ─────────────────────────────────────────────

import tempfile as _tempfile
import base64 as _base64

_COOKIE_FILE: str | None = None

def _setup_cookies() -> str | None:
    raw = os.getenv("YOUTUBE_COOKIES", "").strip()
    if not raw:
        return None
    try:
        try:
            decoded = _base64.b64decode(raw).decode("utf-8")
        except Exception:
            decoded = raw
        if "youtube.com" not in decoded and "HTTP Cookie File" not in decoded:
            print("[cookies] YOUTUBE_COOKIES set but doesn't look like a valid cookie file")
            return None
        f = _tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        f.write(decoded)
        f.flush()
        f.close()
        print(f"[cookies] Loaded YouTube cookies from env → {f.name}")
        return f.name
    except Exception as e:
        print(f"[cookies] Failed to load cookies: {e}")
        return None

_COOKIE_FILE = _setup_cookies()

# ─── YTDLP / YouTube config ───────────────────────────────────────────────
# Railway is a datacenter IP. Even with cookies, the YouTube "web" client
# returns a restricted format manifest for datacenter IPs.
# Fix: use the "ios" client which hits a different API endpoint and is not
# subject to the same IP-based format restrictions. Also use "bestaudio/best"
# (no container filter) and check_formats=False so yt-dlp never rejects a
# format that the server claims is available.

_ytdl_base: dict = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extract_flat": False,
    "check_formats": False,
    "nocheckcertificate": True,
    "geo_bypass": True,
    "extractor_args": {
        "youtube": {
            "player_client": ["ios"],
        }
    },
}

if _COOKIE_FILE:
    _ytdl_base["cookiefile"] = _COOKIE_FILE

YTDL_OPTS = _ytdl_base

# ─── SoundCloud config (fallback) ────────────────────────────────────────

SC_YTDL_OPTS: dict = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
    "extract_flat": False,
    "check_formats": False,
    "nocheckcertificate": True,
}

async def fetch_track_soundcloud(query: str) -> dict | None:
    loop = asyncio.get_running_loop()
    sc_query = query if query.startswith("http://soundcloud.com") or query.startswith("https://soundcloud.com") else f"scsearch1:{query}"
    print(f"[SoundCloud] Searching: {sc_query[:80]}")
    with yt_dlp.YoutubeDL(SC_YTDL_OPTS) as ydl:
        try:
            info = await loop.run_in_executor(
                None, lambda: ydl.extract_info(sc_query, download=False)
            )
        except Exception as e:
            print(f"[SoundCloud] fetch error: {e}")
            return None
    if not info:
        return None
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        return entries[0] if entries else None
    return info

# FIX: Removed -b:a 192k — forcing a specific bitrate can cause FFmpeg to
# fail or stutter when the stream's native bitrate doesn't match.
FFMPEG_BASE_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS = {
    "before_options": FFMPEG_BASE_BEFORE,
    "options": "-vn -ar 48000",
}

# ─── Audio filter presets ──────────────────────────────────────────────────

AUDIO_FILTERS = {
    "off":        {"label": "🎵 Off",              "af": None},
    "bass":       {"label": "🔊 Bass Boost",        "af": "bass=g=10,dynaudnorm=f=150:g=15"},
    "nightcore":  {"label": "🌙 Nightcore",         "af": "aresample=48000,asetrate=48000*1.25,atempo=1.06"},
    "vaporwave":  {"label": "🌊 Vaporwave",         "af": "aresample=48000,asetrate=48000*0.8,atempo=0.9"},
    "8d":         {"label": "🎧 8D Audio",          "af": "apulsator=hz=0.125"},
    "slowed":     {"label": "🐢 Slowed + Reverb",   "af": "aresample=48000,asetrate=48000*0.85,atempo=0.95,aecho=0.8:0.9:500:0.4"},
    "karaoke":    {"label": "🎤 Karaoke",           "af": "pan=stereo|c0=c0-c1|c1=c1-c0"},
    "loud":       {"label": "📢 Loud",              "af": "dynaudnorm=f=200,volume=2.0"},
    "earrape":    {"label": "💀 Earrape",           "af": "dynaudnorm=f=75,volume=8.0"},
    "treble":     {"label": "✨ Treble Boost",      "af": "treble=g=8"},
    "crystal":    {"label": "💎 Crystal Clear",     "af": "highpass=f=200,lowpass=f=3000,dynaudnorm"},
    "soft":       {"label": "🌸 Soft",              "af": "volume=0.6,aecho=0.9:0.9:80:0.2"},
}

def build_ffmpeg_opts(audio_filter: str) -> dict:
    af = AUDIO_FILTERS.get(audio_filter, AUDIO_FILTERS["off"])["af"]
    options = "-vn -ar 48000"
    if af:
        options += f" -af \"{af}\""
    return {
        "before_options": FFMPEG_BASE_BEFORE,
        "options": options,
    }


def get_stream_url(info: dict) -> str:
    url = info.get("url", "")
    if url:
        print(f"[Stream] URL: {url[:80]}")
        return url
    formats = info.get("formats") or []
    if formats:
        best = formats[-1]
        return best.get("url", "")
    return ""


import re as _re

_YT_SEARCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

async def _search_with_ytdlp(query: str, max_results: int) -> list[str]:
    opts = {
        **YTDL_OPTS,
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
    }
    loop = asyncio.get_running_loop()
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = await loop.run_in_executor(
                None, lambda: ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
            )
            if not info:
                return []
            entries = info.get("entries", [])
            return [e["id"] for e in entries if e and e.get("id")][:max_results]
        except Exception as e:
            print(f"[search] yt-dlp ytsearch error: {e}")
            return []


async def _search_with_scrape(query: str, max_results: int) -> list[str]:
    try:
        async with aiohttp.ClientSession(headers=_YT_SEARCH_HEADERS) as session:
            async with session.get(
                "https://www.youtube.com/results",
                params={"search_query": query},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                html = await resp.text()
                if "videoId" not in html:
                    print(f"[search] Scrape: no videoId in response (IP may be blocked)")
                    return []
                ids = _re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
                seen: list[str] = []
                for vid_id in ids:
                    if vid_id not in seen:
                        seen.append(vid_id)
                    if len(seen) >= max_results:
                        break
                return seen
    except Exception as e:
        print(f"[search] Scrape error: {e}")
        return []


async def _scrape_youtube_ids(query: str, max_results: int = 1) -> list[str]:
    if _COOKIE_FILE:
        ids = await _search_with_ytdlp(query, max_results)
        if ids:
            print(f"[search] Found via yt-dlp+cookies: {ids[0]}")
            return ids

    ids = await _search_with_scrape(query, max_results)
    if ids:
        return ids

    if not _COOKIE_FILE:
        ids = await _search_with_ytdlp(query, max_results)
        if ids:
            print(f"[search] Found via yt-dlp (no cookies): {ids[0]}")
            return ids

    print(f"[search] All methods failed for: {query!r}")
    return []


SC_YTDL_OPTS: dict = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
    "extract_flat": False,
    "check_formats": False,
    "nocheckcertificate": True,
}

async def fetch_track_soundcloud(query: str) -> dict | None:
    loop = asyncio.get_running_loop()
    sc_query = query if "soundcloud.com" in query else f"scsearch1:{query}"
    print(f"[SoundCloud] Searching: {sc_query[:80]}")
    with yt_dlp.YoutubeDL(SC_YTDL_OPTS) as ydl:
        try:
            info = await loop.run_in_executor(
                None, lambda: ydl.extract_info(sc_query, download=False)
            )
        except Exception as e:
            print(f"[SoundCloud] fetch error: {e}")
            return None
    if not info:
        return None
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        return entries[0] if entries else None
    return info

async def fetch_track(query: str) -> dict | None:
    loop = asyncio.get_running_loop()
    opts = {**YTDL_OPTS, "noplaylist": True}

    yt_query = query
    if not query.startswith("http"):
        ids = await _scrape_youtube_ids(query, max_results=1)
        if ids:
            yt_query = f"https://www.youtube.com/watch?v={ids[0]}"
        else:
            print(f"[scrape] no YouTube results for: {query} — trying SoundCloud")
            return await fetch_track_soundcloud(query)

    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = await loop.run_in_executor(
                None, lambda: ydl.extract_info(yt_query, download=False)
            )
        except Exception as e:
            print(f"[yt-dlp] fetch error: {e} — falling back to SoundCloud")
            return await fetch_track_soundcloud(query)

    if not info:
        return await fetch_track_soundcloud(query)
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        return entries[0] if entries else await fetch_track_soundcloud(query)
    return info


async def search_youtube(query: str, max_results: int = 5) -> list[dict]:
    if query.startswith("http"):
        loop = asyncio.get_running_loop()
        opts = {**YTDL_OPTS, "extract_flat": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = await loop.run_in_executor(
                    None, lambda: ydl.extract_info(query, download=False)
                )
            except Exception as e:
                print(f"[yt-dlp] search error: {e}")
                return []
        if not info:
            return []
        if "entries" in info:
            return [e for e in info["entries"] if e][:max_results]
        return [info]

    ids = await _scrape_youtube_ids(query, max_results=max_results)
    results = []
    for vid_id in ids:
        results.append({
            "id": vid_id,
            "title": vid_id,
            "webpage_url": f"https://www.youtube.com/watch?v={vid_id}",
            "url": f"https://www.youtube.com/watch?v={vid_id}",
        })
    return results


def make_source(url: str, volume: float, audio_filter: str = "off") -> discord.PCMVolumeTransformer:
    opts = build_ffmpeg_opts(audio_filter)
    source = discord.FFmpegPCMAudio(url, **opts)
    return discord.PCMVolumeTransformer(source, volume=volume)


# ─── Embed helpers ────────────────────────────────────────────────────────────

COLORS = {
    "main": 0x7B2FBE,
    "success": 0x2ECC71,
    "error": 0xE74C3C,
    "info": 0x3498DB,
    "warn": 0xF39C12,
}

def embed(title: str, description: str = "", color_key: str = "main") -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=COLORS[color_key])
    e.set_footer(text="✨ Premium Music • Use ?help for commands")
    return e

def make_progress_bar(elapsed: float, total: float, length: int = 15) -> str:
    if total <= 0:
        return ""
    ratio = min(elapsed / total, 1.0)
    filled = int(ratio * length)
    bar = "━" * filled + "●" + "─" * (length - filled)
    e_m, e_s = divmod(int(elapsed), 60)
    t_m, t_s = divmod(int(total), 60)
    return f"`{bar}` `{e_m}:{e_s:02d} / {t_m}:{t_s:02d}`"


def track_embed(track: dict, state: GuildMusicState, label: str = "🎵 Now Playing") -> discord.Embed:
    import time as _t
    title    = track.get("title", "Unknown")
    url      = track.get("webpage_url") or track.get("url", "")
    uploader = track.get("uploader") or track.get("channel", "Unknown Artist")
    duration = track.get("duration", 0)
    thumbnail = track.get("thumbnail", "")
    mins, secs = divmod(int(duration), 60)

    loop_icons = {"off": "➡️ Off", "track": "🔂 Track", "queue": "🔁 Queue"}
    elapsed = _t.time() - state.track_start_time if state.track_start_time else 0

    e = discord.Embed(
        title=label,
        description=f"**[{title}]({url})**\n{make_progress_bar(elapsed, duration)}",
        color=COLORS["main"],
    )
    e.add_field(name="🎤 Artist",  value=uploader,                                  inline=True)
    e.add_field(name="⏱️ Duration", value=f"{mins}:{secs:02d}",                     inline=True)
    e.add_field(name="🔊 Volume",  value=f"{int(state.volume * 100)}%",             inline=True)
    e.add_field(name="🔁 Loop",    value=loop_icons.get(state.loop_mode, "➡️ Off"), inline=True)
    e.add_field(name="📋 Queue",   value=f"{len(state.queue)} tracks",              inline=True)
    filter_label = AUDIO_FILTERS.get(state.audio_filter, AUDIO_FILTERS["off"])["label"]
    e.add_field(name="🎛️ Effect",  value=filter_label,                              inline=True)
    mode_str = "🌙 24/7" if state.stay_247 else ("🤖 AutoPlay" if state.autoplay else "▶️ Normal")
    e.add_field(name="📡 Mode",    value=mode_str,                                  inline=True)
    if thumbnail:
        e.set_thumbnail(url=thumbnail)
    e.set_footer(text="✨ Premium Music • 48kHz")
    return e


# ─── Voice connection helper ─────────────────────────────────────────────────

async def safe_connect(channel: discord.VoiceChannel, state: GuildMusicState, status_msg=None) -> bool:
    if state.voice_client and state.voice_client.is_connected():
        if state.voice_client.channel != channel:
            await state.voice_client.move_to(channel)
        return True

    for attempt in range(1, 6):
        if state.voice_client:
            try:
                await state.voice_client.disconnect(force=True)
            except Exception:
                pass
            state.voice_client = None

        try:
            if status_msg:
                await status_msg.edit(embed=embed(
                    "🔌 Connecting...",
                    f"Joining **{channel.name}** (attempt {attempt}/5)...",
                    "info"
                ))
            vc = await channel.connect(timeout=30, reconnect=False, self_deaf=True)
            state.voice_client = vc
            print(f"[Voice] Connected on attempt {attempt}")
            return True
        except Exception as e:
            print(f"[Voice] Attempt {attempt} failed: {e}")
            await asyncio.sleep(2)

    return False


# ─── Playback engine ─────────────────────────────────────────────────────────

async def play_next(guild_id: int):
    state = get_state(guild_id)
    vc = state.voice_client
    if not vc or not vc.is_connected():
        return

    if state.loop_mode == "track" and state.current:
        track = state.current
    elif state.queue:
        track = state.queue.popleft()
        if state.loop_mode == "queue" and state.current:
            state.queue.append(state.current)
        state.current = track
    else:
        if state.autoplay and state.history:
            last = state.history[-1]
            search_q = f"{last.get('uploader', '')} {last.get('title', '')} mix"
            try:
                results = await search_youtube(search_q, max_results=5)
                history_urls = {t.get("webpage_url") or t.get("url") for t in state.history}
                picked = next((r for r in results if (r.get("webpage_url") or r.get("url")) not in history_urls), None)
                if picked:
                    state.queue.append(picked)
                    if state.text_channel:
                        await state.text_channel.send(embed=embed(
                            "🤖 AutoPlay",
                            f"Adding **{picked.get('title', 'track')}** to keep the music going!",
                            "info"
                        ))
                    await play_next(guild_id)
                    return
            except Exception as e:
                print(f"[AutoPlay] Error: {e}")

        state.current = None
        if state.text_channel:
            await state.text_channel.send(embed=embed("⏹️ Queue finished", "Nothing more to play. Add tracks with `?play`!", "info"))
        await bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.listening, name=f"{PREFIX}help | YouTube 🎵"
        ))
        return

    page_url = track.get("webpage_url") or track.get("url", "")
    fresh = await fetch_track(page_url) if page_url else None
    if not fresh:
        if state.text_channel:
            await state.text_channel.send(embed=embed("⚠️ Skipped", f"Could not stream **{track.get('title', '?')}**", "warn"))
        await play_next(guild_id)
        return

    state.current = fresh
    stream_url = get_stream_url(fresh)
    if not stream_url:
        if state.text_channel:
            await state.text_channel.send(embed=embed("⚠️ Skipped", f"No audio URL for **{fresh.get('title', '?')}**", "warn"))
        await play_next(guild_id)
        return

    import time as _t
    state.track_start_time = _t.time()
    state.history.append(fresh)

    def after_playing(error):
        if error:
            print(f"[Player] Error: {error}")
        asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)

    source = make_source(stream_url, state.volume, state.audio_filter)
    vc.play(source, after=after_playing)
    state.skip_votes.clear()

    title_short = fresh.get("title", "music")[:40]
    asyncio.run_coroutine_threadsafe(
        bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.listening, name=f"{title_short} 🎵"
        )),
        bot.loop,
    )

    if state.text_channel:
        await state.text_channel.send(embed=track_embed(fresh, state))


# ─── Events ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    shard_info = f" [{bot.shard_count} shard(s)]" if bot.shard_count else ""
    print(f"🎵 {bot.user} is online and ready to drop beats!{shard_info}")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name=f"{PREFIX}help | YouTube 🎵")
    )

@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=embed("❌ Missing Argument", f"`{error.param.name}` is required.", "error"))
    elif isinstance(error, commands.CommandNotFound):
        pass
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.send(embed=embed("❌ Server Only", "This command can only be used in a server.", "error"))
    elif isinstance(error, commands.CheckFailure):
        pass
    else:
        await ctx.send(embed=embed("❌ Error", str(error), "error"))

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild = member.guild
    state = get_state(guild.id)
    vc = state.voice_client

    if not vc or not vc.is_connected():
        return

    real_members = [m for m in vc.channel.members if not m.bot]

    if len(real_members) == 0 and not state.stay_247:
        if state.lonely_task and not state.lonely_task.done():
            state.lonely_task.cancel()

        async def auto_leave():
            await asyncio.sleep(180)
            if vc.is_connected():
                re_check = [m for m in vc.channel.members if not m.bot]
                if len(re_check) == 0:
                    await vc.disconnect(force=True)
                    state.voice_client = None
                    state.queue.clear()
                    state.current = None
                    if state.text_channel:
                        await state.text_channel.send(embed=embed(
                            "👋 Auto-Disconnected",
                            "Left the voice channel after 3 minutes of inactivity.\nUse `?join` or `?play` to bring me back!",
                            "info"
                        ))
                    await bot.change_presence(activity=discord.Activity(
                        type=discord.ActivityType.listening, name=f"{PREFIX}help | YouTube 🎵"
                    ))

        state.lonely_task = asyncio.create_task(auto_leave())

    elif len(real_members) > 0:
        if state.lonely_task and not state.lonely_task.done():
            state.lonely_task.cancel()
            state.lonely_task = None


# ─── Commands ─────────────────────────────────────────────────────────────────

@bot.command(name="join", aliases=["connect", "j"])
@commands.guild_only()
async def join(ctx: commands.Context):
    """Join your voice channel."""
    if not ctx.author.voice:
        return await ctx.send(embed=embed("❌ Not in Voice", "Join a voice channel first!", "error"))
    channel = ctx.author.voice.channel
    state = get_state(ctx.guild.id)
    if state.voice_client and state.voice_client.is_connected():
        await state.voice_client.move_to(channel)
    else:
        state.voice_client = await channel.connect()
    state.text_channel = ctx.channel
    await ctx.send(embed=embed("✅ Joined", f"Connected to **{channel.name}** 🎤", "success"))


@bot.command(name="leave", aliases=["disconnect", "dc", "bye"])
@commands.guild_only()
async def leave(ctx: commands.Context):
    """Leave the voice channel."""
    state = get_state(ctx.guild.id)
    if not state.voice_client or not state.voice_client.is_connected():
        return await ctx.send(embed=embed("❌ Not Connected", "I'm not in a voice channel.", "error"))
    await state.voice_client.disconnect()
    state.voice_client = None
    state.queue.clear()
    state.current = None
    await ctx.send(embed=embed("👋 Disconnected", "Left the voice channel. See you next time!", "info"))


@bot.command(name="play", aliases=["p"])
@commands.guild_only()
async def play(ctx: commands.Context, *, query: str):
    """Play a song from YouTube (search or URL)."""
    state = get_state(ctx.guild.id)
    state.text_channel = ctx.channel

    if not ctx.author.voice and (not state.voice_client or not state.voice_client.is_connected()):
        return await ctx.send(embed=embed("❌ Not in Voice", "Join a voice channel first!", "error"))

    msg = await ctx.send(embed=embed("🔍 Searching", f"Looking for `{query}` on YouTube...", "info"))
    track = await fetch_track(query)
    if not track:
        return await msg.edit(embed=embed("❌ Not Found", f"No results for `{query}`", "error"))

    title = track.get("title", "Unknown")

    if not state.voice_client or not state.voice_client.is_connected():
        channel = ctx.author.voice.channel
        ok = await safe_connect(channel, state, status_msg=msg)
        if not ok:
            return await msg.edit(embed=embed("❌ Voice Error", "Could not connect after 5 attempts. Try `?join` first.", "error"))

    if state.voice_client.is_playing() or state.voice_client.is_paused():
        state.queue.append(track)
        await msg.edit(embed=embed("📋 Added to Queue", f"**{title}** added at position #{len(state.queue)}", "success"))
    else:
        state.current = track
        stream_url = get_stream_url(track)
        if not stream_url:
            return await msg.edit(embed=embed("❌ Stream Error", f"Could not get audio stream for **{title}**", "error"))

        import time as _t
        state.track_start_time = _t.time()
        state.history.append(track)
        await msg.edit(embed=track_embed(track, state))

        def after_playing(error):
            if error:
                print(f"[Player] Error: {error}")
            asyncio.run_coroutine_threadsafe(play_next(ctx.guild.id), bot.loop)

        source = make_source(stream_url, state.volume, state.audio_filter)
        state.voice_client.play(source, after=after_playing)
        state.skip_votes.clear()


@bot.command(name="playsc", aliases=["sc"])
@commands.guild_only()
async def playsc(ctx: commands.Context, *, query: str):
    """Force search on YouTube (same as ?play)."""
    await play(ctx, query=query)


@bot.command(name="search", aliases=["find", "lookup"])
@commands.guild_only()
async def search(ctx: commands.Context, *, query: str):
    """Search YouTube and pick from top 5 results."""
    state = get_state(ctx.guild.id)
    msg = await ctx.send(embed=embed("🔍 Searching", f"Searching YouTube for `{query}`...", "info"))
    results = await search_youtube(query, max_results=5)
    if not results:
        return await msg.edit(embed=embed("❌ Not Found", f"No results for `{query}`", "error"))

    desc = ""
    for i, r in enumerate(results, 1):
        title = r.get("title", "Unknown")
        uploader = r.get("uploader", "?")
        duration = r.get("duration", 0)
        mins, secs = divmod(int(duration), 60)
        desc += f"`{i}.` **{title}** — {uploader} `[{mins}:{secs:02d}]`\n"
    desc += "\nReply with a number (1-5) to play, or `cancel` to abort."

    await msg.edit(embed=embed("🎵 Search Results", desc, "info"))

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    try:
        reply = await bot.wait_for("message", check=check, timeout=30)
        if reply.content.lower() == "cancel":
            return await ctx.send(embed=embed("❌ Cancelled", "Search cancelled.", "warn"))
        idx = int(reply.content.strip()) - 1
        if 0 <= idx < len(results):
            track = results[idx]
            await play(ctx, query=track.get("webpage_url") or track["url"])
        else:
            await ctx.send(embed=embed("❌ Invalid", "Please enter a valid number.", "error"))
    except (ValueError, asyncio.TimeoutError):
        await ctx.send(embed=embed("⏳ Timed Out", "No response received. Search cancelled.", "warn"))


@bot.command(name="pause", aliases=["hold"])
@commands.guild_only()
async def pause(ctx: commands.Context):
    """Pause the current track."""
    state = get_state(ctx.guild.id)
    if state.voice_client and state.voice_client.is_playing():
        state.voice_client.pause()
        await ctx.send(embed=embed("⏸️ Paused", f"Paused **{state.current.get('title', 'track')}**. Use `?resume` to continue.", "info"))
    else:
        await ctx.send(embed=embed("❌ Nothing Playing", "There's nothing to pause.", "error"))


@bot.command(name="resume", aliases=["unpause", "continue", "r"])
@commands.guild_only()
async def resume(ctx: commands.Context):
    """Resume the current track."""
    state = get_state(ctx.guild.id)
    if state.voice_client and state.voice_client.is_paused():
        state.voice_client.resume()
        await ctx.send(embed=embed("▶️ Resumed", f"Resumed **{state.current.get('title', 'track')}**!", "success"))
    else:
        await ctx.send(embed=embed("❌ Not Paused", "Nothing is paused right now.", "error"))


@bot.command(name="skip", aliases=["s", "next", "n"])
@commands.guild_only()
async def skip(ctx: commands.Context):
    """Skip the current track (vote-skip system)."""
    state = get_state(ctx.guild.id)
    if not state.voice_client or not state.voice_client.is_playing():
        return await ctx.send(embed=embed("❌ Nothing Playing", "There's nothing to skip.", "error"))

    vc = state.voice_client
    listeners = [m for m in vc.channel.members if not m.bot]
    needed = max(1, len(listeners) // 2)

    state.skip_votes.add(ctx.author.id)
    votes = len(state.skip_votes)

    if votes >= needed or ctx.author.guild_permissions.manage_guild:
        vc.stop()
        await ctx.send(embed=embed("⏭️ Skipped", f"Skipped **{state.current.get('title', 'track')}** ({votes}/{needed} votes)!", "success"))
    else:
        await ctx.send(embed=embed("🗳️ Vote Skip", f"{votes}/{needed} votes to skip. Keep voting!", "info"))


@bot.command(name="forceskip", aliases=["fs"])
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def forceskip(ctx: commands.Context):
    """Force skip the current track (requires Manage Server)."""
    state = get_state(ctx.guild.id)
    if not state.voice_client or not state.voice_client.is_playing():
        return await ctx.send(embed=embed("❌ Nothing Playing", "There's nothing to skip.", "error"))
    state.voice_client.stop()
    await ctx.send(embed=embed("⏭️ Force Skipped", f"Force skipped by **{ctx.author.display_name}**!", "warn"))


@bot.command(name="stop", aliases=["clear"])
@commands.guild_only()
async def stop(ctx: commands.Context):
    """Stop playback and clear the queue."""
    state = get_state(ctx.guild.id)
    if state.voice_client:
        state.queue.clear()
        state.current = None
        state.loop_mode = "off"
        state.voice_client.stop()
    await ctx.send(embed=embed("⏹️ Stopped", "Playback stopped and queue cleared.", "info"))


@bot.command(name="queue", aliases=["q", "list", "upcoming"])
@commands.guild_only()
async def queue_cmd(ctx: commands.Context):
    """Show the current queue."""
    state = get_state(ctx.guild.id)
    if not state.current and not state.queue:
        return await ctx.send(embed=embed("📋 Empty Queue", "Nothing is in the queue. Use `?play` to add tracks!", "info"))

    desc = ""
    if state.current:
        title = state.current.get("title", "Unknown")
        uploader = state.current.get("uploader", "?")
        duration = state.current.get("duration", 0)
        mins, secs = divmod(int(duration), 60)
        desc += f"**▶️ Now Playing:**\n`→` **{title}** — {uploader} `[{mins}:{secs:02d}]`\n\n"

    if state.queue:
        desc += "**📋 Up Next:**\n"
        for i, t in enumerate(list(state.queue)[:10], 1):
            title = t.get("title", "Unknown")
            uploader = t.get("uploader", "?")
            duration = t.get("duration", 0)
            mins, secs = divmod(int(duration), 60)
            desc += f"`{i}.` **{title}** — {uploader} `[{mins}:{secs:02d}]`\n"
        if len(state.queue) > 10:
            desc += f"\n*...and {len(state.queue) - 10} more tracks*"

    loop_icons = {"off": "➡️ Off", "track": "🔂 Track", "queue": "🔁 Queue"}
    e = discord.Embed(title="🎶 Music Queue", description=desc, color=COLORS["main"])
    e.add_field(name="🔁 Loop", value=loop_icons.get(state.loop_mode, "➡️ Off"), inline=True)
    e.add_field(name="📊 Total", value=f"{len(state.queue)} queued", inline=True)
    e.set_footer(text="✨ Premium Music • ?help for commands")
    await ctx.send(embed=e)


@bot.command(name="nowplaying", aliases=["np", "current", "song"])
@commands.guild_only()
async def nowplaying(ctx: commands.Context):
    """Show what's currently playing."""
    state = get_state(ctx.guild.id)
    if not state.current:
        return await ctx.send(embed=embed("❌ Nothing Playing", "No track is playing right now. Use `?play` to start!", "info"))
    await ctx.send(embed=track_embed(state.current, state))


@bot.command(name="volume", aliases=["vol", "v"])
@commands.guild_only()
async def volume(ctx: commands.Context, vol: int):
    """Set volume (1–150)."""
    state = get_state(ctx.guild.id)
    if not 1 <= vol <= 150:
        return await ctx.send(embed=embed("❌ Invalid Volume", "Volume must be between **1** and **150**.", "error"))
    state.volume = vol / 100
    if state.voice_client and state.voice_client.source:
        state.voice_client.source.volume = state.volume
    bar = "█" * (vol // 10) + "░" * (10 - vol // 10)
    emoji = "🔇" if vol == 0 else "🔉" if vol < 50 else "🔊"
    await ctx.send(embed=embed("🔊 Volume Set", f"{emoji} `[{bar}]` **{vol}%**", "success"))


@bot.command(name="filter", aliases=["fx", "effect", "eq"])
@commands.guild_only()
async def filter_cmd(ctx: commands.Context, preset: str = ""):
    """Apply an audio effect preset. Use ?filter list to see all options."""
    state = get_state(ctx.guild.id)

    if preset.lower() in ("list", "help", ""):
        lines = [f"`{k}` — {v['label']}" for k, v in AUDIO_FILTERS.items()]
        e = discord.Embed(
            title="🎛️ Audio Effects",
            description="**Usage:** `?filter <name>`\n\n" + "\n".join(lines),
            color=COLORS["main"],
        )
        e.set_footer(text="Effects apply instantly to the current track ✨")
        return await ctx.send(embed=e)

    preset = preset.lower()
    if preset not in AUDIO_FILTERS:
        keys = ", ".join(f"`{k}`" for k in AUDIO_FILTERS)
        return await ctx.send(embed=embed("❌ Unknown Effect", f"Available: {keys}", "error"))

    state.audio_filter = preset
    label = AUDIO_FILTERS[preset]["label"]

    if state.voice_client and state.voice_client.is_playing() and state.current:
        stream_url = get_stream_url(state.current)
        if stream_url:
            state.voice_client.stop()
            await asyncio.sleep(0.3)
            source = make_source(stream_url, state.volume, state.audio_filter)

            def after_playing(error):
                if error:
                    print(f"[Filter] Error: {error}")
                asyncio.run_coroutine_threadsafe(play_next(ctx.guild.id), bot.loop)

            state.voice_client.play(source, after=after_playing)

    await ctx.send(embed=embed("🎛️ Effect Applied", f"Now using **{label}**! 🎶", "success"))


@bot.command(name="loop", aliases=["repeat"])
@commands.guild_only()
async def loop(ctx: commands.Context, mode: str = ""):
    """Set loop mode: off / track / queue."""
    state = get_state(ctx.guild.id)
    modes = {"off": "off", "track": "track", "queue": "queue", "song": "track", "t": "track", "q": "queue", "o": "off"}
    if mode.lower() not in modes:
        cycle = {"off": "track", "track": "queue", "queue": "off"}
        state.loop_mode = cycle.get(state.loop_mode, "off")
    else:
        state.loop_mode = modes[mode.lower()]

    icons = {"off": "➡️", "track": "🔂", "queue": "🔁"}
    labels = {"off": "Loop Off", "track": "Looping Track", "queue": "Looping Queue"}
    await ctx.send(embed=embed(f"{icons[state.loop_mode]} {labels[state.loop_mode]}", f"Loop mode set to **{state.loop_mode}**", "success"))


@bot.command(name="shuffle", aliases=["mix"])
@commands.guild_only()
async def shuffle(ctx: commands.Context):
    """Shuffle the queue."""
    import random
    state = get_state(ctx.guild.id)
    if len(state.queue) < 2:
        return await ctx.send(embed=embed("❌ Not Enough Tracks", "Need at least 2 tracks in queue to shuffle.", "warn"))
    q_list = list(state.queue)
    random.shuffle(q_list)
    state.queue = deque(q_list)
    await ctx.send(embed=embed("🔀 Shuffled!", f"Queue of **{len(state.queue)}** tracks has been shuffled!", "success"))


@bot.command(name="remove", aliases=["rm", "delete"])
@commands.guild_only()
async def remove(ctx: commands.Context, index: int):
    """Remove a track from the queue by position."""
    state = get_state(ctx.guild.id)
    if not state.queue:
        return await ctx.send(embed=embed("❌ Empty Queue", "The queue is empty.", "error"))
    if not 1 <= index <= len(state.queue):
        return await ctx.send(embed=embed("❌ Invalid Index", f"Enter a number between 1 and {len(state.queue)}.", "error"))
    q_list = list(state.queue)
    removed = q_list.pop(index - 1)
    state.queue = deque(q_list)
    await ctx.send(embed=embed("🗑️ Removed", f"Removed **{removed.get('title', 'track')}** from the queue.", "success"))


@bot.command(name="move", aliases=["mv"])
@commands.guild_only()
async def move(ctx: commands.Context, from_pos: int, to_pos: int):
    """Move a track in the queue from one position to another."""
    state = get_state(ctx.guild.id)
    if len(state.queue) < 2:
        return await ctx.send(embed=embed("❌ Not Enough Tracks", "Need at least 2 tracks to move.", "warn"))
    q_list = list(state.queue)
    n = len(q_list)
    if not (1 <= from_pos <= n and 1 <= to_pos <= n):
        return await ctx.send(embed=embed("❌ Invalid Position", f"Positions must be between 1 and {n}.", "error"))
    track = q_list.pop(from_pos - 1)
    q_list.insert(to_pos - 1, track)
    state.queue = deque(q_list)
    await ctx.send(embed=embed("↕️ Moved", f"Moved **{track.get('title', 'track')}** to position #{to_pos}.", "success"))


@bot.command(name="clearqueue", aliases=["cq", "emptyqueue"])
@commands.guild_only()
async def clearqueue(ctx: commands.Context):
    """Clear the queue (keeps current song playing)."""
    state = get_state(ctx.guild.id)
    count = len(state.queue)
    state.queue.clear()
    await ctx.send(embed=embed("🗑️ Queue Cleared", f"Removed **{count}** tracks from the queue.", "success"))


@bot.command(name="playnext", aliases=["pn", "playtop"])
@commands.guild_only()
async def playnext(ctx: commands.Context, *, query: str):
    """Add a song to the front of the queue."""
    state = get_state(ctx.guild.id)
    state.text_channel = ctx.channel

    if not state.voice_client or not state.voice_client.is_connected():
        if not ctx.author.voice:
            return await ctx.send(embed=embed("❌ Not in Voice", "Join a voice channel first!", "error"))
        state.voice_client = await ctx.author.voice.channel.connect()

    msg = await ctx.send(embed=embed("🔍 Searching", f"Looking for `{query}` on YouTube...", "info"))
    results = await search_youtube(query, max_results=1)
    if not results:
        return await msg.edit(embed=embed("❌ Not Found", f"No results for `{query}`", "error"))

    track = results[0]
    state.queue.appendleft(track)

    if not state.voice_client.is_playing() and not state.voice_client.is_paused():
        await msg.delete()
        await play(ctx, query=query)
    else:
        await msg.edit(embed=embed("⏫ Added to Front", f"**{track.get('title', 'track')}** will play next!", "success"))


@bot.command(name="seek")
@commands.guild_only()
async def seek(ctx: commands.Context, seconds: int):
    """Seek to a position in the current track (seconds)."""
    state = get_state(ctx.guild.id)
    if not state.current:
        return await ctx.send(embed=embed("❌ Nothing Playing", "No track is playing.", "error"))
    track = state.current
    stream_url = get_stream_url(track)
    if not stream_url:
        return await ctx.send(embed=embed("❌ Cannot Seek", "Seeking not supported for this track.", "error"))

    # FIX: -ss must come before reconnect flags so FFmpeg seeks at input level
    before = f"-ss {seconds} " + FFMPEG_BASE_BEFORE
    opts = {"before_options": before, "options": "-vn -ar 48000"}
    source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(stream_url, **opts), volume=state.volume)

    def after_playing(error):
        asyncio.run_coroutine_threadsafe(play_next(ctx.guild.id), bot.loop)

    state.voice_client.stop()
    state.voice_client.play(source, after=after_playing)
    mins, secs = divmod(seconds, 60)
    await ctx.send(embed=embed("⏩ Seeked", f"Jumped to `{mins}:{secs:02d}` in **{track.get('title', 'track')}**", "success"))


@bot.command(name="replay", aliases=["restart", "beginning"])
@commands.guild_only()
async def replay(ctx: commands.Context):
    """Restart the current track from the beginning."""
    state = get_state(ctx.guild.id)
    if not state.current:
        return await ctx.send(embed=embed("❌ Nothing Playing", "No track is playing.", "error"))
    await seek(ctx, 0)
    await ctx.send(embed=embed("🔄 Replaying", f"Restarting **{state.current.get('title', 'track')}** from the beginning!", "success"))


@bot.command(name="lyrics")
@commands.guild_only()
async def lyrics(ctx: commands.Context, *, query: str = ""):
    """Search for lyrics (uses current track if no query)."""
    state = get_state(ctx.guild.id)
    song = query or (state.current.get("title", "") if state.current else "")
    if not song:
        return await ctx.send(embed=embed("❌ No Track", "Specify a song or play one first.", "error"))

    async with aiohttp.ClientSession() as session:
        try:
            url = f"https://api.lyrics.ovh/v1/{song.replace(' ', '/')}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    lyr = data.get("lyrics", "")[:3900]
                    await ctx.send(embed=embed(f"📜 Lyrics: {song}", lyr or "No lyrics found.", "info"))
                else:
                    await ctx.send(embed=embed("❌ Not Found", f"No lyrics found for **{song}**.", "error"))
        except Exception:
            await ctx.send(embed=embed("❌ Error", "Could not fetch lyrics right now.", "error"))


@bot.command(name="radio", aliases=["station"])
@commands.guild_only()
async def radio(ctx: commands.Context, *, genre: str = "lofi"):
    """Play a YouTube radio/genre playlist."""
    state = get_state(ctx.guild.id)
    state.text_channel = ctx.channel

    if not state.voice_client or not state.voice_client.is_connected():
        if not ctx.author.voice:
            return await ctx.send(embed=embed("❌ Not in Voice", "Join a voice channel first!", "error"))
        state.voice_client = await ctx.author.voice.channel.connect()

    genres = {
        "lofi": "lofi hip hop",
        "chill": "chill vibes playlist",
        "hiphop": "hip hop playlist",
        "edm": "edm electronic playlist",
        "pop": "pop hits playlist",
        "jazz": "jazz playlist",
        "rnb": "r&b playlist",
        "classical": "classical music playlist",
    }
    search_q = genres.get(genre.lower(), f"{genre} playlist")
    msg = await ctx.send(embed=embed("📻 Radio", f"Searching for **{genre}** radio on YouTube...", "info"))

    results = await search_youtube(search_q, max_results=1)
    if not results:
        return await msg.edit(embed=embed("❌ Not Found", f"No radio found for **{genre}**", "error"))

    await msg.delete()
    await play(ctx, query=results[0].get("webpage_url") or results[0]["url"])


@bot.command(name="playlist", aliases=["pl"])
@commands.guild_only()
async def playlist(ctx: commands.Context, *, url: str):
    """Load a YouTube playlist URL into the queue."""
    state = get_state(ctx.guild.id)
    state.text_channel = ctx.channel

    if not state.voice_client or not state.voice_client.is_connected():
        if not ctx.author.voice:
            return await ctx.send(embed=embed("❌ Not in Voice", "Join a voice channel first!", "error"))
        state.voice_client = await ctx.author.voice.channel.connect()

    msg = await ctx.send(embed=embed("📂 Loading Playlist", "Fetching playlist from YouTube...", "info"))

    opts = {**YTDL_OPTS, "noplaylist": False, "extract_flat": True}
    loop = asyncio.get_running_loop()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
    except Exception as e:
        return await msg.edit(embed=embed("❌ Error", f"Could not load playlist: {e}", "error"))

    if not info or "entries" not in info:
        return await msg.edit(embed=embed("❌ Not a Playlist", "That doesn't appear to be a valid YouTube playlist.", "error"))

    tracks = [e for e in info["entries"] if e]
    for t in tracks:
        state.queue.append(t)

    if not state.voice_client.is_playing() and not state.voice_client.is_paused():
        await msg.edit(embed=embed("✅ Playlist Loaded", f"Added **{len(tracks)}** tracks. Starting playback!", "success"))
        await play_next(ctx.guild.id)
    else:
        await msg.edit(embed=embed("✅ Playlist Queued", f"Added **{len(tracks)}** tracks to the queue!", "success"))


@bot.command(name="grab", aliases=["dm", "save", "share"])
@commands.guild_only()
async def grab(ctx: commands.Context):
    """DM yourself the current song info."""
    state = get_state(ctx.guild.id)
    if not state.current:
        return await ctx.send(embed=embed("❌ Nothing Playing", "No track is currently playing.", "error"))

    track = state.current
    title = track.get("title", "Unknown")
    url = track.get("webpage_url") or track.get("url", "")
    uploader = track.get("uploader", "Unknown")
    duration = track.get("duration", 0)
    mins, secs = divmod(int(duration), 60)
    thumbnail = track.get("thumbnail", "")

    e = discord.Embed(
        title="🎵 Saved Track",
        description=f"**[{title}]({url})**\n\nGrabbed from **{ctx.guild.name}**",
        color=COLORS["main"],
    )
    e.add_field(name="🎤 Artist", value=uploader, inline=True)
    e.add_field(name="⏱️ Duration", value=f"{mins}:{secs:02d}", inline=True)
    if thumbnail:
        e.set_thumbnail(url=thumbnail)
    e.set_footer(text="✨ Saved via Lana Music Bot")

    try:
        await ctx.author.send(embed=e)
        await ctx.send(embed=embed("📬 Sent!", f"Track info for **{title}** has been DM'd to you!", "success"))
    except discord.Forbidden:
        await ctx.send(embed=embed("❌ DMs Closed", "Please enable DMs from server members to use this command.", "error"))


@bot.command(name="247", aliases=["stay", "nonstop"])
@commands.guild_only()
async def stay_247(ctx: commands.Context):
    """Toggle 24/7 mode — bot stays in voice even when everyone leaves."""
    state = get_state(ctx.guild.id)
    state.stay_247 = not state.stay_247
    if state.stay_247:
        if state.lonely_task and not state.lonely_task.done():
            state.lonely_task.cancel()
            state.lonely_task = None
        await ctx.send(embed=embed("🌙 24/7 Mode ON", "I'll stay in the voice channel forever, even when it's empty!", "success"))
    else:
        await ctx.send(embed=embed("🌙 24/7 Mode OFF", "I'll now leave after 3 minutes of inactivity.", "info"))


@bot.command(name="autoplay", aliases=["ap", "auto"])
@commands.guild_only()
async def autoplay_cmd(ctx: commands.Context):
    """Toggle AutoPlay — auto-queue related songs when queue is empty."""
    state = get_state(ctx.guild.id)
    state.autoplay = not state.autoplay
    if state.autoplay:
        await ctx.send(embed=embed("🤖 AutoPlay ON", "I'll automatically find and queue related songs when the queue runs out!", "success"))
    else:
        await ctx.send(embed=embed("🤖 AutoPlay OFF", "AutoPlay disabled. Playlist will stop when queue is empty.", "info"))


@bot.command(name="history", aliases=["recent", "played"])
@commands.guild_only()
async def history_cmd(ctx: commands.Context):
    """Show recently played tracks."""
    state = get_state(ctx.guild.id)
    if not state.history:
        return await ctx.send(embed=embed("📜 No History", "No tracks have been played yet this session.", "info"))

    tracks = list(state.history)[-10:][::-1]
    desc = ""
    for i, t in enumerate(tracks, 1):
        title = t.get("title", "Unknown")[:45]
        uploader = t.get("uploader", "?")
        url = t.get("webpage_url") or t.get("url", "")
        duration = t.get("duration", 0)
        mins, secs = divmod(int(duration), 60)
        desc += f"`{i}.` **[{title}]({url})** — {uploader} `[{mins}:{secs:02d}]`\n"

    e = discord.Embed(title="📜 Recently Played", description=desc, color=COLORS["main"])
    e.set_footer(text=f"Showing last {len(tracks)} tracks this session ✨")
    await ctx.send(embed=e)


@bot.command(name="skipto", aliases=["jumpto", "st"])
@commands.guild_only()
async def skipto(ctx: commands.Context, position: int):
    """Skip to a specific position in the queue."""
    state = get_state(ctx.guild.id)
    if not state.voice_client or not state.voice_client.is_playing():
        return await ctx.send(embed=embed("❌ Nothing Playing", "Nothing is playing right now.", "error"))
    if not state.queue:
        return await ctx.send(embed=embed("❌ Empty Queue", "The queue is empty.", "error"))
    if not 1 <= position <= len(state.queue):
        return await ctx.send(embed=embed("❌ Invalid Position", f"Enter a position between 1 and {len(state.queue)}.", "error"))

    q_list = list(state.queue)
    state.queue = deque(q_list[position - 1:])
    state.voice_client.stop()
    await ctx.send(embed=embed("⏭️ Skipped To", f"Jumped to position **#{position}**: **{q_list[position-1].get('title', 'track')}**!", "success"))


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    """Check bot latency."""
    latency = round(bot.latency * 1000)
    color = "success" if latency < 100 else "warn" if latency < 200 else "error"
    shard_id = ctx.guild.shard_id if ctx.guild else 0
    shard_info = f" | Shard `{shard_id}/{bot.shard_count}`" if bot.shard_count and bot.shard_count > 1 else ""
    await ctx.send(embed=embed("🏓 Pong!", f"Latency: **{latency}ms**{shard_info}", color))


@bot.command(name="uptime")
async def uptime_cmd(ctx: commands.Context):
    """Show bot uptime."""
    import time
    elapsed = int(time.time() - bot._start_time)
    hours, rem = divmod(elapsed, 3600)
    mins, secs = divmod(rem, 60)
    await ctx.send(embed=embed("⏰ Uptime", f"**{hours}h {mins}m {secs}s** online and jamming! 🎵", "info"))


@bot.command(name="info", aliases=["botinfo", "about"])
async def info(ctx: commands.Context):
    """Show bot information."""
    e = discord.Embed(title="🎵 Premium Music Bot", color=COLORS["main"])
    e.description = "A premium YouTube-powered music bot built for any server size!\n\nPowered by **YouTube** via yt-dlp • Auto-sharded for 100k+ member servers."
    e.add_field(name="🎤 Prefix",    value=f"`{PREFIX}`",                         inline=True)
    e.add_field(name="🖥️ Servers",   value=str(len(bot.guilds)),                  inline=True)
    e.add_field(name="📡 Latency",   value=f"{round(bot.latency * 1000)}ms",      inline=True)
    e.add_field(name="⚡ Shards",    value=str(bot.shard_count or 1),             inline=True)
    e.add_field(name="🐍 Language",  value="Python 3.11",                         inline=True)
    e.add_field(name="📦 Library",   value="discord.py (AutoSharded)",            inline=True)
    e.add_field(name="🎵 Source",    value="YouTube (48kHz)",                     inline=True)
    e.add_field(name="🎛️ Effects",   value=f"{len(AUDIO_FILTERS)} presets",       inline=True)
    e.add_field(name="🌙 Features",  value="24/7 • AutoPlay • History • Grab",    inline=True)
    e.set_footer(text=f"✨ Use {PREFIX}help to see all commands")
    await ctx.send(embed=e)


@bot.command(name="help", aliases=["h", "commands"])
async def help_cmd(ctx: commands.Context, *, command: str = ""):
    """Show all commands or details about a specific command."""
    if command:
        cmd = bot.get_command(command.lower())
        if not cmd:
            return await ctx.send(embed=embed("❌ Not Found", f"Command `{PREFIX}{command}` not found.", "error"))
        aliases = ", ".join([f"`{PREFIX}{a}`" for a in cmd.aliases]) if cmd.aliases else "None"
        e = embed(f"📖 {PREFIX}{cmd.name}", cmd.help or "No description.", "info")
        e.add_field(name="Aliases", value=aliases, inline=False)
        return await ctx.send(embed=e)

    categories = {
        "🎵 Playback": [
            ("`?play <query>`", "Play from YouTube (search or URL)"),
            ("`?playnext <query>`", "Add to front of queue"),
            ("`?playlist <url>`", "Load a YouTube playlist"),
            ("`?pause`", "Pause the current track"),
            ("`?resume`", "Resume playback"),
            ("`?stop`", "Stop and clear queue"),
            ("`?skip`", "Vote to skip current track"),
            ("`?forceskip`", "Force skip (Manage Server perm)"),
            ("`?seek <seconds>`", "Seek to position in track"),
            ("`?replay`", "Restart current track"),
        ],
        "🔍 Discovery": [
            ("`?search <query>`", "Search YouTube & pick from results"),
            ("`?radio <genre>`", "Play genre radio (lofi, edm, pop, jazz...)"),
            ("`?lyrics [song]`", "Fetch song lyrics"),
        ],
        "📋 Queue": [
            ("`?queue`", "View the current queue"),
            ("`?nowplaying`", "See what's currently playing"),
            ("`?shuffle`", "Shuffle the queue"),
            ("`?remove <pos>`", "Remove track by position"),
            ("`?move <from> <to>`", "Move track in queue"),
            ("`?clearqueue`", "Clear the entire queue"),
        ],
        "⚙️ Settings": [
            (f"`{PREFIX}volume <1-150>`",         "Set volume"),
            (f"`{PREFIX}loop [off/track/queue]`", "Set loop mode"),
            (f"`{PREFIX}filter [preset]`",         "Apply audio effect (bass, nightcore, 8d, vaporwave...)"),
            (f"`{PREFIX}247`",                     "Toggle 24/7 mode (stay in voice forever)"),
            (f"`{PREFIX}autoplay`",                "Toggle AutoPlay (auto-queue related songs)"),
            (f"`{PREFIX}join`",                    "Join your voice channel"),
            (f"`{PREFIX}leave`",                   "Leave voice channel"),
        ],
        "✨ Extras": [
            (f"`{PREFIX}grab`",         "DM yourself the current song"),
            (f"`{PREFIX}history`",      "Show recently played tracks"),
            (f"`{PREFIX}skipto <pos>`", "Jump to a queue position"),
        ],
        "ℹ️ Info": [
            (f"`{PREFIX}ping`",           "Check bot latency & shard"),
            (f"`{PREFIX}uptime`",         "Show bot uptime"),
            (f"`{PREFIX}info`",           "Bot information"),
            (f"`{PREFIX}help [command]`", "This help menu"),
        ],
    }

    e = discord.Embed(
        title="🎵 Premium Music Bot — Command Help",
        description=f"Prefix: **`{PREFIX}`** | Powered by **YouTube** 🎵\nUse `?help <command>` for details on any command.",
        color=COLORS["main"],
    )
    for cat, cmds in categories.items():
        val = "\n".join([f"{c[0]} — {c[1]}" for c in cmds])
        e.add_field(name=cat, value=val, inline=False)
    e.set_footer(text="✨ Premium Music Bot • YouTube Powered")
    await ctx.send(embed=e)


# ─── Track time ───────────────────────────────────────────────────────────────
import time as _time
bot._start_time = _time.time()

# ─── Keep-alive web server ────────────────────────────────────────────────────

_keepalive = "--keepalive" in sys.argv

async def run_keepalive():
    from aiohttp import web

    async def handle(request):
        return web.Response(text="🎵 Lana is alive and playing music!")

    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/ping", handle)

    port = int(os.getenv("PORT", 8090))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    domain = os.getenv("REPLIT_DEV_DOMAIN", "")
    if domain:
        print(f"[Keep-Alive] Server running → https://{domain}/")
    else:
        print(f"[Keep-Alive] Server running on port {port}")


# ─── Run ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        print("❌ ERROR: DISCORD_BOT_TOKEN not set. Add it to your secrets!")
    else:
        async def main():
            if _keepalive:
                await run_keepalive()
            await bot.start(TOKEN)

        asyncio.run(main())
