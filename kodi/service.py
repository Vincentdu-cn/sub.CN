# -*- coding: utf-8 -*-
"""
Kodi subtitle service plugin for Sub.CN API.

Calls the existing FastAPI endpoints (/api/search/video and /api/download/selected).
The plugin strips the protocol prefix from Kodi's file path so the API receives
a local filesystem path. Subtitles are written directly to the video directory
by the API (shared filesystem), so the plugin only needs to return the path.
"""

import os
import sys
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

import requests

__addon__ = xbmcaddon.Addon()
__scriptid__ = __addon__.getAddonInfo('id')
__scriptname__ = __addon__.getAddonInfo('name')
__version__ = __addon__.getAddonInfo('version')
__language__ = __addon__.getLocalizedString

__temp__ = xbmcvfs.translatePath(os.path.join(
    __addon__.getAddonInfo('profile'), 'temp'))


def log(msg):
    xbmc.log("{0}::{1}".format(__scriptname__, msg), level=xbmc.LOGDEBUG)


def get_api_url():
    base = __addon__.getSetting("api_url").rstrip("/")
    return base if base else "http://192.168.199.178:19030"


def get_api_timeout():
    try:
        return int(__addon__.getSetting("api_timeout"))
    except (ValueError, TypeError):
        return 30


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------

def strip_protocol(kodi_path):
    """Convert a Kodi media path to an API-compatible filesystem path.

    nfs://192.168.1.100/volume1/video/Movies/xx.mkv  -> /video/Movies/xx.mkv
    /volume1/video/Movies/xx.mkv                       -> /video/Movies/xx.mkv
    """
    decoded = urllib.parse.unquote(kodi_path)

    if decoded.startswith("stack://"):
        parts = decoded.split(" , ")
        decoded = parts[0][len("stack://"):]

    parsed = urllib.parse.urlparse(decoded)
    if parsed.scheme:
        path = parsed.path
    else:
        path = decoded

    path_from = __addon__.getSetting("path_from").strip()
    path_to = __addon__.getSetting("path_to").strip()
    if path_from and path.startswith(path_from):
        path = path_to + path[len(path_from):]

    return path


def build_kodi_subtitle_path(kodi_video_path, api_subtitle_path):
    """Construct a Kodi-accessible subtitle path from the original video path
    and the API-returned subtitle filename.

    kodi_video_path:   nfs://192.168.1.100/volume1/video/Movies/xx/xx.mkv
    api_subtitle_path: /volume1/video/Movies/xx/xx.zh.manual.srt
    result:            nfs://192.168.1.100/volume1/video/Movies/xx/xx.zh.manual.srt

    The subtitle is in the same directory as the video, so we just swap the filename.
    """
    sub_filename = os.path.basename(api_subtitle_path)
    kodi_dir = os.path.dirname(kodi_video_path)
    # Re-encode if the original path was a URL
    if "://" in kodi_video_path:
        # For URL-like paths, dirname works but may strip the trailing slash
        # Reconstruct manually to preserve the protocol prefix
        decoded = urllib.parse.unquote(kodi_video_path)
        parsed = urllib.parse.urlparse(decoded)
        kodi_dir_path = os.path.dirname(parsed.path)
        new_path = kodi_dir_path + "/" + sub_filename
        # Rebuild the URL with the new path
        return urllib.parse.urlunparse((
            parsed.scheme, parsed.netloc, new_path,
            parsed.params, parsed.query, parsed.fragment
        ))
    else:
        return os.path.join(kodi_dir, sub_filename)


# ---------------------------------------------------------------------------
# Language mapping
# ---------------------------------------------------------------------------

LANG_MAP = {
    "zho_chs": ("zh", "简体中文"),
    "zho_cht": ("zh", "繁體中文"),
    "zho_chs+eng": ("zh", "中英双语"),
    "zho_cht+eng": ("zh", "中英双语"),
    "zho": ("zh", "中文"),
    "chi": ("zh", "中文"),
    "chs": ("zh", "简体中文"),
    "cht": ("zh", "繁體中文"),
    "eng": ("en", "English"),
    "en": ("en", "English"),
}


