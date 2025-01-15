#!/usr/bin/env python3

import sys
import zipfile
import sqlite3
from dataclasses import dataclass
from operator import itemgetter
from pathlib import Path
from sqlite3 import Cursor

from matcher import ObjectListMatcher



CUR_PATH = Path()

TRANSFER_DOWNLOADED_EPISODES = True
EPISODES_DIR_PATH = '/storage/emulated/0/Android/data/de.danoeh.antennapod/files/media/from_podcast_addict'
MATCH_ON_EPISODE_URL_IF_COULD_NOT_FIND_A_MATCH_OTHERWISE = True


@dataclass
class Feed:
    id: int
    name: str
    description: str
    author: str
    keep_updated: int
    feed_url: str
    folder_name: str = ''


def error(msg):
    print("ERROR:", msg)
    sys.exit(1)


def confirmed(msg):
    inp = input(msg + " (y = yes)\n> ").strip().lower()
    return inp in ('y', 'yes')


def get_one_file_or_error(glob_pattern, path=CUR_PATH):
    files = list(path.glob(glob_pattern))
    if not files:
        error("found no file for the pattern: " + glob_pattern)
    if len(files) > 1:
        error("found more than one file for pattern: " + glob_pattern)
    return files[0]


def get_antenna_pod_and_podcast_addict_backup_path():
    podcast_addict_backup_file = get_one_file_or_error("PodcastAddict*.backup")
    antenna_pod_db_file = get_one_file_or_error("AntennaPodBackup*.db")

    path_to_extract_to = CUR_PATH / 'podcast_addict_extracted'

    podcast_addict_db_file = next(path_to_extract_to.glob("*.db"), None)
    if podcast_addict_db_file is None:
        path_to_extract_to.mkdir(exist_ok=True)

        if not zipfile.is_zipfile(podcast_addict_backup_file):
            error("somehow the Podcast Addict .backup file is not a zip")

        z = zipfile.ZipFile(podcast_addict_backup_file)

        print("Extracting Podcast Addict backup..")
        z.extractall(path=path_to_extract_to)

        podcast_addict_db_file = get_one_file_or_error(  #
                "*.db", path=path_to_extract_to)

    return antenna_pod_db_file, podcast_addict_db_file



def transfer(podcast_addict_cur: Cursor, antenna_pod_cur: Cursor):
    # first find match for all feeds in pa
    pa_feeds = [Feed(*a) for a in podcast_addict_cur.execute(
            'select _id, name, description, author, '
            'automaticRefresh, feed_url, folderName from podcasts '
            'where subscribed_status = 1 and is_virtual = 0 and initialized_status = 1')]

    print("# Podcast addict feeds:")
    for feed in pa_feeds:
        print(feed.name)
    print("\n\n")

    ap_feeds = {a[5]: Feed(*a) for a in antenna_pod_cur.execute(
            'select id, title, description, author, keep_updated, download_url from Feeds '
            )}

    pa_to_ap = []

    for n, pa in enumerate(pa_feeds):
        ap_name = '!!! NO MATCH !!!'
        pa_name = pa.name if pa.name else pa.feed_url
        if pa.feed_url in ap_feeds:
            ap = ap_feeds[pa.feed_url]
            ap_name = ap.name
            pa_to_ap.append((pa, ap))

        print(pa_name, ap_name, sep="  ->  ")
    print()

    if not confirmed("Is this correct? Can we continue?"):
        return

    # if you want to merge podcasts (e.g. non-premium and premium -> premium)
    #for pa in pa_feeds:
    #    if pa.name == "Name of non premium podcast feed":
    #        for ap in ap_feeds:
    #            # FIXME: make it work if premium and non-premium share same name
    #            if ap.name == "Name of same podcast but premium version":
    #                transfer_from_feed_to_feed(podcast_addict_cur,
    #                                           antenna_pod_cur, pa, ap)
    #                break
    #        break


    for pa, ap in pa_to_ap:
        transfer_from_feed_to_feed(podcast_addict_cur, antenna_pod_cur, pa, ap)
        print()  # break


ITEM_MATCHER = ObjectListMatcher({(lambda i: i[1]): 1})
ITEM_MATCHER.minimum_similarity = 0.83
ITEM_MATCHER.lock_in_if_similarity_first_above = 0.97


