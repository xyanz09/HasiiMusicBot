#!/usr/bin/env python3
# ==============================================================================
# youtube.py - YouTube Download & Search Handler
# ==============================================================================
# For: Telegram Music Bot (Compatible with Ubuntu 26.04 LTS)
# Features:
# - YouTube search with caching
# - Video/Audio download
# - Playlist support
# - Live stream handling
# - Cookie support for age-restricted content
# ==============================================================================

import os
import re
import glob
import time
import yt_dlp
import random
import asyncio
import aiohttp
from pathlib import Path
from typing import Optional, Union, Dict, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Configure logging
import logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@dataclass
class Track:
    """Data class for YouTube track information"""
    id: str
    title: str
    duration: str
    duration_sec: int
    url: str
    thumbnail: str
    channel_name: str = ""
    view_count: str = ""
    is_live: bool = False
    message_id: int = 0
    file_path: Optional[str] = None
    user: Optional[str] = None
    time: int = 0
    video: bool = False


class YouTube:
    """YouTube download and search handler"""
    
    def __init__(self, config_dict: Dict = None):
        """
        Initialize YouTube handler
        
        Args:
            config_dict: Configuration dictionary with optional settings
        """
        self.base = "https://www.youtube.com/watch?v="
        self.cookies = []
        self.checked = False
        self.warned = False
        
        # YouTube URL regex pattern
        self.regex = re.compile(
            r"(https?://)?(www\.|m\.|music\.)?"
            r"(youtube\.com/(watch\?v=|shorts/|live/|embed/|playlist\?list=)|youtu\.be/)"
            r"([A-Za-z0-9_-]{11}|PL[A-Za-z0-9_-]+)([&?][^\s]*)?"
        )
        
        # Search result caching (10 minute TTL)
        self.search_cache: Dict[str, Tuple[Track, float]] = {}
        self.cache_ttl = 600  # 10 minutes
        
        # Download semaphore (limit concurrent downloads)
        self._download_semaphore = asyncio.Semaphore(5)
        self._max_video_height = 1080
        
        # Config
        self.config = config_dict or {}
        logger.info("✅ YouTube Handler initialized")

    def _to_seconds(self, duration_str: str) -> int:
        """Convert duration string (MM:SS or H:MM:SS) to seconds"""
        try:
            if not duration_str or duration_str == "LIVE":
                return 0
            
            parts = duration_str.split(":")
            if len(parts) == 2:  # MM:SS
                return int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:  # H:MM:SS
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            return 0
        except:
            return 0

    def get_cookies(self) -> Optional[str]:
        """Get random cookie file from cookies directory"""
        if not self.checked:
            cookies_dir = "cookies"
            if os.path.exists(cookies_dir):
                for file in os.listdir(cookies_dir):
                    if file.endswith(".txt"):
                        self.cookies.append(os.path.join(cookies_dir, file))
            self.checked = True
        
        if not self.cookies:
            if not self.warned:
                logger.warning("⚠️ No cookies found. Age-restricted videos may fail.")
                self.warned = True
            return None
        
        return random.choice(self.cookies)

    async def save_cookies(self, urls: list) -> None:
        """
        Download and save YouTube cookies from URLs
        
        Args:
            urls: List of cookie URLs to download
        """
        logger.info("🍪 Downloading cookies from URLs...")
        saved_count = 0
        
        cookies_dir = "cookies"
        if not os.path.exists(cookies_dir):
            os.makedirs(cookies_dir)
        
        for url in urls:
            try:
                # Convert GitHub raw URL
                download_url = url.replace("me/", "me/raw/")
                
                async with aiohttp.ClientSession() as session:
                    async with session.get(download_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status != 200:
                            logger.error(f"❌ Cookie download failed: HTTP {resp.status}")
                            continue
                        
                        content = await resp.read()
                        if not content or len(content) < 50:
                            logger.error(f"❌ Cookie file too small or empty")
                            continue
                        
                        # Save cookie file
                        cookie_path = os.path.join(cookies_dir, f"cookie_{random.randint(10000, 99999)}.txt")
                        with open(cookie_path, "wb") as f:
                            f.write(content)
                        
                        if os.path.exists(cookie_path) and os.path.getsize(cookie_path) > 0:
                            self.cookies.append(cookie_path)
                            saved_count += 1
                            logger.info(f"✅ Saved: {os.path.basename(cookie_path)}")
            
            except asyncio.TimeoutError:
                logger.error(f"❌ Timeout downloading cookies from {url}")
            except Exception as e:
                logger.error(f"❌ Error downloading cookies: {e}")
        
        self.checked = True
        if saved_count > 0:
            logger.info(f"✅ Total cookies saved: {saved_count}")
        else:
            logger.warning("⚠️ No cookies saved successfully")

    def valid(self, url: str) -> bool:
        """Check if URL is valid YouTube URL"""
        return bool(re.match(self.regex, url))

    async def search(self, query: str, message_id: int = 0) -> Optional[Track]:
        """
        Search YouTube for a query
        
        Args:
            query: Search query string
            message_id: Telegram message ID (for tracking)
        
        Returns:
            Track object if found, None otherwise
        """
        # Check cache first
        current_time = time.time()
        if query in self.search_cache:
            cached_track, cache_time = self.search_cache[query]
            if current_time - cache_time < self.cache_ttl:
                logger.info(f"📦 Using cached search result for: {query}")
                # Return copy with updated message_id
                track_copy = Track(
                    id=cached_track.id,
                    title=cached_track.title,
                    duration=cached_track.duration,
                    duration_sec=cached_track.duration_sec,
                    url=cached_track.url,
                    thumbnail=cached_track.thumbnail,
                    channel_name=cached_track.channel_name,
                    view_count=cached_track.view_count,
                    is_live=cached_track.is_live,
                    message_id=message_id,
                )
                return track_copy
        
        try:
            logger.info(f"🔍 Searching YouTube for: {query}")
            
            # Use yt-dlp for search
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'default_search': 'ytsearch1',
                'socket_timeout': 30,
                'retries': 2,
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}}
            }
            
            def _search():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    result = ydl.extract_info(f"ytsearch:{query}", download=False)
                    if result and 'entries' in result and result['entries']:
                        return result['entries'][0]
                    return None
            
            # Run in thread pool to avoid blocking
            info = await asyncio.to_thread(_search)
            
            if not info:
                logger.warning(f"❌ No results found for: {query}")
                return None
            
            # Extract duration
            duration_sec = info.get('duration', 0) or 0
            is_live = duration_sec == 0 and info.get('is_live', False)
            
            # Format duration string
            if is_live:
                duration_str = "LIVE"
            elif duration_sec > 0:
                mins, secs = divmod(duration_sec, 60)
                hours, mins = divmod(mins, 60)
                if hours > 0:
                    duration_str = f"{hours}:{mins:02d}:{secs:02d}"
                else:
                    duration_str = f"{mins}:{secs:02d}"
            else:
                duration_str = "0:00"
            
            # Create track object
            track = Track(
                id=info.get('id', ''),
                title=info.get('title', 'Unknown')[:30],
                duration=duration_str,
                duration_sec=duration_sec,
                url=info.get('webpage_url', ''),
                thumbnail=info.get('thumbnail', ''),
                channel_name=info.get('uploader', ''),
                view_count=info.get('view_count', 0),
                is_live=is_live,
                message_id=message_id,
            )
            
            # Cache the result
            self.search_cache[query] = (track, current_time)
            
            # Limit cache size
            if len(self.search_cache) > 100:
                oldest_key = min(self.search_cache.keys(), 
                               key=lambda k: self.search_cache[k][1])
                del self.search_cache[oldest_key]
            
            logger.info(f"✅ Found: {track.title} ({duration_str})")
            return track
        
        except Exception as e:
            logger.error(f"❌ Search error: {e}")
            return None

    async def playlist(self, url: str, limit: int = 50, user: str = "") -> list:
        """
        Get tracks from YouTube playlist
        
        Args:
            url: Playlist URL
            limit: Maximum number of tracks to extract
            user: Username (for tracking)
        
        Returns:
            List of Track objects
        """
        try:
            logger.info(f"📋 Extracting playlist from: {url}")
            
            tracks = []
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': 'in_playlist',
                'socket_timeout': 30,
                'retries': 2,
            }
            
            def _get_playlist():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    result = ydl.extract_info(url, download=False)
                    return result.get('entries', [])
            
            entries = await asyncio.to_thread(_get_playlist)
            
            for entry in entries[:limit]:
                try:
                    if not entry:
                        continue
                    
                    video_id = entry.get('id', '')
                    if not video_id:
                        continue
                    
                    track = Track(
                        id=video_id,
                        title=entry.get('title', 'Unknown')[:30],
                        duration=entry.get('duration_string', '0:00'),
                        duration_sec=entry.get('duration', 0) or 0,
                        url=f"https://www.youtube.com/watch?v={video_id}",
                        thumbnail=entry.get('thumbnail', ''),
                        channel_name=entry.get('uploader', ''),
                        user=user,
                    )
                    tracks.append(track)
                except Exception as e:
                    logger.warning(f"⚠️ Error parsing playlist entry: {e}")
                    continue
            
            logger.info(f"✅ Extracted {len(tracks)} tracks from playlist")
            return tracks
        
        except Exception as e:
            logger.error(f"❌ Playlist error: {e}")
            return []

    def _locate_download_file(self, video_id: str, video: bool = False) -> Optional[str]:
        """Find existing download file for video_id"""
        pattern = f"downloads/{video_id}*"
        candidates = sorted([
            path for path in glob.glob(pattern)
            if not path.endswith((".part", ".ytdl", ".info.json", ".temp"))
        ])
        
        video_exts = {".mp4", ".mkv", ".webm", ".mov"}
        audio_exts = {".m4a", ".webm", ".opus", ".mp3", ".ogg", ".wav", ".flac"}
        
        if video:
            for path in candidates:
                if not os.path.isdir(path) and Path(path).suffix.lower() in video_exts:
                    return path
        else:
            for path in candidates:
                if not os.path.isdir(path) and Path(path).suffix.lower() in audio_exts:
                    return path
            
            # Fallback: try any video container for audio
            for path in candidates:
                if not os.path.isdir(path) and Path(path).suffix.lower() in video_exts:
                    return path
        
        # Return any file as last resort
        for path in candidates:
            if not os.path.isdir(path):
                return path
        
        return None

    async def download(self, video_id: str, is_live: bool = False, video: bool = False) -> Optional[str]:
        """
        Download YouTube video/audio
        
        Args:
            video_id: YouTube video ID
            is_live: Whether it's a live stream
            video: Download as video (True) or audio (False)
        
        Returns:
            Path to downloaded file or stream URL for live
        """
        url = self.base + video_id
        
        # Handle live streams - extract stream URL only
        if is_live:
            logger.info(f"🔴 Extracting live stream URL for: {video_id}")
            
            cookie = self.get_cookies()
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'cookiefile': cookie,
                'format': 'bestaudio/best',
                'noplaylist': True,
                'socket_timeout': 20,
                'extractor_retries': 5,
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
            }
            
            def _extract_live():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    try:
                        info = ydl.extract_info(url, download=False)
                        if info:
                            # Try to get stream URL
                            stream_url = info.get('url')
                            if stream_url:
                                return stream_url
                            
                            # Check formats
                            for fmt in info.get('formats', []):
                                if fmt.get('url') and fmt.get('acodec') != 'none':
                                    return fmt['url']
                        
                        return info.get('manifest_url')
                    except Exception as e:
                        logger.error(f"❌ Live stream extraction failed: {e}")
                        return None
            
            try:
                stream_url = await asyncio.wait_for(
                    asyncio.to_thread(_extract_live),
                    timeout=35
                )
                if stream_url:
                    logger.info(f"✅ Got live stream URL")
                    return stream_url
            except asyncio.TimeoutError:
                logger.error(f"❌ Live stream extraction timeout")
            
            return None
        
        # Check if file already cached
        existing = self._locate_download_file(video_id, video=video)
        if existing:
            logger.info(f"📦 Using cached file: {existing}")
            return existing
        
        # Create downloads directory
        downloads_dir = Path("downloads")
        downloads_dir.mkdir(parents=True, exist_ok=True)
        
        # Download with semaphore (limit concurrent downloads)
        async with self._download_semaphore:
            logger.info(f"⏳ Downloading: {video_id} ({'video' if video else 'audio'})")
            
            cookie = self.get_cookies()
            
            base_opts = {
                'outtmpl': 'downloads/%(id)s.%(ext)s',
                'quiet': True,
                'noplaylist': True,
                'geo_bypass': True,
                'no_warnings': True,
                'overwrites': False,
                'nocheckcertificate': True,
                'continuedl': True,
                'noprogress': True,
                'concurrent_fragment_downloads': 4,
                'http_chunk_size': 524288,
                'socket_timeout': 30,
                'retries': 2,
                'fragment_retries': 2,
                'extractor_retries': 5,
                'sleep_interval_requests': 1,
                'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
            }
            
            if video:
                # Video download
                ydl_opts = {
                    **base_opts,
                    'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                    'postprocessors': [
                        {
                            'key': 'FFmpegVideoConvertor',
                            'preferedformat': 'mp4',
                        }
                    ],
                }
            else:
                # Audio download
                ydl_opts = {
                    **base_opts,
                    'format': 'bestaudio[ext=m4a]/bestaudio[acodec=opus]/bestaudio/best',
                    'postprocessors': [],
                }
            
            if cookie:
                ydl_opts['cookiefile'] = cookie
            
            def _download():
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        if info:
                            time.sleep(0.5)
                            located = self._locate_download_file(video_id, video=video)
                            if located:
                                return located
                except yt_dlp.utils.ExtractorError as e:
                    error_msg = str(e).lower()
                    if "not available" in error_msg:
                        logger.error(f"❌ Video not available (region-blocked/private)")
                    elif "age" in error_msg:
                        logger.error(f"❌ Age-restricted video (need cookies)")
                    else:
                        logger.error(f"❌ Extractor error: {e}")
                except Exception as e:
                    logger.error(f"❌ Download error: {e}")
                
                return None
            
            try:
                file_path = await asyncio.wait_for(
                    asyncio.to_thread(_download),
                    timeout=300  # 5 minute timeout
                )
                
                if file_path and os.path.exists(file_path):
                    logger.info(f"✅ Downloaded: {os.path.basename(file_path)}")
                    return file_path
                else:
                    logger.error(f"❌ Download failed or file not found")
                    return None
            
            except asyncio.TimeoutError:
                logger.error(f"❌ Download timeout for: {video_id}")
             