def map_language(lang_code):
    """Map API language code to (2-letter code, display name)."""
    return LANG_MAP.get(lang_code, ("und", lang_code))


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def api_search(video_path, title, year, season, episode):
    """Call /api/search/video, return (search_id, results) or (None, error)."""
    try:
        resp = requests.post(
            get_api_url() + "/api/search/video",
            json={
                "video_path": video_path,
                "title": title,
                "year": year,
                "season": season,
                "episode": episode,
            },
            timeout=get_api_timeout(),
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            return None, data["error"]
        return data.get("search_id"), data.get("results", [])
    except requests.exceptions.RequestException as e:
        return None, str(e)


def api_download(video_path, search_id, result_index):
    """Call /api/download/selected, return (subtitle_path, error)."""
    try:
        resp = requests.post(
            get_api_url() + "/api/download/selected",
            json={
                "video_path": video_path,
                "search_id": search_id,
                "result_index": int(result_index),
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        log("download: API response status={0}, subtitle_path={1}".format(
            data.get("status"), data.get("subtitle_path")))
        if data.get("status") == "success":
            return data.get("subtitle_path"), None
        return None, data.get("error", "下载失败: status={0}".format(data.get("status")))
    except Exception as e:
        log("download: api_download exception: {0}".format(e))
        return None, str(e)


# ---------------------------------------------------------------------------
# Kodi subtitle service functions
# ---------------------------------------------------------------------------

def search(item):
    """Search for subtitles. Called by Kodi when user opens subtitle search."""
    kodi_video_path = item.get("file_original_path", "")
    local_video_path = strip_protocol(kodi_video_path)

    log("search: kodi_path={0}, local_path={1}".format(kodi_video_path, local_video_path))

    title = item.get("title", "")
    year = item.get("year", "")
    season = item.get("season", "")
    episode = item.get("episode", "")

    # Kodi passes season/episode as strings; normalize empty to None
    if season and str(season) == "0":
        season = ""
    if season:
        try:
            season = int(season)
        except (ValueError, TypeError):
            season = None
    else:
        season = None

    if episode:
        try:
            episode = int(episode)
        except (ValueError, TypeError):
            episode = None
    else:
        episode = None

    search_id, results = api_search(local_video_path, title, year, season, episode)

    if search_id is None:
        log("search error: {0}".format(results))
        xbmcgui.Dialog().notification(
            __scriptname__, "搜索失败: {0}".format(results),
            xbmcgui.NOTIFICATION_ERROR, 5000
        )
        return

    log("search: got {0} results, search_id={1}".format(len(results), search_id))

    for result in results:
        index = result.get("index", 0)
        lang_code = result.get("language", "")
        short_lang, display_lang = map_language(lang_code)
        label2 = result.get("title", "")
        provider = result.get("provider", "")
        score_pct = result.get("score_pct", 0)

        # Build display label: "简体中文 [zimuku] 85%"
        label = "{0} [{1}]".format(display_lang, provider)
        if score_pct:
            label += " {0:.0f}%".format(score_pct)

        star_rating = min(5, max(0, round(score_pct / 20))) if score_pct else 0

        listitem = xbmcgui.ListItem(label=label, label2=label2)
        listitem.setArt({'icon': str(star_rating), 'thumb': short_lang})
        listitem.setProperty("sync", "false")
        listitem.setProperty("hearing_imp", "false")

        # Encode download params in the URL
        download_url = "plugin://{0}/?action=download&search_id={1}&result_index={2}&video_path={3}".format(
            __scriptid__,
            search_id,
            index,
            urllib.parse.quote(local_video_path, safe=''),
        )

        xbmcplugin.addDirectoryItem(
            handle=int(sys.argv[1]),
            url=download_url,
            listitem=listitem,
            isFolder=False,
        )


def download(params):
    """Download a selected subtitle. Called by Kodi when user picks a result."""
    try:
        search_id = params.get("search_id", "")
        result_index = params.get("result_index", "")
        video_path = urllib.parse.unquote(params.get("video_path", ""))

        log("download: search_id={0}, index={1}, video_path={2}".format(
            search_id, result_index, video_path))

        subtitle_path, error = api_download(video_path, search_id, result_index)

        if subtitle_path:
            log("download: success, subtitle_path={0}".format(subtitle_path))
            xbmcgui.Dialog().notification(
                __scriptname__, "字幕已下载到视频目录，刷新字幕列表即可使用",
                xbmcgui.NOTIFICATION_INFO, 5000
            )
        else:
            log("download error: {0}".format(error))
            xbmcgui.Dialog().notification(
                __scriptname__, "下载失败: {0}".format(error),
                xbmcgui.NOTIFICATION_ERROR, 8000
            )
    except Exception as e:
        log("download: unhandled exception: {0}".format(e))
        xbmcgui.Dialog().notification(
            __scriptname__, "下载失败: {0}".format(e),
            xbmcgui.NOTIFICATION_ERROR, 8000
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def get_params():
    paramstring = sys.argv[2]
    params = {}
    if len(paramstring) >= 2:
        cleaned = paramstring.lstrip('?')
        if cleaned.endswith('/'):
            cleaned = cleaned[:-2]
        for pair in cleaned.split('&'):
            parts = pair.split('=', 1)
            if len(parts) == 2:
                params[parts[0]] = parts[1]
    return params


params = get_params()
action = params.get('action', 'search')

if action in ('search', 'manualsearch'):
    item = {}
    item['temp'] = False
    item['rar'] = False
    item['mansearch'] = False
    item['year'] = xbmc.getInfoLabel("VideoPlayer.Year")
    item['season'] = str(xbmc.getInfoLabel("VideoPlayer.Season"))
    item['episode'] = str(xbmc.getInfoLabel("VideoPlayer.Episode"))
    item['tvshow'] = xbmc.getInfoLabel("VideoPlayer.TVshowtitle")
    item['title'] = xbmc.getInfoLabel("VideoPlayer.OriginalTitle")
    item['file_original_path'] = urllib.parse.unquote(xbmc.Player().getPlayingFile())
    item['3let_language'] = []

    if 'searchstring' in params:
        item['mansearch'] = True
        item['mansearchstr'] = params['searchstring']

    for lang in urllib.parse.unquote(params.get('languages', '')).split(","):
        if lang:
            item['3let_language'].append(xbmc.convertLanguage(lang, xbmc.ISO_639_2))

    if item['title'] == "":
        item['title'] = xbmc.getInfoLabel("VideoPlayer.Title")
        if item['title'] == os.path.basename(xbmc.Player().getPlayingFile()):
            title, year = xbmc.getCleanMovieTitle(item['title'])
            item['title'] = title.replace('[', '').replace(']', '')
            item['year'] = year

    if item['episode'].lower().find("s") > -1:
        item['season'] = "0"
        item['episode'] = item['episode'][-1:]

    if item['file_original_path'].find("http") > -1:
        item['temp'] = True
    elif item['file_original_path'].find("rar://") > -1:
        item['rar'] = True
        item['file_original_path'] = os.path.dirname(item['file_original_path'][6:])
    elif item['file_original_path'].find("stack://") > -1:
        stack_path = item['file_original_path'].split(" , ")
        item['file_original_path'] = stack_path[0][8:]

    search(item)

elif action == 'download':
    download(params)

xbmcplugin.endOfDirectory(int(sys.argv[1]))
