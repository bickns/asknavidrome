"""
Free-text search resolution for AskNavidrome.

Handles turning whatever Alexa hands back in a NaviSonicPlaySearch,
NaviSonicQueueSearch or NaviSonicInsertSearch slot into an actual set
of Navidrome song IDs. Subsonic's search3 API is the only real
free-text search primitive available - there's no separate "smarter"
endpoint - so everything here is client-side scoring and fallback
logic layered on top of it: punctuation-normalized retry variants,
local fuzzy spelling correction against the cached artist list, and a
priority chain (song, then album, then artist, then genre) with a
minimum match-confidence score at every step so a coincidental
low-quality match never wins over a genuinely better one further down
the chain.

A matched SONG resolves to exactly that one song. Choosing what to
play after it is not done here - when the queue runs dry, handlers.py
asks pick_similar_song() for the next song just in time.

Note: this module only governs behaviour once Alexa has already
routed a request to one of the search intents. It has no influence
over which intent Alexa chooses to route a given utterance to in the
first place.
"""

from difflib import SequenceMatcher
import random
import re

from asknavidrome.config import (
    connection,
    logger,
    known_genres,
    known_artists,
    min_song_count,
    SEARCH_RESULT_COUNT,
    MIN_ACCEPTABLE_SCORE,
    ARTIST_SPELLING_CORRECTION_THRESHOLD,
)


def genre_matches(query, genre_name):
    """Whole-word match against a genre name.

    Avoids false positives from raw substring matching - e.g. "dance"
    would wrongly match "dancehall" under a naive `in` check, since
    "dance" is a substring of "dancehall" but they are unrelated
    genres. Splitting on whitespace/hyphen/ampersand and comparing
    whole tokens avoids this while still correctly matching "dance"
    against "dance-pop" (which does split into legitimate tokens).

    :param str query: The spoken/typed genre term
    :param str genre_name: A genre name from the known_genres cache
    :return: True if query matches one of genre_name's whole tokens
    :rtype: bool
    """
    tokens = re.split(r'[\s\-&]+', genre_name.lower())
    return query.lower() in tokens


def parse_query_for_artist(text: str):
    """Split 'song by artist' style phrasing out of a free-text query.

    Strips optional filler words ("the song", "the band", "the artist")
    so both terse ("X by Y") and wordier ("the song X by the artist Y")
    phrasings resolve to clean search terms.

    :param str text: The raw query text as spoken/typed
    :return: (song_text, artist_text) if a ' by ' pattern is found,
        otherwise (text, None)
    :rtype: tuple[str, str | None]
    """

    match = re.search(
        r'^(?:the\s+song\s+)?(.*?)\s+by\s+(?:the\s+(?:band|artist)\s+)?(.*)$',
        text, re.IGNORECASE
    )
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return text, None


def _search_variants(term: str):
    """Generate normalized variants of a search term to work around
    common tagging punctuation mismatches (e.g. 'and' vs '&').

    :param str term: The original search term
    :return: A list of variants to try, starting with the original
    :rtype: list[str]
    """

    variants = [term]

    if re.search(r'\band\b', term, re.IGNORECASE):
        variants.append(re.sub(r'\band\b', '&', term, flags=re.IGNORECASE))
    if '&' in term:
        variants.append(term.replace('&', 'and'))

    no_punct = re.sub(r"[,\.]", '', term)
    if no_punct != term:
        variants.append(no_punct)

    return variants


def correct_artist_spelling(term: str, threshold: float = ARTIST_SPELLING_CORRECTION_THRESHOLD):
    """Find the closest known artist name to term using local fuzzy
    matching, for cases where speech-to-text produces a plausible but
    incorrectly-spelled name (e.g. spoken "gipsy kings" vs the tagged
    "Gypsy Kings").

    :param str term: The spoken/typed artist name
    :param float threshold: Minimum similarity ratio required to accept a match
    :return: The corrected name, or None if nothing scores above threshold
    :rtype: str | None
    """

    if not known_artists:
        return None

    best_match = None
    best_score = 0.0

    for name in known_artists:
        score = SequenceMatcher(None, term.lower(), name.lower()).ratio()
        if score > best_score:
            best_score = score
            best_match = name

    if best_score >= threshold:
        logger.debug(f'correct_artist_spelling: "{term}" -> "{best_match}" (score {best_score:.2f})')
        return best_match
    return None


