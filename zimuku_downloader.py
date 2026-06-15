#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zimuku Subtitle Downloader - Standalone CLI
Downloads subtitles from zimuku.com / srtku.com with automatic
Yunsuo WAF captcha bypass using ddddocr.

Usage:
    python zimuku_downloader.py search "Movie Name"
    python zimuku_downloader.py download "Movie Name" -o ./subs
    python zimuku_downloader.py download "TV Show" -s 1 -e 5 -o ./subs
"""

import argparse
from pathlib import Path

# Re-export from new locations for backward compatibility
from subtitle_providers.zimuku import ZimukuClient, ZimukuSubtitle
from subtitle_providers.utils import (
    parse_filename,
    compute_match_score,
    build_search_keyword,
    is_filename,
)


def cmd_search(args):
    client = ZimukuClient()
    try:
        video_info = None
        search_keyword = args.keyword

        if args.filename:
            video_info = parse_filename(args.keyword)
            if not video_info:
                print("[!] Could not parse filename as video file")
                return
            search_keyword = build_search_keyword(video_info)
            print(f"Parsed: {args.keyword} -> searching for '{search_keyword}'")
        elif is_filename(args.keyword):
            print(f"[Hint] Input looks like a filename. Use --filename for better matching.")

        results = client.search(search_keyword, season=args.season,
                                episode=args.episode, video_info=video_info)
        if not results:
            print("No subtitles found.")
            return

        print(f"\n{'=' * 60}")
        print(f"Found {len(results)} subtitle(s):")
        print(f"{'=' * 60}")
        for i, sub in enumerate(results, 1):
            score_str = f" [Score: {sub.score}]" if sub.score is not None else ""
            print(f"  [{i:3d}] [{sub.language:8s}] {sub.title}{score_str}")
            print(f"        URL: {sub.detail_url}")
            if args.verbose and sub.score is not None:
                video_type = video_info.get('type', 'movie') if video_info else 'movie'
                _, matched = compute_match_score(video_info or {}, sub.title, video_type)
                if matched:
                    print(f"        Matched: {', '.join(sorted(matched))}")
    finally:
        client.close()


def cmd_download(args):
    client = ZimukuClient()
    try:
        video_info = None
        search_keyword = args.keyword

        if args.filename:
            video_info = parse_filename(args.keyword)
            if not video_info:
                print("[!] Could not parse filename as video file")
                return
            search_keyword = build_search_keyword(video_info)
            print(f"Parsed: {args.keyword} -> searching for '{search_keyword}'")
        elif is_filename(args.keyword):
            print(f"[Hint] Input looks like a filename. Use --filename for better matching.")

        results = client.search(search_keyword, season=args.season,
                                episode=args.episode, video_info=video_info)
        if not results:
            print("No subtitles found.")
            return

        output_dir = Path(args.output)

        if args.interactive:
            print(f"\n{'=' * 60}")
            print(f"Found {len(results)} subtitle(s):")
            print(f"{'=' * 60}")
            for i, sub in enumerate(results, 1):
                score_str = f" [Score: {sub.score}]" if sub.score is not None else ""
                print(f"  [{i}] [{sub.language}] {sub.title}{score_str}")
                if args.verbose and sub.score is not None:
                    video_type = video_info.get('type', 'movie') if video_info else 'movie'
                    _, matched = compute_match_score(video_info or {}, sub.title, video_type)
                    if matched:
                        print(f"        Matched: {', '.join(sorted(matched))}")

            choice = input("\nEnter number to download (or 'all'): ").strip()
            if choice.lower() == "all":
                selected = results
            else:
                try:
                    idx = int(choice) - 1
                    selected = [results[idx]]
                except (ValueError, IndexError):
                    print("Invalid selection.")
                    return
        else:
            if video_info:
                lang_matches = [s for s in results if s.language == args.lang]
                if lang_matches and any(s.score is not None for s in lang_matches):
                    best = max(lang_matches, key=lambda s: s.score if s.score is not None else 0)
                    selected = [best]
                else:
                    selected = [next(
                        (s for s in results if s.language == args.lang), results[0]
                    )]
            else:
                lang_matches = [s for s in results if s.language == args.lang]
                if lang_matches and any(s.score is not None for s in lang_matches):
                    best = max(lang_matches, key=lambda s: s.score if s.score is not None else 0)
                    selected = [best]
                else:
                    selected = [next(
                        (s for s in results if s.language == args.lang), results[0]
                    )]

        for sub in selected:
            vfn = args.keyword if args.filename else None
            client.download(sub, output_dir, preferred_lang=args.lang,
                            video_filename=vfn)

    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Zimuku Subtitle Downloader - Standalone CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s search "The Dark Knight"
  %(prog)s search "Total.Eclipse.1995.Bluray-720p.x264.AAC.LAMA.mp4" --filename
  %(prog)s download "The Dark Knight" -o ./subs
  %(prog)s download "Total.Eclipse.1995.mp4" --filename -o ./subs
  %(prog)s download "Movie.mkv" --filename --verbose -o ./subs
  %(prog)s download "Game of Thrones" -s 1 -e 1 -o ./subs --interactive
  %(prog)s download "Movie Name" -l zho_chs -o ./subs
        """
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # search
    search_parser = subparsers.add_parser("search", help="Search for subtitles")
    search_parser.add_argument("keyword", help="Movie / TV show name")
    search_parser.add_argument("-s", "--season", type=int, help="Season number")
    search_parser.add_argument("-e", "--episode", type=int, help="Episode number")
    search_parser.add_argument("-f", "--filename", action="store_true",
                               help="Treat keyword as a video filename and parse it with guessit")
    search_parser.add_argument("-v", "--verbose", action="store_true",
                               help="Show detailed score breakdown for each subtitle")

    # download
    dl_parser = subparsers.add_parser("download", help="Download subtitles")
    dl_parser.add_argument("keyword", help="Movie / TV show name")
    dl_parser.add_argument("-o", "--output", default="./subtitles",
                           help="Output directory (default: ./subtitles)")
    dl_parser.add_argument("-s", "--season", type=int, help="Season number")
    dl_parser.add_argument("-e", "--episode", type=int, help="Episode number")
    dl_parser.add_argument("-l", "--lang", default="zho_chs",
                           choices=["zho_chs", "zho_cht", "eng", "unknown"],
                           help="Preferred language (default: zho_chs)")
    dl_parser.add_argument("-i", "--interactive", action="store_true",
                           help="Interactive mode: pick from results")
    dl_parser.add_argument("-f", "--filename", action="store_true",
                           help="Treat keyword as a video filename and parse it with guessit")
    dl_parser.add_argument("-v", "--verbose", action="store_true",
                           help="Show detailed score breakdown for each subtitle")

    args = parser.parse_args()

    if args.command == "search":
        cmd_search(args)
    elif args.command == "download":
        cmd_download(args)


if __name__ == "__main__":
    main()
