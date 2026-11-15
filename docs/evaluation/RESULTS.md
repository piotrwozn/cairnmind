# Results of `cairnmind eval repository` on Cairnmind

**The acceptance figure of issue #104, sixteen of twenty questions complete and right in the worst of three runs, is not met: measured the same way, the worst run gave 14 of 20 on the 0.10.0 tree and 15 of 20 after the parsing and definition steps below, a change within the spread of two to three questions between runs.** The issue stays open.

This page is kept out of the copy that the evaluation indexes (see [../EVALUATION.md](../EVALUATION.md)), so that a run cannot read the results of earlier runs. It reports the questions that failed by topic, not by their answers.

## After the parsing and definition steps (next release)

**Still not met: the worst of three runs on the final tree of these steps gave 15 of 20 complete and right; the target is 16.**

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

## Next steps for the next release

These are measured on the out-of-sample set, not on the twenty questions above, so that the rules are not shaped on the figure they are judged by:

- A larger model for the support check and for answering. Issue #67 makes the model selectable; most failures above are a model that quotes the right passage and answers from another, or a quote that is not literal.
- Values from `pyproject.toml` and other TOML or JSON configuration as findable definitions, as module constants and class fields already are, so that a question about the required Python version or the project version reaches the line that states it. Done for TOML (see the section at the top); JSON is not done, since most JSON files of a repository are data (this one holds seventy memory snapshot objects), and which JSON files count as configuration is still to be decided.
- A definition reached by one distinctive question word together with its owner or file, for a setting whose name has a word the question does not use (the default question timeout). Done for a name with one question word in a file or class that has one; it does not reach the default question timeout, whose setting, file and class share no word with the question.
- Parameter defaults of functions as definitions, for a value written only as a default or a literal inside a function (the Java heap of the engine). Done for Python parameter defaults; literals inside a function body (the Java heap) are not definitions yet.
