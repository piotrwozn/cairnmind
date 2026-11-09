# Repozytorium pilotażowe DevBrain

Jedno zadanie do przekazania między świeżymi sesjami: Codex implementuje, Claude
testuje i wykonuje review, kolejny Codex odtwarza wynik ze wspólnej pamięci.

Definicja zadania i kryteria są w `pilot-plan.json`. Profil `unit` jest w
`.devbrain/checks.toml`. Implementacja i testy zostaną utworzone przez pierwszego
Codexa; początkowy katalog celowo ich nie zawiera.

Docelowe API: `token_auth.validate_token(token) -> bool`. Wyłącznie napis `valid`
daje `True`. Inne napisy i dane innego typu dają `False`, bez wyjątku. To mały
przykład pilotażowy służący sprawdzeniu środowiska.

W `tests/test_token_auth.py` trzeba napisać co najmniej 12 metod testowych unittest:
poprawny token, pusty napis, zły token, inne wielkości liter, spacja z przodu,
spacja z tyłu, `None`, liczby całkowite, bool, bytes, lista i słownik. Każda metoda
ma sprawdzać rzeczywisty wynik funkcji. Kod wyjścia 0 z pustego zestawu testów nie
spełnia kryteriów; reviewer czyta kod testów oraz podsumowanie stdout/stderr.

Raporty, kopie plików i notatki z pilotażu trafiają poza repozytorium, do katalogu
`DEVBRAIN_PILOT_REPORT_DIR`. Zmiana kodu, testów lub profilu po sprawdzeniu powoduje
potrzebę ponownej weryfikacji; stan `done` i świeżość dowodu są odrębne.
