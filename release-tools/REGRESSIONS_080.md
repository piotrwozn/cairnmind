# Kontrolowane regresje 0.8

Stan: **prepared / not_run**. Zapisane helpery ani AST nie stanowią wyniku testów. Nie uruchamiaj ich przed zakończoną bramką runtime `pass`. Każda faza wymaga osobnego świeżego katalogu dowodów i uruchomienia przez `run-supervised-080.ps1`; `python_launcher` to literalny interpreter venv0.8, którego base/SHA odpowiadają gate. Ostateczny wynik wymaga zewnętrznego `receipt.json` po rzeczywistym wyjściu procesów i obserwacji pustego scope.

Kontroler: `run-regressions-080.py`. Wspólne argumenty:

```text
--execute --phase guards|ux|frontend|recall --run-id ID
--artifact-root /mnt/c/.../ID --source-root /home/piotr/.../frozen-devbrain
--private-root /home/piotr/.cache/devbrain-regressions-080
```

`--private-root` musi wcześniej istnieć jako własny katalog Linux0700. Kontroler tworzy w nim nowy katalog losowy i prywatny HOME. Krótki własny katalog `/tmp/r8-<uuid>`0700 jest wspólnym TMPDIR rodzica/dzieci oraz cache `tempfile`, aby sockety tmux mieściły się w limicie100bajtów. Nie otwiera produkcyjnej bazy, profili ani OAuth.

| Faza | Dodatkowe argumenty | Zakres i warunek |
|---|---|---|
| `guards` | `--updater /.../preflight-candidate/devbrain-update-0.8.0.py` | Wszystkie8 bieżących modułów guardów0.8, collect+execution przez literalny venv `-I`, `pytest --noconftest`, JUnit i zero pominięć. Bez silnika/modeli; testy własności updatera mogą tworzyć krótkie nieszkodliwe procesy Python. Manifest wiąże testy, zależne helpery i SHA jawnego kandydata. Archiwalne071/072, smoke i interaktywny fixture nadzoru nie są uruchamiane. Kandydat preflight nie jest publikacją ani kwalifikacją wydania. |
| `ux` | `--arcade-dist /.../przypięta-dystrybucja` | Pełne moduły `test_terminal*`, `test_workspace*`, `test_cockpit*`, `test_graph*`, `test_api_workspace_e2e.py`, `test_api_worker.py`, `test_client_runtime.py`, `test_runtime_pane.py`. Normalny conftest, kwalifikowane Java/tmux, własny silnik. Osobne collection i execution muszą mieć identyczne nodeids, bez pominięć/błędów. Obejmuje obie wielkości testu latency; nie zastępuje zadań UX z człowiekiem. |
| `frontend` | `--node /.../bin/node --npm-cli /.../npm/bin/npm-cli.js --chromium /.../rzeczywisty-binarny-chromium` | Świeża kopia źródeł, `npm ci` z lockfile, pełne unit/build/Playwright oraz audyty all/runtime. CI=1, jeden worker, Chromium `--disable-gpu`, osobny Vite na losowym porcie, mocki HTTP/WebSocket, brak proxy do produkcyjnego backendu. Build trafia tylko do własnego scratch/dist. Node i Chromium muszą być rzeczywistymi plikami ELF; wersje i SHA są raportowane. Hash npm dotyczy wejściowego pliku CLI. Oba audyty są zbierane także przy wykrytych podatnościach; nonzero daje FAIL. |
| `recall` | `--arcade-dist /.../przypięta-dystrybucja --chat-model qwen3.5:9b --embedding-model qwen3-embedding:0.6b` | Rzeczywiste `evaluation.run_evaluation(clients=False)` i Ollama. Wymaga20 prób, minimum90% trafności, wszystkich krytycznych faktów i deterministycznej izolacji. Oryginalny scorer jest wywoływany dokładnie raz; Progress zapisuje wyniki poszczególnych przypadków. RAM/GPU monitorowane co5s do zakończenia. Nie startuje ani nie zatrzymuje usługi Ollama. |

Recall może otrzymać jawne `--ollama-url http://127.0.0.1:11434` i `--num-ctx 16384`. Są to parametry testu; nie zmieniają ustawień produkcji. Źródła, gate i runtime są sprawdzane przed/po etapach. JVM i serwer frontendowy są kończone wyłącznie przez zachowany pidfd z ponownym sprawdzeniem właściciela/cgroup. Kontroler obserwuje30s bez pozostawionych własnych dzieci; zewnętrzny launcher osobno obserwuje jego finalizację. Wymuszony cleanup albo utrata dowodu daje FAIL.

W `required_artifacts` wrappera zawsze podaj `gate.json`, `regression-source.json`, `regressions-results.json`, `controller-report.json`, `supervisor-report.json`, a dodatkowo:

- Guards: `guards-collection.json`, `guards.xml`.
- UX: `ux-collection.json`, `ux.xml`.
- Frontend: `frontend-unit.xml`, `frontend-browser.xml`, `frontend-build-manifest.json`, `frontend-audit-all.json`, `frontend-audit-runtime.json`.
- Recall: `recall-results.json`, `recall/checkpoint.json`, `recall/cases.jsonl`, `recall/resources.jsonl`.

Końcowy status kontrolera to `regressions_completed_pending_external_watch`. Raport ma scope `devbrain_targeted_regressions`, jawne `phase`, `full_suite_pass=false` i `eight_hour_qualification=false`. Zaliczenie jednej fazy nie oznacza zaliczenia pozostałych, pełnego zestawu backendowego ani ośmiu godzin stabilności. Raporty i JUnit pozostają w katalogu dowodów; prywatne katalogi testowe są zachowywane do kontroli operatora.

Jawny zestaw guards: `test_account080_guards.py`, `test_devbrain_update080_guards.py`, `test_full_suite_080_guards.py`, `test_interactive_ux080_guards.py`, `test_queue_replay080_guards.py`, `test_regressions080_guards.py`, `test_scope_080_guards.py`, `test_stability_080_guards.py`. Dodany nowy moduł wymaga aktualizacji tej listy w kontrolerze; nie zostanie po cichu pominięty. Historyczne testy i ich ścieżki nie są modyfikowane ani aliasowane.
