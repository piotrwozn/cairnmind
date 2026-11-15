# Results of `cairnmind eval repository` on Cairnmind

**The acceptance figure of issue #104, sixteen of twenty questions complete and right in the worst of three runs, is met with the default models: 18, 18 and 18 of 20 on the final tree, after the merge of main, with no complete and wrong answer.** The tree one commit of main earlier gave 20, 18 and 19. The same day, measured the same way, the tree before these changes gave 13, 13 and 15. On 22 further questions the figure rose from 13 right to 19 before the merge of main; after it two runs gave 16 right with 2 complete and wrong, and 18 right with none wrong. That set is no longer unseen by the work (see the section).

**The default chat model stays `qwen3.5:9b` with a context of 16,384 tokens. That is the owner's decision of 15 November 2026; no other model is adopted or recommended.** The other models that were measured are listed below as measured and not adopted.

This page is kept out of the copy that the evaluation indexes (see [../EVALUATION.md](../EVALUATION.md)), so that a run cannot read the results of earlier runs. It reports the questions that failed by topic, not by their answers.

## After the citation, definition and second-look changes

Measured on 15 November 2026 on an NVIDIA RTX 4080 Super (16 GB) under WSL2 with Ollama 0.40.14, `qwen3.5:9b` (digest 56671c2a…, 4-bit, context 16,384 tokens) and `qwen3-embedding:0.6b` (digest ac6da0df…), unless a row names another model. `ask_timeout` was 600 seconds as in the earlier runs; with the default model no question of the runs in the table below came near the default of 60 (the slowest took 19.6 seconds).

### The acceptance figure

Three runs of the code path of `cairnmind eval repository` per tree, each run with its own copy, index and database:

| Tree | Indexed | Run 1 | Run 2 | Run 3 | Worst | Complete and wrong | Seconds per question (median) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| main before the changes (4527643) | 312 files, 1,756 chunks | 13 | 13 | 15 | 13 | 0 | 6.6 to 7.0 |
| the changes without the last fix of the second look (6ca88dd) | 315 files, 1,766 chunks | 19 | 18 | 20 | 18 | 0 | 5.1 to 5.4 |
| the changes before the merge of main (41e70ae) | 315 files, 1,766 chunks | 18 | 18 | 18 | 18 | 0 | 5.1 to 5.3 |
| merged with main (b86d171), before the two fixes below | 403 files, 2,201 chunks | 16 | 17 | stopped | 16 | 1 in each run | 5.5 |
| merged with main and fixed (2c27a6b) | 403 files, 2,201 chunks | 20 | 18 | 19 | 18 | 0 | 5.2 to 5.5 |
| **final tree: the same with one more commit of main, #223 (b5edd80)** | 405 files, 2,207 chunks | **18** | **18** | **18** | **18** | **0** | 5.1 to 5.6 |

