import app

# --- genre_matches() - pure function, no API calls, test the actual bug fix ---
app.genre_matches('rock', 'hard rock')      # expect True
app.genre_matches('rock', 'classic rock')   # expect True
app.genre_matches('dance', 'dancehall')     # expect False - this is the fix
app.genre_matches('dance', 'dance-pop')     # expect True
app.genre_matches('pop', 'synth-pop')       # expect True


# --- known_genres / DEFAULT_GENRES - confirm your edited list and real fetch ---
len(app.known_genres)          # expect 164, confirms real fetch (not fallback)
app.DEFAULT_GENRES             # confirms your edits took effect
'jazz' in app.DEFAULT_GENRES   # expect False - you removed it


# --- resolve_search() - the actual thing everything depends on ---
from unittest.mock import Mock

def fake_slot(value):
    """resolve_search expects slot objects with a .value attribute,
    same shape as what get_slot_value_v2() returns - fake one quickly
    rather than going through the whole Alexa request machinery."""
    s = Mock()
    s.value = value
    return s

# Song title match - should hit step 1
song_ids, desc = app.resolve_search(fake_slot('bohemian rhapsody'))
print(desc, len(song_ids) if song_ids else None)

# Artist match - should hit step 2 (pick something NOT also a song title)
song_ids, desc = app.resolve_search(fake_slot('go west'))
print(desc, len(song_ids) if song_ids else None)

# Genre match - should hit step 3 (pick something from your known_genres list)
song_ids, desc = app.resolve_search(fake_slot('rock'))
print(desc, len(song_ids) if song_ids else None)

# Nothing at all - should return (None, None)
song_ids, desc = app.resolve_search(fake_slot('this definitely does not exist xyz123'))
print(song_ids, desc)   # expect None None

# Artist-scoped song search (the artist= parameter path)
song_ids, desc = app.resolve_search(fake_slot('freedom'), fake_slot('robert miles'))
print(desc, len(song_ids) if song_ids else None)