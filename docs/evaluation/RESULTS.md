# Results of `cairnmind eval repository` on Cairnmind

This page is kept out of the copy that the evaluation indexes (see [../EVALUATION.md](../EVALUATION.md)), so that a run cannot read the results of earlier runs. It reports the questions that failed by topic, not by their answers.

## The twenty questions of this repository (in-sample)

The retrieval rules of this release (ten matched excerpts, the ranking of test files, the definitions shown with a question, the support check) were developed while measuring these same twenty questions, so this figure is in-sample: it shows that the rules work on the questions they were shaped on, not how well they generalise. The figure next to it, from questions the work never saw, is below.

Measured on 13 November 2026 at commit 7b1743d with `qwen3.5:9b` (digest 56671c2a…) and `qwen3-embedding:0.6b` (digest ac6da0df…) on an NVIDIA RTX 4080 Super under WSL2, three runs of `cairnmind eval repository .`. Each run indexed the files Git tracks (240 files, 1,301 chunks, without the paths of `NOT_INDEXED`) into its own data directory and database and took about 5 minutes.

| Run | Complete | Complete and right | Complete and wrong | Not complete |
| --- | --- | --- | --- | --- |
| 1 | 16 | 15 | 1 | 4 |
| 2 | 15 | 15 | 0 | 5 |
| 3 | 14 | 14 | 0 | 6 |

**Complete and right: 14 to 15 of 20 (worst run 14, best 15). This is below the acceptance figure of 16 of 20 that issue #104 set.** Complete means that every quote is a literal passage of the current source and that the support check agreed; right means that an accepted form of the expected value, read from the code at run time where it can change, appears in the answer as a whole token. A complete answer without it counts as wrong.

What failed, by topic:

- **Required Python version** (all three runs). The version is stated in `pyproject.toml`, `CONTRIBUTING.md` and `docs/INSTALL.md`, but none of these passages reached the model: it answered from the lock file or other documents, said `unknown`, or returned an answer that could not be parsed or whose quote was not literal.
- **Default question timeout** (all three runs). The setting's definition was not among the definitions shown, since its name has a word the question does not use; the model quoted other files, among them this evaluation's own module, whose documentation uses the same number as an example of how values are compared, and the answer was refused or `unknown`.
- **Java heap of the engine** (all three runs). The value is a literal inside a function, not a definition; the model's quotes from the changelog were not literal.
- **Journal event limit** (all three runs). The definition was shown; the model answered from a neighbouring constant of the same file, and the support check rejected it.
- **Default context size of the chat model** (runs 2 and 3). The model answered from the Ollama provider code, and the support check rejected it.
- **Location of the sample project memory** (run 3). The model quoted a test, and the support check rejected it.
- **The command that reads a snapshot with only Python and uv** (run 1, complete and wrong). The model named the script that builds the sample memory; its quote was literal and the support check accepted it.

Before the last two fixes (at commit e8a4a69, three runs, with the tests of the definition lookup still indexed) the same evaluation gave 15, 14 and 16 complete and right, one complete and wrong: the model took a test's look-alike port as the service port. Those tests are now in `NOT_INDEXED`. An earlier measurement with a looser index (every file under the repository) and a looser scoring gave 16 to 17; it is superseded by the figures above.

## Questions the work never saw (out-of-sample)

A second set of 22 questions about this repository, written by the reviewer and kept outside the repository so that the implementation could not be shaped on it, judged by hand (a complete answer without the right value counts as wrong), one run each:

| Tree | Complete | Complete and right | Complete and wrong |
| --- | --- | --- | --- |
| main before this work | 7 | 4 | 3 |
| this work at commit c6ea2b5 | 14 | 14 | 0 |
| this work at the final commit | *to be filled in after the reviewer's rerun* | | |

At c6ea2b5 the eight questions that were not complete were the project version (context budget), the ArcadeDB address and the installation time (answered `unknown`), the racy window, the worker poll interval, the indexing time and the graphics card (rejected by the support check; the last two were complete in earlier runs, so the check varies between runs), and the limit of the impact documentation scan (a quote that was not literal). The set is run by hand, once per tree; it will be rerun on the final tree.

## What the support check does and does not catch

As measured while this was built: it rejects a value quoted from a test, a lock file or a comment that only mentions a number, and lets through a value of a neighbouring setting or a number used for something else, such as a display limit. It also varies between runs on the same answer. In the runs above it accepted one wrong answer (a command named in a script that builds the sample) and rejected answers taken from neighbouring constants and tests.