def fuzzy_search_song(term: str):
    """Try search_song() against normalized variants of term until one
    returns a result.

    :param str term: The song title to search for
    :return: A list of matching song dicts, or None if nothing matched
    :rtype: list | None
    """
    for variant in _search_variants(term):
        logger.debug(f'fuzzy_search_song: trying variant "{variant}"')
        result = connection.search_song(variant, SEARCH_RESULT_COUNT)
        logger.debug(f'fuzzy_search_song: variant "{variant}" returned {len(result) if result else 0} results')
        if result:
            logger.info(f'fuzzy_search_song("{term}"): {len(result)} results (via variant "{variant}")')
            return result
    logger.info(f'fuzzy_search_song("{term}"): no results')
    return None


def fuzzy_search_artist(term: str):
    """Try search_artist() against normalized variants, then local fuzzy
    spelling correction, then finally fall back to inspecting song
    search results directly - this catches artists who exist as
    plain-text metadata on tracks but were never indexed as a
    standalone artist entity.

    :param str term: The artist name to search for
    :return: A list of matching artist dicts, or None if nothing matched
    :rtype: list | None
    """

    for variant in _search_variants(term):
        logger.debug(f'fuzzy_search_artist: trying variant "{variant}"')
        result = connection.search_artist(variant)
        logger.debug(f'fuzzy_search_artist: variant "{variant}" returned {len(result) if result else 0} results')
        if result:
            logger.info(f'fuzzy_search_artist("{term}"): {len(result)} results (via variant "{variant}") - top match: {result[0].get("name")!r} (id={result[0].get("id")})')
            return result

    corrected = correct_artist_spelling(term)
    if corrected and corrected.lower() != term.lower():
        logger.debug(f'fuzzy_search_artist: no direct match for "{term}", trying spelling-corrected "{corrected}"')
        result = connection.search_artist(corrected)
        if result:
            logger.info(f'fuzzy_search_artist("{term}"): {len(result)} results via spelling correction to "{corrected}"')
            return result

    logger.debug(f'fuzzy_search_artist: no indexed match for "{term}", trying song-metadata fallback')
    song_results = connection.search_song(term, SEARCH_RESULT_COUNT)
    if song_results:
        for song in song_results:
            song_artist = song.get('artist', '')
            if _match_score(term, song_artist) >= 3.0 and song.get('artistId'):
                logger.info(f'fuzzy_search_artist("{term}"): matched via song-metadata fallback - "{song_artist}" (id={song.get("artistId")}) via track "{song.get("title")}"')
                return [{'id': song.get('artistId'), 'name': song_artist}]

    logger.info(f'fuzzy_search_artist("{term}"): no results via any method')
    return None


def fuzzy_search_album(term: str):
    """Try search_album() against normalized variants of term until one
    returns a result.

    :param str term: The album title to search for
    :return: A list of matching album dicts, or None if nothing matched
    :rtype: list | None
    """
    for variant in _search_variants(term):
        logger.debug(f'fuzzy_search_album: trying variant "{variant}"')
        result = connection.search_album(variant)
        logger.debug(f'fuzzy_search_album: variant "{variant}" returned {len(result) if result else 0} results')
        if result:
            logger.info(f'fuzzy_search_album("{term}"): {len(result)} results (via variant "{variant}")')
            return result
    logger.info(f'fuzzy_search_album("{term}"): no results')
    return None


def _normalize_for_comparison(text: str) -> str:
    """Strip punctuation that commonly differs between how a title is
    spoken and how it's actually tagged (commas, periods, apostrophes),
    and collapse whitespace. Used by _match_score so that e.g. "paint
    it black" and "Paint It, Black" are treated as the same string for
    exact/startswith/contains comparison, rather than only ever being
    comparable via a fuzzy ratio (which can't reach the score tiers
    that represent real confidence - see _match_score).

    :param str text: The string to normalize
    :return: The normalized string
    :rtype: str
    """
    text = text.lower().strip()
    text = re.sub(r"[,.'\u2018\u2019]", '', text)
    text = re.sub(r'\s+', ' ', text)
    return text


def _match_score(query: str, candidate: str) -> float:
    """Score how well candidate matches query, for ranking search results
    client-side rather than trusting whatever order the server returned
    them in.

    Punctuation (commas, periods, apostrophes) is stripped from both
    sides before comparing, so a trivial difference between spoken
    phrasing and the actual tag (e.g. "paint it black" vs "Paint It,
    Black") doesn't prevent an otherwise-exact match from reaching the
    high-confidence score tiers.

    :param str query: The search term
    :param str candidate: A candidate string to score against it
    :return: 4.0 for an exact match, 3.0 for a prefix match, 2.0 for
        a substring match, otherwise a SequenceMatcher similarity ratio
    :rtype: float
    """

    q = _normalize_for_comparison(query)
    c = _normalize_for_comparison(candidate)

    if q == c:
        return 4.0
    if c.startswith(q):
        return 3.0
    if q in c:
        return 2.0
    return SequenceMatcher(None, q, c).ratio()


