import requests
import json
import logging
import time
from datetime import datetime
from bs4 import BeautifulSoup
from typing import Optional

logger = logging.getLogger(__name__)


def fetch_wishlist(user_id: str) -> Optional[str]:
    """
    Fetch Douban wishlist HTML page.

    URL: https://movie.douban.com/people/{user_id}/wish
    Params: sort=time, start=0, mode=grid, type=movie
    Headers: User-Agent (Chrome)

    Returns: HTML string or None on error
    """
    url = f"https://movie.douban.com/people/{user_id}/wish"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    params = {"sort": "time", "start": 0, "mode": "grid", "type": "movie"}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        logger.info(f"Successfully fetched wishlist for user {user_id}")
        return response.text
    except requests.RequestException as e:
        logger.error(f"Failed to fetch wishlist for user {user_id}: {e}")
        return None


def parse_movies(html: str) -> list[dict]:
    """
    Parse movies from Douban wishlist HTML.

    The HTML structure for each movie item is:
    - Container: div.item.comment-item
    - Title (with variants): .title a — contains text like
      "心之全蚀 / Total Eclipse / 全蚀狂爱(台) / Eclipse totale"
      Split by " / " to get title_variants array
    - Year: .intro text — extract first 4 chars as year
    - Add date: .date text — format like "2025-05-10" or
      "2025-05-02T00:00:00.000Z"
      Normalize to "YYYY-MM-DD" format

    Returns: list of dicts, each with:
    {
        "title_variants": ["心之全蚀", "Total Eclipse", ...],
        "year": "1995",
        "add_date": "2025-03-16"
    }
    """
    movies = []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        logger.error(f"Failed to parse HTML: {e}")
        return movies

    for item in soup.select("div.item.comment-item"):
        title_tag = item.select_one(".title a")
        if not title_tag:
            continue

        raw_title = title_tag.get_text(strip=True)
        title_variants = [v.strip() for v in raw_title.replace(" / ", "/").split("/") if v.strip()]

        intro_tag = item.select_one(".intro")
        year = ""
        if intro_tag:
            intro_text = intro_tag.get_text(strip=True)
            if intro_text and len(intro_text) >= 4:
                year = intro_text[:4]

        date_tag = item.select_one(".date")
        add_date = ""
        if date_tag:
            raw_date = date_tag.get_text(strip=True)
            try:
                if "T" in raw_date:
                    dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    add_date = dt.strftime("%Y-%m-%d")
                else:
                    dt = datetime.strptime(raw_date, "%Y-%m-%d")
                    add_date = dt.strftime("%Y-%m-%d")
            except ValueError:
                add_date = raw_date[:10] if len(raw_date) >= 10 else raw_date

        movies.append({
            "title_variants": title_variants,
            "year": year,
            "add_date": add_date,
        })

    logger.info(f"Parsed {len(movies)} movies from wishlist HTML")
    return movies