The second row is the tree without one fix that its runs showed to be missing: on its second look the model once asked for a search, which could not fit beside the whole chunks, and the question about the Java heap stayed without an answer in two of the three runs. A whole run on the final tree took about five minutes, of which a little over three are indexing. Main moved once more while the fourth and fifth rows were measured (#223, restore in groups), so the measurement was repeated on the tree with it. Commits after b5edd80 change only this page, which is not indexed.

Not complete on the final tree: the location of the sample project memory and the journal event limit, in all three runs, each time refused by the support check (`answer_unsupported`). On the tree before it the first was refused in two of the three runs and the second in one.

The reports of these runs, as the product wrote them (they are kept with the measurement, not in the repository):

| Tree | Reports |
| --- | --- |
| 4527643 | `repository-20261115T092209Z-c93b808e.json`, `repository-20261115T093009Z-cd1c1076.json`, `repository-20261115T093840Z-1c4c8520.json`; further questions `repository-20261115T094839Z-d2eac281.json` |
| 6ca88dd | `repository-20261115T142542Z-e92bdbbd.json`, `repository-20261115T143320Z-067300c7.json`, `repository-20261115T143947Z-c286bd09.json` |
| 41e70ae | `repository-20261115T144609Z-2d5d8804.json`, `repository-20261115T145245Z-9ed48cb0.json`, `repository-20261115T145927Z-1f4274e4.json`; further questions `repository-20261115T160639Z-f647b8ce.json` |
| b86d171 | `repository-20261115T151756Z-7ebd1bda.json`, `repository-20261115T152347Z-6b8565a2.json` |
| 2c27a6b | `repository-20261115T154201Z-fa20a786.json`, `repository-20261115T154743Z-5689ca88.json`, `repository-20261115T155325Z-1894b8e9.json`; further questions `repository-20261115T155943Z-8783da3d.json` |
| b5edd80 | `repository-20261115T161659Z-1b38365a.json`, `repository-20261115T162233Z-2337aaf7.json`, `repository-20261115T162810Z-be427882.json`; further questions `repository-20261115T163416Z-49d6c6cd.json` |

### What the merge of main changed

Main had moved by two pull requests (#216 and #220) while this work was measured, and by a third (#223) during the measurement after the merge; the index grew from 315 to 405 files. Nothing main changed in the product alters what the answering model is shown here: the copy that is evaluated holds no records, so its project state is empty before and after (the order of #213 and the `confirmed_by` of #177 have nothing to act on), and no memory is held back from a local model (#181). What changed is the repository the questions are asked about. Right after the merge the twenty gave 16 and 17, with one complete answer counted as wrong in each run. Two causes, both found in the recorded calls:

- **The default timeout of a question** was not answered in either run. The team memory modules of #216 have a constant called `DEFAULT`. The definition lookup counted a name that is only a filler word ("default", "max") as a match for every question with that word, and the rule that prefers the rarer word put it before the setting, which lost its place among the four definitions shown. Fixed in the lookup: a filler word counts beside a question word of the name, never by itself. This fault was in the lookup before the merge; main only supplied the name that met it.
- **The Java heap** was answered as "1 GB", quoted from the deployment guide, which states it that way beside `-Xmx1024m`. Before the merge the first pass found no answer and the second look read the engine's argument list; on the larger index the guide's passage is among the first ten hits and the model answers from it at once. The answer is right, and the question's accepted forms did not list it, so the report counted it as complete and wrong. The question now also accepts the value in whole gigabytes, derived from the value read at run time like its other forms. Without that form the runs of the last two rows would read 19, 17, 18 and 17, 17, 17, with one complete and wrong answer each; all six answers were "1 GB".

For #213 this evaluation therefore shows only that the twenty did not get worse through the merge once the two points above were fixed (18, 18, 18 before; 20, 18, 19 and 18, 18, 18 after). Whether the model still gets the decision in force is shown by the recall evaluation, whose fixture has records: see the last section of this page.

### Step by step

To tell the steps apart, each was asked on the same three indexes of main (4527643; 312 files, 1,756 chunks), built once and kept, with the further questions on the first index. This is not `cairnmind eval repository` itself (nothing is indexed again, so a step cannot move the figure through a new index), but the same agent, questions and scoring. The model's replies still differ from one asking to the next on the same index, so a difference of one question in a row is not a measured effect. These rows were measured before the merge of main and were not repeated after it.

| Step | Index 1 | Index 2 | Index 3 | Worst | Complete and wrong | 22 further questions: right, wrong |
| --- | --- | --- | --- | --- | --- | --- |
| main | 15 | 15 | 16 | 15 | 0 | 11, 3 |
| + a quote may differ from its source in layout | 16 | 16 | 16 | 16 | 0 | 13, 3 |
| + definitions named with other words | 18 | 18 | 16 | 16 | 0 | 16, 2 |
| + each passage once among the first ten (taken out again) | 17 | 16 | 14 | 14 | 2 | 18, 1 |
| + the second look, re-spaced quotes and a hit's own ID | 19 | 19 | 17 | 17 | 0 | 16, 2 |
| + definitions shown first (the tree 6ca88dd of the table above) | 18 | 19 | 19 | 18 | 0 | 19, 1 |

The second look alone, before re-spaced quotes and a hit's own ID were accepted, was asked on index 1 only: 18 of the twenty, and 16 right, 2 wrong of the further questions. Its answer about the Java heap was right and was refused for the spacing of its quote and for citing the hit's ID, which is why the two rules were added. The further questions were judged by hand as before: a licence named in words the accepted forms do not list, an answer given in Chinese with the right number, and a number written with its decimal (10.0 for 10) count as right; a complete answer with another value counts as wrong.

What each step changes:

- **Quote layout.** A citation was refused when the model copied the words of a passage but not its layout: wrapped lines joined with a space, a Markdown code span without its backticks, a list written with other spacing, a quote of several lines with one left out. Validation now finds the passage of the source that has the same characters in the same order, backticks and the spacing beside punctuation aside (a space between two words or numbers must be there on both sides), and for a quote that skips lines of the source its longest literal run of whole lines, provided every line of the quote is in the source in the quote's order; the quote that is recorded and shown is always the source's own text. A quote written under the source ID of another shown passage is recorded for the one shown source that holds it, and the ID a hit was shown under stands for its source. After the one repair, citations that still fail are left out when another citation of the answer verified; the support check then judges the answer on the verified ones, and the result says in `freshness.warnings` that citations were left out.
- **Definitions.** The lookup missed settings whose name has a word the question does not use: a joining or unit word (`FILES_PER_TRANSACTION`, `poll_seconds`, `MAX_FILE_BYTES`), or a product name written joined in prose (`arcade_version` for "ArcadeDB version"). Such words now stand in a name like filler words, and a name word of five letters or more matches a question word that begins with it. Among candidates that share as many words, a value comes before a function or a class, and then the one whose question word the fewest other candidates have. A synonym (`arcade_url` for "address") is still out of reach. Several of these rules trace to one question each: the rule for a question word that begins a name word serves the ArcadeDB version question of the twenty, a value before a function or class the question about the steps of the knowledge agent, the rarer word among equal candidates the racy window question, and the filler-word rule the default question timeout after the merge. Each is written as a general rule, and no question, accepted answer or file name of either set is in the code, but none has been measured on a question it was not written for. Two side effects are known: a name word of five letters matches any question word that begins with it (a constant `CHECK_SECONDS` for a question about a checkpoint), and a name of one question word and a unit word, from an unrelated file, can take one of the four definition places.
- **Each passage once (taken out).** Chunks of one file overlap, so several of the ten hits can show the same excerpt. Asking retrieval for twenty hits and keeping ten distinct ones changed the order of the first ten as well, because deeper rankings are merged; one question that had passed in every earlier run was then answered from a script that ranked second, complete and wrong. The step was removed. The second look keeps one rule of it: overlapping chunks of a file are read whole once.
- **The second look.** A hit shows 900 characters of a chunk of up to 6,000, chosen by the words of the question, and the line that answers often has none of them (the Java heap is a literal in a list of arguments). When the first pass ends without an answer and a hit was shown in part, the question gets one more pass over the whole chunks of the first hits, without tools, inside the same time limit and context guard. Only a complete answer, validated and judged like any other, replaces the first result. For a chat server on another computer that pass stays inside the rule of #181: a chunk that may not leave the machine is not read whole either. How much it was used, from the recorded calls: before the merge of main (41e70ae) it ran for one of the twenty, the Java heap, in all three runs and answered it. On the trees after the merge (2c27a6b and b5edd80) it ran for none of the twenty in any of the six runs, because the heap is answered at once from the deployment guide; the 18, 18, 18 of b5edd80 does not rest on it. Of the 22 further questions it ran for three or four in a run and answered one (the indexing time before the merge, the size of the resume packet on b5edd80). Its cost falls on a question that honestly has no answer: one more search and a prompt of about 28,000 to 29,400 characters, under the guard of 32,768.
- **Definitions first.** The definitions stood at the end of the ten passages and outside the short catalog of the first call, and the model often quoted a passage that uses a value instead of the line that defines it. They are now shown before the hits.

### The 22 further questions are no longer unseen

The diagnosis this work started from read the failing answers of both sets, and the definition step was written with four of the further questions in view (the largest indexed file, the steps of the knowledge agent, the ArcadeDB address, the worker poll interval), as was the rule for a quote that skips lines. Their rise is therefore partly in-sample, like the twenty. A claim about questions nobody has looked at needs a new set.

Measured like the twenty, one run per tree with its own index, judged by hand as described above:

| Tree | Right | Complete and wrong | Not complete |
| --- | --- | --- | --- |
| main before the changes (4527643) | 13 | 0 | 9 |
| the changes before the merge of main (41e70ae) | 19 | 0 | 3 |
| merged with main and fixed (2c27a6b) | 16 | 2 | 4 |
| final tree (b5edd80) | 18 | 0 | 4 |

**After the merge the further questions are not better than before it and may be worse: 16 and 18 right against 19, and in one of the two runs two complete and wrong answers.** The two runs after the merge differ by one commit of main that touches nothing a question asks about, so their difference is the spread between runs, which one run per tree cannot separate from an effect. The recorded calls of the first run after the merge show what differed from the run before it, and it is again the repository that grew, not a change of main in the product:

- **The licence** was not answered in either run after the merge: the question says "published", the team memory modules and their tests (`memory_published…`, #216) now fill the ten hits, and the passage that states the licence is no longer among them.
- **The largest indexed file** was answered rightly and refused by the support check in the first run (answered and accepted in the second): among the definitions shown is now a constant of the backup reader (#216), the model cited it beside the right ones, and the check refused the answer for it.
- **The size of a resume packet** was answered in the first run with the neighbouring constant, the number of records (complete and wrong; right in the second). Lines that main added to the same file moved the excerpt that is shown, which now begins one line after the size limit.
- **The indexing time** was answered from the neighbouring changelog entry in the first run (complete and wrong), as in most earlier runs, and refused by the support check in the second; before the merge the second look happened to read the right entry.

None of the four was changed: each would be a new rule shaped on a question of this set.

### Other local models and a larger context: measured, not adopted

All on the tree of the row "+ the second look" of the step table, on its three kept indexes, so compare within this table. Graphics memory is what Ollama reports for the loaded chat model; the embedding model takes 1.2 GB more. Another program used 3 to 4 GB of the same card during most of these runs, which slowed them unevenly: the times compare rows only roughly.

| Chat model | Download | Graphics memory | Index 1 | Index 2 | Index 3 | Worst | Wrong | Further questions: right, wrong | Seconds per question (median) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `qwen3.5:9b`, context 16,384 (the default) | 6.6 GB | 6.1 GB | 19 | 19 | 17 | 17 | 0 | 16, 2 | 9 to 11 |
| `qwen3.5:9b`, context 32,768 | none | 6.7 GB | 19 | 19 | 17 | 17 | 0 | 18, 1 | 10 to 11 |
| `qwen3.5:9b-q8_0` (8-bit), context 16,384 | 10 GB | 9.5 GB | 18 | 18 | 18 | 18 | 0 | 17, 1 | 6 to 10 |
| `gemma4:12b-it-qat`, context 16,384 | 7.2 GB | 7.7 GB | 16 | 15 | 14 | 14 | 0 | 15, 1 | 11 to 12 |

No row differs from the default by more than the spread between runs, except `gemma4:12b-it-qat`, which was worse on the twenty. `bonsai-2:27b` was also measured: it could not answer in this pipeline (1 of 20 on one index; it read sources until the context guard or the step limit ended the question) and was dropped.

### What still fails

- **The location of the sample project memory** in most runs: no sentence of the repository states it, it appears in commands, and the support check refuses a path taken from a command.
- **The journal event limit** in more than half of the runs: the model reads the right definition and answers with a neighbouring number, or the support check refuses the right answer.
- Of the further questions: the installation time and the graphics card in almost every run (the passage is not among the first ten hits, and the model does not search), the indexing time in most runs, and since the merge of main the licence.
- The few definitions shown and the ten hits are places that a growing repository competes for: a new module with a similar name can take the place of the passage that answers, as the merge of main showed twice.
- The support check still decides differently on the same answer from run to run, and the same question on a new index still gets a different reply, because the prompt carries the IDs of that index.

## After the parsing and definition steps (pull request #206)

**Not met at that point: the worst of three runs on the final tree of these steps gave 15 of 20 complete and right; the target is 16.**

Measured on 14 November 2026 from release 0.10.0 (ae7ba4c), with the same models (`qwen3.5:9b` digest 56671c2a…, `qwen3-embedding:0.6b` digest ac6da0df…) and the same twenty questions, three runs per tree, each tree indexed from its own commit (280 files). Other work used the same graphics card during these runs; with the default question timeout of 60 seconds up to nine questions of a run ended in that timeout, which measured the shared card and not retrieval, so these runs set `ask_timeout` to 600 seconds. No question of the runs below took longer than 46 seconds, so the longer timeout ended none of them and the default of 60 seconds would not have ended any either; the earlier series that hit the timeout ran while the card was more heavily shared. The runs at 4326755 further down were made on another tree and day; compare the trees within this table, not with those runs.

| Tree | Run 1 | Run 2 | Run 3 | Worst | Complete and wrong |
| --- | --- | --- | --- | --- | --- |
| 0.10.0 | 15 | 14 | 15 | 14 | 1 |
| + TOML keys as definitions | 16 | 16 | 14 | 14 | 2 |
| + one question word with its file or class | 16 | 17 | 15 | 15 | 0 |
| + parameter defaults as definitions | 16 | 17 | 16 | 16 | 1 |
| + made-up values in the parsing test fixtures (final) | 16 | 17 | 15 | 15 | 0 |

What changed, by question:

- **Required Python version**: never complete on 0.10.0; complete and right in all twelve runs since the keys of `pyproject.toml` are definitions and `requires-python` is shown with the question.
- **Default question timeout** and **Java heap of the engine**: still never complete. The setting's name (`ask_timeout`) shares no word with the question and is in no file or class that does; the heap is a literal in a list inside a function, which none of these steps makes a definition.
- **Journal event limit**: the wrong answer quoted from a test (a count of 2) came in four of the twelve runs before the fixture step and in none of the three after it; with three runs that is not a measured effect.
- The other questions moved by one run here or there in both directions (quotes that were not literal, the support check). The spread between runs of one tree (two to three questions) is as large as the differences between the trees, so steps 3 to 5 cannot be told apart on these twenty questions; none made the figure worse beyond that spread.
- The questions an implementation never saw were not measured again.

## The twenty questions of this repository (in-sample)

The retrieval rules of this release (ten matched excerpts, the ranking of test files, the definitions shown with a question, the support check) were developed while measuring these same twenty questions, so this figure is in-sample: it shows that the rules work on the questions they were shaped on, not how well they generalise. The figure next to it, from questions the work never saw, is below.

Measured on 13 November 2026 at commit 4326755 (the final tree, after merging main) with `qwen3.5:9b` (digest 56671c2a…) and `qwen3-embedding:0.6b` (digest ac6da0df…) on an NVIDIA RTX 4080 Super under WSL2, three runs of `cairnmind eval repository .`. Each run indexed the files Git tracks (240 files, 1,305 chunks, without the paths of `NOT_INDEXED`) into its own data directory and database and took about 5 minutes.

| Run | Complete | Complete and right | Complete and wrong | Not complete |
| --- | --- | --- | --- | --- |
| 1 | 14 | 13 | 1 | 6 |
| 2 | 14 | 13 | 1 | 6 |
| 3 | 16 | 16 | 0 | 4 |

**Complete and right: 13 to 16 of 20 (worst run 13, best 16). The acceptance figure of 16 of 20 in the worst run is not met.** Complete means that every quote is a literal passage of the current source and that the support check agreed; right means that an accepted form of the expected value, read from the code at run time where it can change, appears in the answer as a whole token. A complete answer without it counts as wrong.

What failed, by topic:

- **Required Python version** (all three runs). The version is stated in `pyproject.toml`, `CONTRIBUTING.md` and `docs/INSTALL.md`, but none of these passages reached the model: it quoted the lock file and unrelated code, and the quotes were not literal or the answer could not be parsed.
- **Java heap of the engine** (all three runs). The value is a literal inside a function, not a definition; the model's quotes from the changelog and the settings were not literal.
- **Default question timeout** (all three runs). The setting's definition was not among the definitions shown, since its name has a word the question does not use; the model quoted unrelated code, and the answer was refused or `unknown`.
- **Journal event limit** (all three runs). The definition was shown and quoted. In run 1 the model answered with a count taken from a test next to it, and the support check accepted that answer (complete and wrong); in run 2 the check rejected the answer, in run 3 the quote was not literal.
- **Location of the sample project memory** (runs 1 and 2). The model quoted a test and the script that builds the sample, and the support check rejected the answer.
- **Length of the scope drift list** (runs 1 and 2). The definition was shown and quoted; the check rejected the answer once, the other time a quote was not literal.
- **Default context size of the chat model** (run 1). The model quoted a test, not literally.
- **The command that reads a snapshot with only Python and uv** (run 2, complete and wrong). The model named the script that builds the sample memory; its quote was literal and the support check accepted it.

Earlier measurements of the same twenty questions, for comparison: at commit 7b1743d, before the last merge of main, 15, 15 and 14 complete and right (worst 14); at commit e8a4a69, with the tests of the definition lookup still indexed, 15, 14 and 16, with one look-alike port from a test taken as the service port. An earlier measurement with a looser index and a looser scoring gave 16 to 17; it is superseded by the figures above. The spread between runs on the same tree (13 to 16 here) is as large as the differences between these trees.

## Questions the work never saw (out-of-sample)

A second set of 22 questions about this repository, written by the reviewer and kept outside the repository so that the implementation could not be shaped on it, judged by hand (a complete answer without the right value counts as wrong), one run each:

| Tree | Complete | Complete and right | Complete and wrong |
| --- | --- | --- | --- |
| main before this work | 7 | 4 | 3 |
| this work at an earlier commit | 12 | 10 | 2 |
| this work at commit c6ea2b5 | 14 | 14 | 0 |
| this work at commit 9b21625 | 13 | 12 | 1 |

The run at 9b21625 is the last one; the final tree differs from it by the merge of main and a changed documentation example, not by a retrieval rule. Its one wrong answer quoted "43 s instead of 257 s" and gave 257 seconds as the new indexing time; the support check rejected the same answer in the run before, so its verdict on it varies between runs. Nine questions were not complete: the largest indexed file, the installation time, the worker poll interval and the graphics card were answered `unknown`; the answers about the project version and the shortest quote were rejected by the support check; the answers about the ArcadeDB address, the racy window and the journal line limit had quotes that were not literal.

At c6ea2b5 the eight questions that were not complete were the project version (context budget), the ArcadeDB address and the installation time (answered `unknown`), the racy window, the worker poll interval, the indexing time and the graphics card (rejected by the support check), and the limit of the impact documentation scan (a quote that was not literal).

## What the support check does and does not catch

As measured while this was built: it rejects a value quoted from a test, a lock file or a comment that only mentions a number, and lets through a value of a neighbouring setting or a number used for something else, such as a count in a test. It also varies between runs on the same answer. In the final runs it accepted two wrong answers (a count from a test next to the journal limit, and a command named in a script that builds the sample) and rejected answers that quoted the right definition.

## Next steps as they stood before the first section

This list was written before the changes of the first section and is kept for the record; what became of each step is added to it.

- A larger model for the support check and for answering. Issue #67 makes the model selectable; most failures above are a model that quotes the right passage and answers from another, or a quote that is not literal. Measured in the first section: the other local models that were tried did not answer better than the default, the default stays, and the quotes that were not literal are now matched to their passage by the product.
- Values from `pyproject.toml` and other TOML or JSON configuration as findable definitions, as module constants and class fields already are, so that a question about the required Python version or the project version reaches the line that states it. Done for TOML (see the section at the top); JSON is not done, since most JSON files of a repository are data (this one holds seventy memory snapshot objects), and which JSON files count as configuration is still to be decided.
- A definition reached by one distinctive question word together with its owner or file, for a setting whose name has a word the question does not use (the default question timeout). Done for a name with one question word in a file or class that has one; it does not reach the default question timeout, whose setting, file and class share no word with the question.
- Parameter defaults of functions as definitions, for a value written only as a default or a literal inside a function (the Java heap of the engine). Done for Python parameter defaults; literals inside a function body (the Java heap) are not definitions yet; the second look of the first section reaches that one by reading the whole chunk.

# Results of `cairnmind eval run` with the embedding role on another server (#181)

**On the recall fixture, holding conversation chunks back from a remote embedding server cost no recall that these runs can show: 20 of 20 in all six runs with everything local, 20 of 20 in eight of nine runs with the embedding role remote and the default `memory_remote_classes` (21 of the fixture's 22 chunks held back), and 20 of 20 in all five runs with conversation added to the setting.** The fixture cannot show a larger drop, for the reason given below.

Measured on 15 November 2026 on the branch of #180 and #181 (release 0.10.0 with both), with `qwen3.5:9b` for chat and `qwen3-embedding:0.6b` for embedding in every run, on an NVIDIA RTX 4080 Super under WSL2. Each run of `run_evaluation` (the code of `cairnmind eval run`, without `--clients`) used its own temporary data directory and database, on one temporary ArcadeDB engine started for the measurement, never an installation.

**How the remote server was made.** No second computer was used. The embedding role pointed at an OpenAI-compatible server (`provider: openai_compatible`, `base_url: https://172.28.5.101:PORT/v1`, `allow_remote: true`): a TLS relay listening on this machine's own non-loopback address (its WSL network address) in front of the same local Ollama (127.0.0.1:11434, which serves an OpenAI-compatible API under `/v1`). The certificate was signed by a private authority made for the run with `openssl`, for that IP address, as the remote-engine test of #72 does. The product treated the address as remote: `is_loopback` was false, plain `http://` to it was refused ("Plain HTTP is allowed only for localhost; use HTTPS."), and the doctor wording for the role named the address and the classes held back. The product's HTTP client verifies certificates against certifi's bundle and ignores `SSL_CERT_FILE`, so the probe pointed certifi's bundle at certifi's authorities plus the private one; without that, the product refused the server. A run with the private authority not trusted was refused (a TLS failure at the relay, reported as a connection error); host-name checking is the HTTP client's default and was not tested separately, since no run used a certificate for another address. The chat role stayed on the local Ollama, so `ask` itself was not gated; only embedding was. The relay counted the connections that went through it.

| Setup | Runs | Recall (passed of 20) | Chunks held back from embedding | Connections through the relay per run | Time per run |
| --- | --- | --- | --- | --- | --- |
| Everything local (default) | 6 | 20 in all six | 0 of 22 | 0 | 104–115 s (three runs), 311–357 s (three runs) |
| Embedding remote, `memory_remote_classes` = `["project"]` (default) | 9 | 20 in eight, 19 in one | 21 of 22 (all conversation) | 25–34 | 99–210 s |
| Embedding remote, `["project", "conversation"]` | 5 | 20 in all five | 0 of 22 | 46–51 | 124–190 s |

In every run the five deterministic checks were true and the factual accuracy was 1.0, except the one run at 0.95. That run failed: its one miss, `recall08_review_state`, is a critical case ("Canonical facts differ", "Grounded response was incomplete"). That run came from an earlier version of the measuring script, which did not yet keep the answers of failed cases, so why the model answered differently is not known; the later runs kept them and had no failure. The script is not part of the repository; this section describes its method. With one miss in nine runs against none in eleven runs of the other setups, these runs cannot tell a missing vector from the spread of the model. The model made 43 to 55 calls per run in each setup.

**Why the fixture shows no drop.** The 20 golden cards and the canonical evidence are `manual` sources, which are `conversation`; only the plan's snapshot is `project`. So with the default setting the remote server embedded one chunk of 22, and the cards were found by full-text search alone. Each question names its case key (for example `recall08_review_state`), which is also in its card, so full-text search finds the right card with or without a vector. The fixture measures that held-back chunks stay findable by their words and that answering works with no vectors for them; it does not measure what vectors add for a question that shares no word with its answer. That needs a fixture whose questions are paraphrases; none was built here.

**What this shows and does not show.** It shows that the gate held back exactly the conversation chunks (21 of 22) and let them go once conversation was allowed (none held, about 20 more connections per run for the extra embeddings), that the product used an embedding server it considered remote over HTTPS with the certificate authority checked, and that recall on this fixture did not depend on those vectors. It does not show a server on another computer: network latency, another server's build of the model or its numeric differences, or a public certificate. The model was the same in all three setups, so the vectors of the "remote" server equal the local ones. Time per run measured a graphics card shared with another long measurement (one local series took three times as long as the other on the same code), not the relay or the remote role; the slow runs were repeated and gave the same recall.

# Results of `cairnmind eval run` before and after the context order of #213

**With real models the recall evaluation passed 20 of 20 in three runs before and in three runs after the change, and after it the decision in force is in the project state the answering model is shown, where before it was the one record left out.**

Measured on 15 November 2026 with `qwen3.5:9b` and `qwen3-embedding:0.6b` on the same card, by `run_evaluation` (the code of `cairnmind eval run`, without `--clients`), each run with its own temporary engine, data directory and database. "Before" is the branch of #104 as it stood before the merge of main (41e70ae, on main 4527643, which has neither #213 nor #177); "after" is the same branch merged with main (b86d171). The #104 changes are in both, so the two differ by what main brought.

| Tree | Runs | Recall (passed of 20) | Model calls per run | Seconds per run |
| --- | --- | --- | --- | --- |
| before (41e70ae) | 3 | 20, 20, 20 | 44 to 48 | 88 to 96 |
| after (b86d171) | 3 | 20, 20, 20 | 48 to 54 | 95 to 98 |

In all six runs the five deterministic checks were true and every critical case passed. Reports: before `recall-20261115T152716Z-a5995907.json`, `recall-20261115T152901Z-ff246428.json`, `recall-20261115T153044Z-54ca359e.json`; after `recall-20261115T153231Z-ac8d1033.json`, `recall-20261115T153414Z-78a4f996.json`, `recall-20261115T153558Z-3006998d.json` (kept with the measurement, not in the repository).

**What the model was shown.** The fixture has eight records, and the project state, which is bounded in characters, showed seven of them. The first prompt of every case was recorded:

- Before: four confirmed tasks, the proposed task, the finished task and the proposed decision. The record left out was the confirmed decision, the one in force.
- After: four confirmed tasks, the proposed task, the confirmed decision and the proposed decision. The record left out is the finished task. Each confirmed record also says who confirmed it (`confirmed_by`, here `seed`, #177), and the state is 158 characters longer (7,550 against 7,392).

**What this shows and does not show.** It shows the order of #213 reaching the model in a real run, and that recall on this fixture did not change with it or with `confirmed_by`. It does not show that an answer became better: the cases passed before as well, because a question names its case key and search finds the card whatever the project state holds, as the section on #181 above explains. The few more model calls per run after the merge (44, 45, 48 against 50, 48, 54) were not traced to a cause. `cairnmind eval repository` shows nothing about #213, because its copy has no records (see the first section). `cairnmind eval memory` was not run.