def _play_count(song: dict) -> int:
    """Get a song's play count from a search result dict.

    Navidrome omits the playCount key entirely when a track has never
    been played, so a missing (or unparseable) value is treated as 0.
    Note the count is per Navidrome user - it reflects whichever
    account the skill is logged in as.

    :param dict song: A song dict from a Subsonic search result
    :return: The play count, or 0 if absent
    :rtype: int
    """
    try:
        return int(song.get('playCount') or 0)
    except (TypeError, ValueError):
        return 0


def best_song_match(query: str, songs: list) -> list:
    """Re-order a list of song dicts so the best match to query is first.

    Songs are ordered by match score first. Where scores tie exactly
    (e.g. two different recordings both titled "September", each
    scoring 4.0), the more-played track comes first. If play counts
    also tie, the order the server returned them in is preserved
    (Python's sort is stable).

    :param str query: The search term
    :param list songs: A list of song dicts (each must have a 'title' key)
    :return: The same songs, sorted by descending score, then play count
    :rtype: list
    """
    return sorted(
        songs,
        key=lambda s: (_match_score(query, s.get('title', '')), _play_count(s)),
        reverse=True
    )


def filter_to_best_matches(query: str, songs: list, threshold: float = 0.15) -> list:
    """Keep only songs whose match score is close to the best match's
    score, discarding loosely-related results.

    No longer used by resolve_search() - a song match now resolves to
    the single best song. Retained pending dead-code cleanup.

    :param str query: The search term
    :param list songs: A list of song dicts
    :param float threshold: Maximum score gap from the top match to still be kept
    :return: The subset of songs within threshold of the best score
    :rtype: list
    """

    scored = [(s, _match_score(query, s.get('title', ''))) for s in songs]
    scored.sort(key=lambda pair: pair[1], reverse=True)

    top_score = scored[0][1]
    return [s for s, score in scored if top_score - score <= threshold]


def best_album_match(query: str, albums: list):
    """Pick the single best-matching album dict from a list of
    candidates, rather than combining every loosely-matched album's
    songs together.

    :param str query: The search term
    :param list albums: A list of album dicts (each must have a 'name' key)
    :return: The single best-matching album dict
    :rtype: dict
    """
    return max(albums, key=lambda a: _match_score(query, a.get('name', '')))


def expand_single_track_album(song_id_list: list, fallback_description: str):
    """If a match collapsed to a single track, pad it out with similar
    songs so playback doesn't stop dead after one track.

    No longer used by resolve_search(). Only the old
    NaviSonicPlayAlbumByArtist handler (dead code awaiting cleanup)
    still calls it.

    :param list song_id_list: Song IDs from a match
    :param str fallback_description: Description to use if the song's
        own details can't be looked up
    :return: (song_id_list, description) - unchanged if len != 1,
        otherwise the padded list and an updated description
    :rtype: tuple[list, str]
    """
    if len(song_id_list) != 1:
        return song_id_list, fallback_description

    song_id = song_id_list[0]
    song_details = connection.get_song_details(song_id).get('song', {})
    song_title = song_details.get('title', fallback_description)
    song_artist = song_details.get('artist', 'an unknown artist')

    similar_ids = connection.get_similar_songs(song_id, min_song_count)
    combined = [song_id] + [s for s in similar_ids if s != song_id]

    if len(combined) > 1:
        return combined, f'{song_title} by {song_artist} and similar songs'
    return combined, f'{song_title} by {song_artist}'