def search_tmdb(query: str, year: str, api_key: str) -> Optional[dict]:
    """
    Search TMDB API for a movie.

    URL: https://api.themoviedb.org/3/search/movie
    Method: GET
    Params:
        - api_key: {api_key}
        - query: {query}
        - year: {year}
        - include_adult: false
        - language: zh
        - page: 1

    Returns: First result dict from results array, or None
    """
    url = "https://api.themoviedb.org/3/search/movie"
    params = {
        "api_key": api_key,
        "query": query,
        "year": year,
        "include_adult": "false",
        "language": "zh",
        "page": 1,
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        results = data.get("results", [])
        if results:
            first = results[0]
            logger.info(f"TMDB search found '{first.get('original_title')}' for query '{query}' ({year})")
            return first
        logger.warning(f"TMDB search returned no results for '{query}' ({year})")
        return None
    except requests.RequestException as e:
        logger.error(f"TMDB search failed for '{query}' ({year}): {e}")
        return None


def check_match(title_variants: list[str], tmdb_result: dict) -> bool:
    """
    Check if TMDB result's original_title matches any variant.

    Logic: if tmdb_result["original_title"] in title_variants → match
    """
    original_title = tmdb_result.get("original_title", "")
    if not original_title:
        logger.warning(f"No original_title in TMDB result")
        return False

    # Exact match first
    if original_title in title_variants:
        logger.info(f"Match found: '{original_title}' in {title_variants}")
        return True

    # Split variants on "/" — Douban format: "English Title/ 中文标题(地区)"
    # e.g. "The Fountain/ 超时空·爱(港)" → extract "The Fountain"
    for variant in title_variants:
        parts = variant.split("/")
        for part in parts:
            stripped = part.strip()
            # Remove trailing region tags like "(港)", "(台)"
            import re
            stripped = re.sub(r'\([^\)]*\)$', '', stripped).strip()
            if stripped and stripped.lower() == original_title.lower():
                logger.info(f"Match found via split: '{original_title}' ≈ '{stripped}' (from '{variant}')")
                return True

    # Fuzzy: original_title is a prefix of a variant part
    for variant in title_variants:
        parts = variant.split("/")
        for part in parts:
            stripped = part.strip()
            import re
            stripped = re.sub(r'\([^\)]*\)$', '', stripped).strip()
            if stripped and (stripped.lower().startswith(original_title.lower()) or original_title.lower().startswith(stripped.lower())):
                logger.info(f"Match found via prefix: '{original_title}' ≈ '{stripped}' (from '{variant}')")
                return True

    logger.warning(f"No match: '{original_title}' not in {title_variants}")
    return False


def get_quality_profile(tmdb_result: dict) -> int:
    """
    Determine Radarr quality profile based on original_language.

    - if tmdb_result["original_language"] == "en" → return 7
    - else → return 9
    """
    if tmdb_result.get("original_language") == "en":
        return 7
    return 9


def add_to_radarr(tmdb_id: int, quality_profile: int, radarr_url: str, radarr_api_key: str, root_folder_path: str = "/video/Movies") -> Optional[dict]:
    """
    Add movie to Radarr.

    URL: {radarr_url}/api/v3/movie
    Method: POST
    Headers:
        - accept: application/json
        - Content-Type: application/json
    Params:
        - apikey: {radarr_api_key}
    Body:
    {
        "title": "add_movie",
        "qualityProfileId": {quality_profile},
        "tmdbId": {tmdb_id},
        "rootFolderPath": "/video/Movies",
        "addOptions": {
            "ignoreEpisodesWithFiles": true,
            "ignoreEpisodesWithoutFiles": true,
            "monitor": "movieOnly",
            "searchForMovie": false,
            "addMethod": "manual"
        }
    }

    Returns: response dict or None on error
    """
    url = f"{radarr_url.rstrip('/')}/api/v3/movie"
    headers = {
        "accept": "application/json",
        "Content-Type": "application/json",
    }
    params = {"apikey": radarr_api_key}
    payload = {
        "title": "add_movie",
        "qualityProfileId": quality_profile,
        "tmdbId": tmdb_id,
        "rootFolderPath": root_folder_path,
        "addOptions": {
            "ignoreEpisodesWithFiles": True,
            "ignoreEpisodesWithoutFiles": True,
            "monitor": "movieOnly",
            "searchForMovie": False,
            "addMethod": "manual",
        },
    }

    try:
        response = requests.post(url, headers=headers, params=params, json=payload, timeout=10)
        if response.status_code == 400:
            # Check if movie already exists — that's not an error
            try:
                errors = response.json()
                if isinstance(errors, list):
                    for err in errors:
                        if err.get("errorCode") == "MovieExistsValidator":
                            logger.info(f"TMDB ID {tmdb_id} already exists in Radarr, skipping")
                            return {"status": "already_exists", "tmdbId": tmdb_id}
            except (ValueError, KeyError):
                pass
        response.raise_for_status()
        data = response.json()
        logger.info(f"Successfully added TMDB ID {tmdb_id} to Radarr (profile {quality_profile})")
        return data
    except requests.RequestException as e:
        logger.error(f"Failed to add TMDB ID {tmdb_id} to Radarr: {e}")
        return None


def notify_dingtalk(webhook_url: str, movie_name: str, original_title: str) -> bool:
    """
    Send DingTalk notification on success.

    URL: {webhook_url}
    Method: POST
    Headers: Content-Type: application/json
    Body:
    {
        "msgtype": "text",
        "text": {
            "content": "成功添加电影 {movie_name}({original_title}) 到Radarr！"
        }
    }

    Returns: True if sent, False on error
    """
    headers = {"Content-Type": "application/json"}
    payload = {
        "msgtype": "text",
        "text": {
            "content": f"成功添加电影 {movie_name}({original_title}) 到Radarr！"
        }
    }

    try:
        response = requests.post(webhook_url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"DingTalk notification sent for '{movie_name}'")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send DingTalk notification for '{movie_name}': {e}")
        return False


def load_baseline(config: dict) -> dict:
    """
    Load baseline from config.
    Returns: {"movie_name": "", "add_date": ""} or config value
    """
    baseline = config.get("douban_baseline", {})
    if not isinstance(baseline, dict):
        baseline = {}
    return {
        "movie_name": baseline.get("movie_name", ""),
        "add_date": baseline.get("add_date", ""),
    }


def save_baseline(config: dict, baseline: dict):
    """
    Save baseline to config.
    Updates config["douban_baseline"] in place.
    """
    config["douban_baseline"] = {
        "movie_name": baseline.get("movie_name", ""),
        "add_date": baseline.get("add_date", ""),
    }
    logger.info(f"Baseline updated: {baseline}")


def check_once(config: dict) -> dict:
    """
    Main orchestration function.

    Steps:
    1. Load baseline from config["douban_baseline"]
    2. Fetch wishlist HTML using config["douban_user_id"]
    3. Parse movies from HTML
    4. Compare first movie's add_date with baseline
    5. For each new movie:
       a. For each variant in title_variants:
          - Search TMDB with variant
          - Check match
          - If matched: get quality profile, add to Radarr, notify DingTalk
          - If not matched: sleep(3) and continue to next variant
       b. If no variant matched: log failure
    6. Update baseline with first movie's info
    7. Add to history
    8. Return result dict with stats

    Returns:
    {
        "success": True/False,
        "new_movies_count": int,
        "added_movies": [{"name": str, "original_title": str, "year": str}],
        "failed_movies": [{"name": str, "reason": str}],
        "last_check": "ISO datetime string"
    }
    """
    result = {
        "success": False,
        "new_movies_count": 0,
        "added_movies": [],
        "failed_movies": [],
        "last_check": datetime.now().isoformat(),
    }

    user_id = config.get("douban_user_id", "")
    tmdb_api_key = config.get("tmdb_api_key", "")
    radarr_url = config.get("radarr_url", "")
    radarr_api_key = config.get("radarr_api_key", "")
    radarr_root_folder = config.get("radarr_root_folder_path", "/video/Movies")
    dingtalk_enabled = config.get("dingtalk_enabled", False)
    dingtalk_webhook_url = config.get("dingtalk_webhook_url", "")

    if not user_id:
        logger.error("Missing douban_user_id in config")
        result["failed_movies"].append({"name": "", "reason": "Missing douban_user_id"})
        return result

    baseline = load_baseline(config)

    html = fetch_wishlist(user_id)
    if html is None:
        result["failed_movies"].append({"name": "", "reason": "Failed to fetch wishlist"})
        return result

    movies = parse_movies(html)
    if not movies:
        logger.warning("No movies found in wishlist")
        return result

    new_movies = []
    if not baseline.get("add_date"):
        new_movies = movies
        logger.info(f"First run: treating all {len(movies)} movies as new")
    else:
        try:
            baseline_date = datetime.strptime(baseline["add_date"], "%Y-%m-%d")
            first_movie_date = datetime.strptime(movies[0]["add_date"], "%Y-%m-%d")
            if first_movie_date <= baseline_date:
                logger.info("No new movies since last check")
                result["success"] = True
                result["new_movies_count"] = 0
                config["douban_last_check"] = result["last_check"]
                return result
            new_movies = [
                m for m in movies
                if datetime.strptime(m["add_date"], "%Y-%m-%d") > baseline_date
            ]
            logger.info(f"Found {len(new_movies)} new movies since {baseline['add_date']}")
        except (ValueError, KeyError) as e:
            logger.error(f"Date comparison failed: {e}")
            result["failed_movies"].append({"name": "", "reason": f"Date comparison error: {e}"})
            return result

    result["new_movies_count"] = len(new_movies)

    for movie in new_movies:
        title_variants = movie.get("title_variants", [])
        year = movie.get("year", "")
        primary_name = title_variants[0] if title_variants else ""

        matched = False
        for variant in title_variants:
            if not tmdb_api_key:
                logger.error("Missing tmdb_api_key, skipping TMDB search")
                break

            tmdb_result = search_tmdb(variant, year, tmdb_api_key)
            if tmdb_result is None:
                time.sleep(3)
                continue

            if check_match(title_variants, tmdb_result):
                tmdb_id = tmdb_result.get("id")
                if tmdb_id is not None:
                    quality_profile = get_quality_profile(tmdb_result)
                    radarr_response = add_to_radarr(
                        tmdb_id, quality_profile, radarr_url, radarr_api_key, radarr_root_folder
                    )
                    if radarr_response is not None:
                        original_title = tmdb_result.get("original_title", "")
                        result["added_movies"].append({
                            "name": primary_name,
                            "original_title": original_title,
                            "year": year,
                        })
                        logger.info(f"Successfully processed '{primary_name}' ({year})")

                        if dingtalk_enabled and dingtalk_webhook_url:
                            notify_dingtalk(dingtalk_webhook_url, primary_name, original_title)

                        matched = True
                        break
                    else:
                        result["failed_movies"].append({
                            "name": primary_name,
                            "reason": f"Radarr add failed for TMDB ID {tmdb_id}",
                        })
                        matched = True
                        break
                else:
                    logger.warning(f"TMDB result missing 'id' for '{primary_name}'")
            time.sleep(3)

        if not matched:
            result["failed_movies"].append({
                "name": primary_name,
                "reason": "No TMDB match found for any title variant",
            })
            logger.error(f"Failed to match '{primary_name}' ({year}) on TMDB")

    if movies:
        new_baseline = {
            "movie_name": movies[0]["title_variants"][0] if movies[0].get("title_variants") else "",
            "add_date": movies[0].get("add_date", ""),
        }
        save_baseline(config, new_baseline)

    history_entry = {
        "timestamp": result["last_check"],
        "new_movies_count": result["new_movies_count"],
        "added_movies": result["added_movies"],
        "failed_movies": result["failed_movies"],
    }
    if "douban_history" not in config or not isinstance(config.get("douban_history"), list):
        config["douban_history"] = []
    config["douban_history"].append(history_entry)
    config["douban_last_check"] = result["last_check"]

    result["success"] = len(result["failed_movies"]) == 0 or len(result["added_movies"]) > 0
    logger.info(
        f"Check complete: {result['new_movies_count']} new, "
        f"{len(result['added_movies'])} added, "
        f"{len(result['failed_movies'])} failed"
    )
    return result