def transfer_from_feed_to_feed(podcast_addict_cur: Cursor,  #
                               antenna_pod_cur: Cursor,  #
                               pa: Feed,  #
                               ap: Feed):
    print(f'# Feed: {ap.name}')
    antenna_pod_cur.execute("UPDATE Feeds "
                            "SET keep_updated = ? "
                            "WHERE id = ?",  #
                            (pa.keep_updated, ap.id,))

    pa_episodes = list(podcast_addict_cur.execute(  #
            #        0   1     n2            n3       n4
            'select _id, name, seen_status, favorite, local_file_name, '
            # n5           n6           n7                  n8            n9
            'playbackDate, duration_ms, chapters_extracted, download_url, position_to_resume '
            'from episodes where podcast_id = ? and '
            '(seen_status = 1 or position_to_resume < 0 or '
            '(local_file_name != "" and local_file_name IS NOT NULL))',
            (pa.id,)))

    ap_episodes = list(antenna_pod_cur.execute(  #
            'select fi.id, fi.title, fm.download_url '
            'from FeedItems fi '
            'LEFT JOIN FeedMedia fm ON fi.id = fm.feeditem '
            'where fi.feed = ? and fi.read = 0 '
            , (ap.id,)))

    combinations = len(pa_episodes) * len(ap_episodes)
    print(f"\nRough estimate: {combinations / 4000:.2f} seconds\n\n")
    pa_indices = ITEM_MATCHER.get_indices(ap_episodes, pa_episodes)
    seen_match_count = 0


    for ap_ep, pa_idx in zip(ap_episodes, pa_indices):
        if pa_idx < 0:
            found = False

            # give it one last chance
            ap_url = ap_ep[2]
            if MATCH_ON_EPISODE_URL_IF_COULD_NOT_FIND_A_MATCH_OTHERWISE and ap_url is not None:
                ap_url = ap_url.strip()
                if len(ap_url) > 9:
                    for pa_idx_urlmatch, pa_ep in enumerate(pa_episodes):
                        if not pa_ep[8]:
                            continue

                        pa_url = pa_ep[8].strip()
                        if pa_url and pa_url == ap_url:
                            print(f"! Fallback to URL match for: {ap_ep[1]}")
                            found = True
                            pa_idx = pa_idx_urlmatch
                            break

            if not found:
                print(f"!  No match for: {ap_ep[1]}")
                continue

        seen_match_count += 1
        pa_ep = pa_episodes[pa_idx]
        if pa_ep[2]:
            transfer_from_seen_ep_to_ep(antenna_pod_cur, podcast_addict_cur,  #
                                        pa_ep, ap_ep)
        else:
            transfer_progress_ep_to_ep(antenna_pod_cur, podcast_addict_cur,  #
                                        pa_ep, ap_ep)


        if pa_ep[3]:
            antenna_pod_cur.execute(
                "INSERT INTO Favorites (feeditem, feed) VALUES "
                "(?, ?)", (ap_ep[0], ap.id))

        if pa_ep[4] and TRANSFER_DOWNLOADED_EPISODES:
            transfer_from_dld_ep_to_ep(antenna_pod_cur, podcast_addict_cur,  #
                                       pa_ep, ap_ep, pa.folder_name)

        if pa_ep[7]:
            transfer_chapters(antenna_pod_cur, podcast_addict_cur,  #
                              pa_ep, ap_ep, pa.id)

    print(f'\nINFO: In this feed {seen_match_count} episodes were matched')


def transfer_chapters(antenna_pod_cur: Cursor,  #
                      podcast_addict_cur: Cursor,  #
                      pa_ep: tuple,  #
                      ap_ep: tuple, pa_feed_id: int):
    for title, start in podcast_addict_cur.execute(  #
            "select name, start from chapters "
            "where podcastId = ? and episodeId = ?", (pa_feed_id, pa_ep[0])):
        antenna_pod_cur.execute("INSERT INTO SimpleChapters "
                                "(title, start, feeditem) VALUES "
                                "(?, ?, ?)", (title, start, ap_ep[0]))


def transfer_from_dld_ep_to_ep(antenna_pod_cur: Cursor,  #
                               podcast_addict_cur: Cursor,  #
                               pa_ep: tuple,  #
                               ap_ep: tuple,  #
                               pa_folder_name: str):
    pa_ep_id, _, _, _, pa_local_file_name, _, _, _, _, _ = pa_ep

    dir_path = EPISODES_DIR_PATH.rstrip("/") + "/" + pa_folder_name
    file_path = dir_path + "/" + pa_local_file_name
    antenna_pod_cur.execute("UPDATE FeedMedia "
                            "SET file_url = ?, "
                            "downloaded = 1 "
                            "WHERE feeditem = ?",  #
                            (file_path, ap_ep[0],))


def transfer_from_seen_ep_to_ep(antenna_pod_cur: Cursor,  #
                                podcast_addict_cur: Cursor,  #
                                pa_ep: tuple,  #
                                ap_ep: tuple):
    print(ap_ep[1], "  <<matched to seen>>  ", pa_ep[1])
    pa_ep_id, _, _, _, _, pa_playbackDate, pa_duration_ms, _, _, _ = pa_ep
    antenna_pod_cur.execute("UPDATE FeedItems SET read = 1 WHERE id = ?",
                            (ap_ep[0],))

    antenna_pod_cur.execute("UPDATE FeedMedia "
                            "SET playback_completion_date = ?, "
                            "last_played_time = ?, "
                            "played_duration = ? "
                            "WHERE feeditem = ?",  #
                            (pa_playbackDate, pa_playbackDate, pa_duration_ms,
                             ap_ep[0],))

def transfer_progress_ep_to_ep(antenna_pod_cur: Cursor,
                                podcast_addict_cur: Cursor,
                                pa_ep: tuple,
                                ap_ep: tuple):
    print(ap_ep[1], "  <<matched to in-progress>>  ", pa_ep[1])
    pa_ep_id, _, _, _, _, pa_playbackDate, pa_duration_ms, _, _, pa_position = pa_ep

    antenna_pod_cur.execute("UPDATE FeedMedia "
                            "SET last_played_time = ?, "
                            "position = ?, "
                            "played_duration = ? "
                            "WHERE feeditem = ?",
                            (pa_playbackDate, pa_position, pa_position,
                             ap_ep[0],))


ap_db, pa_db = get_antenna_pod_and_podcast_addict_backup_path()
print("\nAntennaPod .db file found:", ap_db)
print("Podcast Addict .db file found:", pa_db)
print("\n")

podcast_addict_con = None
antenna_pod_con = None
try:
    podcast_addict_con = sqlite3.connect(pa_db)
    antenna_pod_con = sqlite3.connect(ap_db)

    transfer(podcast_addict_con.cursor(), antenna_pod_con.cursor())
finally:
    antenna_pod_con.commit()

    if podcast_addict_con is not None:
        antenna_pod_con.close()

    if antenna_pod_con is not None:
        podcast_addict_con.close()