def pick_similar_song(seed_ids: list, exclude_ids: set):
    """Choose ONE song to play next, based on songs similar to the seeds.

    Each seed is sent to Navidrome's similarity lookup. Every candidate
    is then scored by how highly it ranks in each seed's list: a song
    that appears near the top of several seeds' lists beats a song that
    tops only one. This is what makes the recent songs matter, rather
    than the first seed's list always winning. Anything in exclude_ids
    is never chosen.

    A failure looking up one seed is logged with its traceback and that
    seed is skipped; the others carry on.

    :param list seed_ids: Song IDs to seed from, most important first
    :param set exclude_ids: Song IDs that must not be chosen
    :return: The chosen song ID, or None if nothing qualifies
    :rtype: str | None
    """
    scores = {}

    for seed in seed_ids:
        try:
            similar = connection.get_similar_songs(seed, SEARCH_RESULT_COUNT)
        except Exception:
            logger.exception(f'pick_similar_song: similar-songs lookup failed for seed {seed} - skipping it')
            similar = []

        logger.info(f'pick_similar_song: seed {seed} -> {len(similar)} similar songs')

        for rank, candidate in enumerate(similar):
            if candidate in exclude_ids:
                continue
            scores[candidate] = scores.get(candidate, 0.0) + 1.0 / (rank + 1)

    if not scores:
        logger.info(f'pick_similar_song: no candidate left after excluding {len(exclude_ids)} songs')
        return None

    chosen = max(scores, key=scores.get)
    logger.info(f'pick_similar_song: chose {chosen} (score {scores[chosen]:.2f}) from {len(scores)} candidates')
    return chosen


def resolve_search(query):
    """
    Resolve a free-text query against songs, then albums, then artists,
    then genre, in that priority order. Every step logs at INFO level
    what was searched and how many results came back, so search
    behaviour is diagnosable directly from the log without needing
    NAVI_DEBUG turned up.

    If the query contains "X by Y" phrasing, X and Y are extracted; the
    artist is looked up first, and an artist-scoped song or album match
    is tried before falling through to the generic steps below (which
    use the cleaned song/album text, not the full noisy original
    query). This artist-scoped branch keeps its own internal order
    (song, then album) rather than following the same song/album/
    artist/genre priority as the generic steps, since the artist is
    already known at that point.

    A song-title or album-title match is only accepted if its score
    meets MIN_ACCEPTABLE_SCORE - a weak fuzzy hit no longer wins by
    default, allowing a genuinely better match further down the chain
    to be used instead of being hijacked by a loosely-matching one.

    A matched song resolves to that ONE song (highest score, then
    most-played, see best_song_match()). Other tracks that merely share
    the title are not included, and nothing is padded on - choosing
    what follows is left to pick_similar_song().

    :param query: The Alexa slot object holding the spoken/typed query text
    :return: (song_id_list, description) or (None, None) if nothing found
    :rtype: tuple[list, str] | tuple[None, None]
    """

    song_text, artist_text = parse_query_for_artist(query.value)
    search_text = song_text if artist_text else query.value

    logger.info(f'resolve_search: query="{query.value}" -> search_text="{search_text}" artist_text={artist_text!r}')

    if artist_text:
        artist_results = fuzzy_search_artist(artist_text)
        logger.info(f'resolve_search: artist search for "{artist_text}" -> {len(artist_results) if artist_results else 0} matches')

        if artist_results:
            artist_id = artist_results[0].get('id')
            logger.info(f'resolve_search: using artist id={artist_id} name={artist_results[0].get("name")!r}')

            song_results = fuzzy_search_song(search_text)
            logger.info(f'resolve_search: song search for "{search_text}" -> {len(song_results) if song_results else 0} matches (pre artist-filter)')

            song_dets = [item for item in song_results if item.get('artistId') == artist_id] if song_results else []
            logger.info(f'resolve_search: {len(song_dets)} of those songs belong to artist id={artist_id}')

            if song_dets:
                song_dets = best_song_match(search_text, song_dets)
                best = song_dets[0]
                top_score = _match_score(search_text, best.get('title', ''))
                logger.info(
                    f'resolve_search: artist-scoped SONG step best match "{best.get("title")}" '
                    f'(album: {best.get("album")!r}, playCount={_play_count(best)}) '
                    f'scored {top_score:.2f} against "{search_text}"'
                )

                if top_score >= MIN_ACCEPTABLE_SCORE:
                    logger.info(f'resolve_search: matched via artist-scoped SONG step - "{best.get("title")}"')
                    return [best.get('id')], f'{best.get("title")} by {best.get("artist", artist_text)}'
                else:
                    logger.info(f'resolve_search: artist-scoped SONG step score too weak ({top_score:.2f} < {MIN_ACCEPTABLE_SCORE}) - trying album instead')

            artist_album_lookup = connection.albums_by_artist(artist_id)
            logger.info(f'resolve_search: artist has {len(artist_album_lookup) if artist_album_lookup else 0} albums in library')

            if artist_album_lookup:
                candidate = best_album_match(search_text, artist_album_lookup)
                score = _match_score(search_text, candidate.get('name', ''))
                logger.info(
                    f'resolve_search: best album candidate "{candidate.get("name")}" '
                    f'(id={candidate.get("id")}, songCount={candidate.get("songCount")}) '
                    f'scored {score:.2f} against "{search_text}"'
                )

                if score >= MIN_ACCEPTABLE_SCORE:
                    song_id_list = connection.build_song_list_from_albums([candidate], -1)
                    logger.info(f'resolve_search: build_song_list_from_albums returned {len(song_id_list)} song IDs for "{candidate.get("name")}"')
                    return song_id_list, f'the album {candidate.get("name")} by {artist_text}'
        else:
            logger.info(f'resolve_search: no artist match for "{artist_text}" - falling through to generic search')
        # falls through to the generic search below, using search_text

    # 1. Song title
    song_results = fuzzy_search_song(search_text)
    logger.info(f'resolve_search: SONG step for "{search_text}" -> {len(song_results) if song_results else 0} matches')
    if song_results:
        song_results = best_song_match(search_text, song_results)
        best = song_results[0]
        top_score = _match_score(search_text, best.get('title', ''))
        logger.info(
            f'resolve_search: SONG step best match "{best.get("title")}" '
            f'by {best.get("artist")!r} (album: {best.get("album")!r}) scored {top_score:.2f} '
            f'(playCount={_play_count(best)})'
        )

        if top_score >= MIN_ACCEPTABLE_SCORE:
            logger.info(f'resolve_search: SONG step accepted - "{best.get("title")}" by {best.get("artist")!r}')
            return [best.get('id')], f'{best.get("title")} by {best.get("artist", "an unknown artist")}'
        else:
            logger.info(f'resolve_search: SONG step score too weak ({top_score:.2f} < {MIN_ACCEPTABLE_SCORE}) - trying album instead')

    # 2. Album title
    album_results = fuzzy_search_album(search_text)
    logger.info(f'resolve_search: ALBUM step for "{search_text}" -> {len(album_results) if album_results else 0} matches')
    if album_results:
        best_album = best_album_match(search_text, album_results)
        score = _match_score(search_text, best_album.get('name', ''))
        logger.info(
            f'resolve_search: ALBUM step best match "{best_album.get("name")}" '
            f'(id={best_album.get("id")}, songCount={best_album.get("songCount")}) scored {score:.2f}'
        )

        if score >= MIN_ACCEPTABLE_SCORE:
            song_id_list = connection.build_song_list_from_albums([best_album], -1)
            logger.info(f'resolve_search: ALBUM step build_song_list_from_albums returned {len(song_id_list)} song IDs')
            return song_id_list, f'the album {best_album.get("name")} by {best_album.get("artist", "an unknown artist")}'
        else:
            logger.info(f'resolve_search: ALBUM step score too weak ({score:.2f} < {MIN_ACCEPTABLE_SCORE}) - trying artist instead')

    # 3. Artist name
    artist_results = fuzzy_search_artist(search_text)
    logger.info(f'resolve_search: ARTIST step for "{search_text}" -> {len(artist_results) if artist_results else 0} matches')
    if artist_results:
        artist_album_lookup = connection.albums_by_artist(artist_results[0].get('id'))
        song_id_list = connection.build_song_list_from_albums(artist_album_lookup, SEARCH_RESULT_COUNT)
        logger.info(f'resolve_search: ARTIST step matched "{artist_results[0].get("name")}" - {len(song_id_list)} songs pulled')
        random.shuffle(song_id_list)
        return song_id_list[:SEARCH_RESULT_COUNT], f'music by {search_text}'

    # 4. Genre - last resort. Whole-word match against known genres,
    # catching subgenres too (e.g. "rock" also matches "hard rock")
    # without false positives from raw substring matching.
    matched_genres = [g for g in known_genres if genre_matches(search_text, g)]
    logger.info(f'resolve_search: GENRE step for "{search_text}" -> matched genres: {matched_genres}')
    if matched_genres:
        all_songs = []
        for g in matched_genres:
            all_songs.extend(connection.build_song_list_from_genre(g, min_song_count))
        logger.info(f'resolve_search: GENRE step - {len(all_songs)} songs pulled across {len(matched_genres)} matched genres')
        if all_songs:
            random.shuffle(all_songs)
            return all_songs[:SEARCH_RESULT_COUNT], f'{search_text} music'

    logger.info(f'resolve_search: no match found for "{query.value}" at any step')
    return None, None